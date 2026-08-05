"""Tests for quota status logic and the bundled EC2 target list.

Credential-free: these cover the state machine and the integrity of
``EC2_VCPU_TARGETS``, not the API calls.
"""

import pytest

from pdum.aws import quotas
from pdum.aws.quotas import QuotaStatus, QuotaTarget
from tests.fakes import FakeQuotasClient, client_error

TARGET = QuotaTarget("On-demand P", "L-417A185B", 1152)


class TestQuotaStatus:
    """`satisfied` / `has_open_request` / `needs_submit` are mutually consistent."""

    def test_met_target_is_satisfied(self):
        status = QuotaStatus(target=TARGET, current=2048)
        assert status.satisfied
        assert not status.needs_submit
        assert status.state == "satisfied"

    def test_exactly_met_target_counts_as_satisfied(self):
        assert QuotaStatus(target=TARGET, current=1152).satisfied

    def test_shortfall_needs_submitting(self):
        status = QuotaStatus(target=TARGET, current=0)
        assert not status.satisfied
        assert status.needs_submit
        assert status.state == "to-submit"

    def test_open_request_suppresses_resubmission(self):
        # The API allows only one open request per quota, so an in-flight
        # request must not be resubmitted even though the target is unmet.
        status = QuotaStatus(target=TARGET, current=0, pending_value=1152, pending_status="CASE_OPENED")
        assert status.has_open_request
        assert not status.needs_submit
        assert status.state == "open"

    def test_absent_quota_is_not_satisfied(self):
        # A quota code missing from a region reports None, which must not be
        # mistaken for "already large enough".
        status = QuotaStatus(target=TARGET, current=None)
        assert not status.satisfied
        assert status.needs_submit


class TestEc2Targets:
    """The bundled starting point should be internally coherent."""

    def test_codes_are_unique(self):
        codes = [t.code for t in quotas.EC2_VCPU_TARGETS]
        assert len(codes) == len(set(codes))

    def test_labels_are_unique(self):
        labels = [t.label for t in quotas.EC2_VCPU_TARGETS]
        assert len(labels) == len(set(labels))

    @pytest.mark.parametrize("target", quotas.EC2_VCPU_TARGETS, ids=lambda t: t.code)
    def test_codes_look_like_quota_codes(self, target):
        assert target.code.startswith("L-")
        assert target.value > 0

    def test_spot_is_sized_above_on_demand(self):
        # Spot vCPUs are cheaper, so the same dollar ceiling should buy more.
        by_label = {t.label: t.value for t in quotas.EC2_VCPU_TARGETS}
        for family in ["Standard", "G and VT", "P", "X", "Inf", "Trn", "DL"]:
            on_demand = next(v for k, v in by_label.items() if k.startswith("On-demand") and family in k)
            spot = by_label[f"Spot {family}"]
            assert spot > on_demand, family


def test_open_statuses_cover_both_pending_forms():
    assert "PENDING" in quotas.OPEN_STATUSES
    assert "CASE_OPENED" in quotas.OPEN_STATUSES
    assert "APPROVED" not in quotas.OPEN_STATUSES


class TestReportAndSubmit:
    """Report/submit logic, exercised against an in-memory Service Quotas client."""

    @pytest.fixture
    def targets(self):
        return [
            QuotaTarget("already big", "L-AAA", 100),
            QuotaTarget("in flight", "L-BBB", 100),
            QuotaTarget("needs raising", "L-CCC", 100),
        ]

    @pytest.fixture
    def fake(self, targets, monkeypatch):
        stub = FakeQuotasClient(
            applied={"L-AAA": 200, "L-BBB": 5, "L-CCC": 5},
            pending={"L-BBB": (100, "CASE_OPENED")},
        )
        monkeypatch.setattr(quotas, "client", lambda *a, **k: stub)
        return stub

    def test_report_joins_applied_and_pending(self, targets, fake):
        states = {s.target.code: s.state for s in quotas.report(targets)}
        assert states == {"L-AAA": "satisfied", "L-BBB": "open", "L-CCC": "to-submit"}

    def test_submit_only_raises_what_is_needed(self, targets, fake):
        results = quotas.submit(targets)
        outcomes = {r.target.code: r.outcome for r in results}
        assert outcomes == {"L-AAA": "skipped", "L-BBB": "skipped", "L-CCC": "submitted"}
        # Only the one genuine gap reached the API.
        assert fake.requested == [("L-CCC", 100.0)]

    def test_submit_is_idempotent_once_a_request_is_open(self, targets, fake):
        quotas.submit(targets)
        fake.pending["L-CCC"] = (100, "PENDING")
        again = quotas.submit(targets)
        assert all(r.outcome == "skipped" for r in again)
        assert len(fake.requested) == 1

    def test_dry_run_writes_nothing(self, targets, fake):
        results = quotas.submit(targets, dry_run=True)
        assert [r.outcome for r in results if r.outcome == "dry-run"]
        assert fake.requested == []

    def test_submit_stops_at_the_account_cap(self, monkeypatch):
        many = [QuotaTarget(f"q{i}", f"L-{i:03d}", 100) for i in range(5)]
        stub = FakeQuotasClient(applied={t.code: 0 for t in many}, cap_after=2)
        monkeypatch.setattr(quotas, "client", lambda *a, **k: stub)

        results = quotas.submit(many)
        # Two land, the third trips the cap, and iteration stops there rather
        # than burning through the rest with identical failures.
        assert [r.outcome for r in results] == ["submitted", "submitted", "cap-reached"]
        assert len(stub.requested) == 2
        assert "maximum number of open" in results[-1].error

    def test_unexpected_errors_are_recorded_without_stopping(self, monkeypatch):
        pair = [QuotaTarget("bad", "L-BAD", 100), QuotaTarget("good", "L-GOOD", 100)]
        stub = FakeQuotasClient(applied={"L-BAD": 0, "L-GOOD": 0})

        original = stub.request_service_quota_increase

        def selective(ServiceCode, QuotaCode, DesiredValue):  # noqa: N803
            if QuotaCode == "L-BAD":
                raise client_error("AccessDeniedException", "RequestServiceQuotaIncrease")
            return original(ServiceCode=ServiceCode, QuotaCode=QuotaCode, DesiredValue=DesiredValue)

        stub.request_service_quota_increase = selective
        monkeypatch.setattr(quotas, "client", lambda *a, **k: stub)

        results = quotas.submit(pair)
        assert [r.outcome for r in results] == ["error", "submitted"]
        assert "AccessDeniedException" in results[0].error

    def test_missing_quota_in_region_is_reported_as_absent(self, monkeypatch):
        target = [QuotaTarget("absent here", "L-NOPE", 100)]
        stub = FakeQuotasClient(applied={})
        monkeypatch.setattr(quotas, "client", lambda *a, **k: stub)

        status = quotas.report(target)[0]
        assert status.current is None
        assert status.needs_submit
