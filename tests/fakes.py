"""In-memory stand-ins for the AWS clients these modules call.

Not collected by pytest (the filename does not match ``test_*.py``). These exist
so the request/response logic can be tested without credentials or network.
"""

from datetime import UTC, datetime
from typing import Any

from botocore.exceptions import ClientError


def client_error(code: str, operation: str, message: str = "boom") -> ClientError:
    """Build a ``ClientError`` shaped the way botocore raises them."""
    return ClientError({"Error": {"Code": code, "Message": message}}, operation)


class _Paginator:
    def __init__(self, pages: list[dict[str, Any]]) -> None:
        self._pages = pages

    def paginate(self, **_kwargs: Any) -> list[dict[str, Any]]:
        return self._pages


class FakeQuotasClient:
    """A Service Quotas client backed by dictionaries.

    Parameters
    ----------
    applied : dict
        Quota code mapped to its currently applied value. Codes absent from this
        dict raise ``NoSuchResourceException``, mimicking a quota that does not
        exist in the region.
    pending : dict, optional
        Quota code mapped to ``(desired, status)`` for in-flight requests.
    cap_after : int, optional
        Raise ``QuotaExceededException`` once this many increases have been
        requested, mimicking the account-wide open-request cap.
    """

    def __init__(
        self,
        applied: dict[str, float],
        pending: dict[str, tuple[float, str]] | None = None,
        cap_after: int | None = None,
    ) -> None:
        self.applied = applied
        self.pending = pending or {}
        self.cap_after = cap_after
        self.requested: list[tuple[str, float]] = []

    def get_service_quota(self, ServiceCode: str, QuotaCode: str) -> dict[str, Any]:  # noqa: N803
        if QuotaCode not in self.applied:
            raise client_error("NoSuchResourceException", "GetServiceQuota")
        return {"Quota": {"Value": self.applied[QuotaCode]}}

    def get_paginator(self, name: str) -> _Paginator:
        assert name == "list_requested_service_quota_change_history"
        return _Paginator(
            [
                {
                    "RequestedQuotas": [
                        {
                            "QuotaCode": code,
                            "QuotaName": f"Quota {code}",
                            "DesiredValue": value,
                            "Status": status,
                            "Id": f"req-{code}",
                            "CaseId": f"case-{code}",
                            "Created": datetime(2026, 8, 5, 15, 0, tzinfo=UTC),
                            "LastUpdated": datetime(2026, 8, 5, 15, 30, tzinfo=UTC),
                        }
                        for code, (value, status) in self.pending.items()
                    ]
                }
            ]
        )

    def request_service_quota_increase(
        self,
        ServiceCode: str,  # noqa: N803
        QuotaCode: str,  # noqa: N803
        DesiredValue: float,  # noqa: N803
    ) -> dict[str, Any]:
        if self.cap_after is not None and len(self.requested) >= self.cap_after:
            raise client_error(
                "QuotaExceededException",
                "RequestServiceQuotaIncrease",
                "This account has reached the maximum number of open service quota increase (SQI) requests.",
            )
        self.requested.append((QuotaCode, DesiredValue))
        return {"RequestedQuota": {"Id": f"req-{len(self.requested)}", "Status": "PENDING"}}


class FakeSsmClient:
    """An SSM client backed by a dict of full parameter paths to values."""

    def __init__(self, parameters: dict[str, str] | None = None) -> None:
        self.parameters = dict(parameters or {})
        self.types: dict[str, str] = dict.fromkeys(self.parameters, "SecureString")
        self.deleted: list[str] = []

    def get_parameter(self, Name: str, WithDecryption: bool = False) -> dict[str, Any]:  # noqa: N803
        if Name not in self.parameters:
            raise client_error("ParameterNotFound", "GetParameter")
        return {"Parameter": {"Value": self.parameters[Name]}}

    def put_parameter(self, Name: str, Value: str, Type: str, Overwrite: bool) -> dict[str, Any]:  # noqa: N803
        self.parameters[Name] = Value
        self.types[Name] = Type
        return {"Version": 1}

    def delete_parameter(self, Name: str) -> dict[str, Any]:  # noqa: N803
        if Name not in self.parameters:
            raise client_error("ParameterNotFound", "DeleteParameter")
        del self.parameters[Name]
        self.deleted.append(Name)
        return {}

    def get_paginator(self, name: str) -> "_SsmByPathPaginator":
        assert name == "get_parameters_by_path"
        return _SsmByPathPaginator(self)


class _SsmByPathPaginator:
    """Honours the ``Path`` filter, as the real by-path API does server-side."""

    def __init__(self, ssm: FakeSsmClient) -> None:
        self._ssm = ssm

    def paginate(self, Path: str, **_kwargs: Any) -> list[dict[str, Any]]:  # noqa: N803
        return [
            {
                "Parameters": [
                    {
                        "Name": path,
                        "Type": self._ssm.types.get(path, "SecureString"),
                        "Version": 1,
                        "LastModifiedDate": datetime(2026, 8, 5, 12, 0, tzinfo=UTC),
                    }
                    for path in sorted(self._ssm.parameters)
                    if path.startswith(Path)
                ]
            }
        ]
