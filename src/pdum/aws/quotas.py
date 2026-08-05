"""Service Quotas: inspect current limits and request increases.

A fresh AWS account can launch almost nothing — typically 5 vCPUs of standard
on-demand EC2 and **zero** of every accelerator family. Raising that is a
per-region, per-quota request process with several non-obvious traps, which this
module encodes.

```python
from pdum.aws import quotas

statuses = quotas.report(quotas.EC2_VCPU_TARGETS, region="us-east-1")
for s in statuses:
    print(s.target.label, s.current, s.state)

results = quotas.submit(quotas.EC2_VCPU_TARGETS, region="us-east-1")
```

Functions here return data and never print, so callers own presentation.

Things that surprise people
---------------------------
**EC2 quotas count vCPUs, not instances.** "I want 4 GPUs" becomes "I want 768
vCPUs of P", because ``p5.48xlarge`` is 192 vCPUs. Convert through the instance
type you actually intend to run.

**Instance generation is not a quota dimension.** ``P`` spans p3 (V100), p4
(A100) and p5 (H100); ``G`` spans g4dn (T4), g5 (A10G), g6 (L4) and g6e (L40S).
Old and new hardware cannot be requested separately.

**Nor is CPU architecture.** Graviton (``c7g``, ``c8g``, ``m7g``, ``r7g``) draws
from the *Standard* pool, whose name lists instance-family letters
(A, C, D, H, I, M, R, T, Z), not architectures — the ``A`` is ``a1``, not "ARM".

**Quota is a ceiling, not a commitment — and not capacity.** Holding a large
quota costs nothing, so asking high has no downside but refusal. But an approved
P quota does not mean ``p5`` will launch; on-demand H100 is frequently
unavailable regardless of quota, and Capacity Blocks for ML is the mechanism
that actually reserves that class of hardware.

Rate limits on requesting
-------------------------
Two meta-quotas govern the requests themselves:

* ``L-36BDD542`` "Active requests per quota" is **1**, so an in-flight request
  cannot be revised — you wait for it to be decided.
* An undocumented account-wide cap of :data:`ACCOUNT_OPEN_REQUEST_CAP` open
  requests, which is not readable from any API and surfaces only as
  ``QuotaExceededException``. It held at exactly 20 on two separate accounts.

So a large batch cannot be submitted in one sitting. :func:`submit` stops
cleanly at the cap and is idempotent, making the workflow: submit, wait for
cases to be decided, submit again.

Looking up quota codes
----------------------
Never hand-type a quota code; a wrong one is a wasted support case.

```console
$ aws service-quotas list-service-quotas --service-code ec2 \\
    --query 'Quotas[].[QuotaCode,QuotaName,Value]' --output text
```

Note that ``list-service-quotas`` returns only quotas with an *applied* value,
so ones never modified can be missing. Use ``list-aws-default-service-quotas``
to enumerate the full catalogue, and check ``Adjustable`` before requesting —
some quotas cannot be raised at all.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from botocore.exceptions import ClientError

from pdum.aws.core import client

__all__ = [
    "ACCOUNT_OPEN_REQUEST_CAP",
    "EC2_VCPU_TARGETS",
    "OPEN_STATUSES",
    "QuotaRequest",
    "QuotaStatus",
    "QuotaTarget",
    "SubmitResult",
    "current_values",
    "open_requests",
    "report",
    "request_history",
    "submit",
]

#: Request statuses that still occupy one of the account's open-request slots.
OPEN_STATUSES = frozenset({"PENDING", "CASE_OPENED"})

#: Undocumented account-wide cap on simultaneously open quota-increase requests.
#: Not readable from any API; observed to hold at exactly 20 on two accounts.
ACCOUNT_OPEN_REQUEST_CAP = 20


@dataclass(frozen=True, slots=True)
class QuotaTarget:
    """A quota and the value to request for it.

    Attributes
    ----------
    label : str
        Human-readable name, used only for display.
    code : str
        Service Quotas code, e.g. ``"L-417A185B"``.
    value : float
        Desired value. For EC2 compute quotas this is a vCPU count.
    """

    label: str
    code: str
    value: float


@dataclass(frozen=True, slots=True)
class QuotaStatus:
    """Where one quota stands relative to its target.

    Attributes
    ----------
    target : QuotaTarget
        The quota and desired value.
    current : float or None
        Currently applied value, or ``None`` if the quota does not exist in this
        region.
    pending_value : float or None
        Desired value of an in-flight request, if any.
    pending_status : str or None
        Status of that in-flight request, e.g. ``"CASE_OPENED"``.
    """

    target: QuotaTarget
    current: float | None
    pending_value: float | None = None
    pending_status: str | None = None

    @property
    def satisfied(self) -> bool:
        """Whether the applied value already meets the target."""
        return self.current is not None and self.current >= self.target.value

    @property
    def has_open_request(self) -> bool:
        """Whether a request for this quota is awaiting a decision."""
        return self.pending_status is not None

    @property
    def needs_submit(self) -> bool:
        """Whether this quota still needs a request raised."""
        return not self.satisfied and not self.has_open_request

    @property
    def state(self) -> str:
        """A one-word summary: ``satisfied``, ``open`` or ``to-submit``."""
        if self.satisfied:
            return "satisfied"
        return "open" if self.has_open_request else "to-submit"


@dataclass(frozen=True, slots=True)
class QuotaRequest:
    """One quota-increase request as Service Quotas recorded it.

    Attributes
    ----------
    code : str
        Quota code the request targets.
    name : str
        Human-readable quota name from the API.
    desired : float
        Value that was asked for.
    status : str
        ``PENDING``, ``CASE_OPENED``, ``APPROVED``, ``DENIED``, ``CASE_CLOSED``,
        ``NOT_APPROVED`` or ``INVALID_REQUEST``.
    request_id : str or None
        Service Quotas request id.
    case_id : str or None
        Support case id, present once a request has been escalated to a human.
    created : datetime or None
        When the request was raised.
    last_updated : datetime or None
        When it last changed status.
    """

    code: str
    name: str
    desired: float
    status: str
    request_id: str | None = None
    case_id: str | None = None
    created: datetime | None = None
    last_updated: datetime | None = None

    @property
    def is_open(self) -> bool:
        """Whether this request still awaits a decision."""
        return self.status in OPEN_STATUSES

    @property
    def is_approved(self) -> bool:
        """Whether this request was granted.

        An unapproved request does not imply the quota is unchanged. AWS also
        raises quotas on young accounts automatically, independently of any
        request — observed raising standard EC2 vCPU limits in a region where no
        request had been submitted at all. Always compare against the applied
        value from :func:`current_values` rather than inferring it from status.
        """
        return self.status == "APPROVED"


@dataclass(frozen=True, slots=True)
class SubmitResult:
    """The outcome of trying to raise one quota.

    Attributes
    ----------
    target : QuotaTarget
        The quota that was attempted.
    outcome : str
        One of ``"submitted"``, ``"skipped"``, ``"dry-run"``, ``"cap-reached"``
        or ``"error"``.
    request_id : str or None
        Service Quotas request id, when one was created.
    request_status : str or None
        Status the request came back with, typically ``"PENDING"``.
    error : str or None
        Error message when ``outcome`` is ``"error"`` or ``"cap-reached"``.
    """

    target: QuotaTarget
    outcome: str
    request_id: str | None = None
    request_status: str | None = None
    error: str | None = None


def _quotas_client(region: str | None) -> Any:
    return client("service-quotas", region)


def current_values(
    targets: list[QuotaTarget],
    *,
    service_code: str = "ec2",
    region: str | None = None,
) -> dict[str, float | None]:
    """Look up the currently applied value for each target's quota.

    Parameters
    ----------
    targets : list of QuotaTarget
        Quotas to look up.
    service_code : str, default "ec2"
        Service Quotas service code.
    region : str, optional
        Region to query. Quotas are per-region.

    Returns
    -------
    dict of str to (float or None)
        Quota code mapped to its applied value, or ``None`` where the quota does
        not exist in this region.
    """
    quotas = _quotas_client(region)
    out: dict[str, float | None] = {}
    for target in targets:
        try:
            response = quotas.get_service_quota(ServiceCode=service_code, QuotaCode=target.code)
            out[target.code] = response["Quota"]["Value"]
        except ClientError:
            # Not every quota code exists in every region; treat as absent.
            out[target.code] = None
    return out


def request_history(*, service_code: str = "ec2", region: str | None = None) -> list[QuotaRequest]:
    """List every quota-increase request, decided or not.

    This is how you find out what AWS actually did with a batch: statuses
    include ``APPROVED``, ``DENIED`` and ``CASE_CLOSED`` alongside the open
    ``PENDING`` and ``CASE_OPENED``.

    Parameters
    ----------
    service_code : str, default "ec2"
        Service Quotas service code.
    region : str, optional
        Region to query.

    Returns
    -------
    list of QuotaRequest
        Requests newest-updated first.
    """
    quotas = _quotas_client(region)
    paginator = quotas.get_paginator("list_requested_service_quota_change_history")
    out: list[QuotaRequest] = []
    for page in paginator.paginate(ServiceCode=service_code):
        for request in page["RequestedQuotas"]:
            out.append(
                QuotaRequest(
                    code=request["QuotaCode"],
                    name=request.get("QuotaName", ""),
                    desired=request["DesiredValue"],
                    status=request["Status"],
                    request_id=request.get("Id"),
                    case_id=request.get("CaseId"),
                    created=request.get("Created"),
                    last_updated=request.get("LastUpdated"),
                )
            )
    out.sort(key=lambda r: (r.last_updated is None, r.last_updated), reverse=True)
    return out


def open_requests(*, service_code: str = "ec2", region: str | None = None) -> dict[str, tuple[float, str]]:
    """Find quota-increase requests that are still awaiting a decision.

    Parameters
    ----------
    service_code : str, default "ec2"
        Service Quotas service code.
    region : str, optional
        Region to query.

    Returns
    -------
    dict of str to tuple
        Quota code mapped to ``(desired_value, status)`` for each open request.
    """
    return {
        request.code: (request.desired, request.status)
        for request in request_history(service_code=service_code, region=region)
        if request.is_open
    }


def report(
    targets: list[QuotaTarget],
    *,
    service_code: str = "ec2",
    region: str | None = None,
) -> list[QuotaStatus]:
    """Describe where each target stands, without changing anything.

    Parameters
    ----------
    targets : list of QuotaTarget
        Quotas to inspect.
    service_code : str, default "ec2"
        Service Quotas service code.
    region : str, optional
        Region to query.

    Returns
    -------
    list of QuotaStatus
        One entry per target, in the order given.
    """
    applied = current_values(targets, service_code=service_code, region=region)
    pending = open_requests(service_code=service_code, region=region)
    out: list[QuotaStatus] = []
    for target in targets:
        value, status = pending.get(target.code, (None, None))
        out.append(
            QuotaStatus(
                target=target,
                current=applied.get(target.code),
                pending_value=value,
                pending_status=status,
            )
        )
    return out


def submit(
    targets: list[QuotaTarget],
    *,
    service_code: str = "ec2",
    region: str | None = None,
    dry_run: bool = False,
) -> list[SubmitResult]:
    """Request increases for every target that needs one.

    Idempotent: targets already satisfied, or already carrying an open request,
    are skipped rather than resubmitted. Stops at the first
    ``QuotaExceededException``, since once the account's open-request cap is hit
    every subsequent call would fail the same way.

    Parameters
    ----------
    targets : list of QuotaTarget
        Quotas to raise.
    service_code : str, default "ec2"
        Service Quotas service code.
    region : str, optional
        Region to act on.
    dry_run : bool, default False
        Report what would be submitted without calling the write API.

    Returns
    -------
    list of SubmitResult
        One entry per target considered. Iteration stops early on
        ``"cap-reached"``, so the list may be shorter than *targets*; check for
        that outcome to know whether to re-run later.
    """
    quotas = _quotas_client(region)
    statuses = report(targets, service_code=service_code, region=region)
    out: list[SubmitResult] = []

    for status in statuses:
        target = status.target
        if not status.needs_submit:
            out.append(SubmitResult(target=target, outcome="skipped"))
            continue
        if dry_run:
            out.append(SubmitResult(target=target, outcome="dry-run"))
            continue
        try:
            response = quotas.request_service_quota_increase(
                ServiceCode=service_code,
                QuotaCode=target.code,
                DesiredValue=float(target.value),
            )
            requested = response["RequestedQuota"]
            out.append(
                SubmitResult(
                    target=target,
                    outcome="submitted",
                    request_id=requested.get("Id"),
                    request_status=requested.get("Status"),
                )
            )
        except ClientError as exc:
            error = exc.response["Error"]
            if error["Code"] == "QuotaExceededException":
                out.append(SubmitResult(target=target, outcome="cap-reached", error=error["Message"]))
                break
            out.append(SubmitResult(target=target, outcome="error", error=f"{error['Code']}: {error['Message']}"))
    return out


#: A broad EC2 starting point: roughly $300/hr of headroom per family at
#: us-east-1 Linux on-demand prices as of 2026-08-05. Spot is set at twice the
#: on-demand vCPU count, because a spot vCPU costs roughly a half to a third as
#: much and so the same dollar ceiling buys more of them.
#:
#: Treat this as an example to copy and edit, not a recommendation — the right
#: numbers depend entirely on what you intend to run.
EC2_VCPU_TARGETS: list[QuotaTarget] = [
    QuotaTarget("On-demand Standard (incl. Graviton)", "L-1216C47A", 6912),
    QuotaTarget("On-demand G and VT (T4/A10G/L4/L40S)", "L-DB2E81BA", 1920),
    QuotaTarget("On-demand P (V100/A100/H100)", "L-417A185B", 1152),
    QuotaTarget("On-demand X (high-memory)", "L-7295265B", 768),
    QuotaTarget("On-demand Inf", "L-1945791B", 576),
    QuotaTarget("On-demand Trn", "L-2C3B7624", 768),
    QuotaTarget("On-demand DL", "L-6E869C2A", 192),
    QuotaTarget("Spot Standard", "L-34B43A08", 13824),
    QuotaTarget("Spot G and VT", "L-3819A6DF", 3840),
    QuotaTarget("Spot P", "L-7212CCBC", 2304),
    QuotaTarget("Spot X", "L-E3A00192", 1536),
    QuotaTarget("Spot Inf", "L-B5D1601B", 1152),
    QuotaTarget("Spot Trn", "L-6B0D517C", 1536),
    QuotaTarget("Spot DL", "L-85EED4F7", 384),
]
