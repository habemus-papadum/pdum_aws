"""Tests for the ``pdum-aws`` command line interface.

Every test runs against in-memory AWS clients, so nothing here needs
credentials or a network.
"""

import json

import pytest
from typer.testing import CliRunner

from pdum.aws import quotas as quotas_lib
from pdum.aws.cli import app
from tests.fakes import FakeQuotasClient, FakeSsmClient

runner = CliRunner()


@pytest.fixture(autouse=True)
def _no_ambient_prefix(monkeypatch):
    """Stop a developer's own PDUM_SSM_PREFIX from leaking into assertions."""
    monkeypatch.delenv("PDUM_SSM_PREFIX", raising=False)


@pytest.fixture
def ssm(monkeypatch):
    fake = FakeSsmClient({"/myapp/API_KEY": "sekrit", "/myapp/OTHER": "value"})
    monkeypatch.setattr("pdum.aws.secrets.client", lambda *a, **k: fake)
    return fake


@pytest.fixture
def quotas(monkeypatch):
    fake = FakeQuotasClient(
        applied={"L-AAA": 200, "L-BBB": 5, "L-CCC": 5},
        pending={"L-BBB": (100, "CASE_OPENED")},
    )
    monkeypatch.setattr("pdum.aws.quotas.client", lambda *a, **k: fake)
    targets = [
        quotas_lib.QuotaTarget("already big", "L-AAA", 100),
        quotas_lib.QuotaTarget("in flight", "L-BBB", 100),
        quotas_lib.QuotaTarget("needs raising", "L-CCC", 100),
    ]
    monkeypatch.setattr(quotas_lib, "EC2_VCPU_TARGETS", targets)
    return fake


class TestTopLevel:
    def test_help_lists_both_command_groups(self):
        result = runner.invoke(app, ["--help"])
        assert result.exit_code == 0
        assert "secrets" in result.stdout
        assert "quotas" in result.stdout

    def test_no_args_shows_help_rather_than_failing_silently(self):
        assert "Usage" in runner.invoke(app, []).stdout

    def test_whoami(self, monkeypatch):
        monkeypatch.setattr(
            "pdum.aws.cli.aws.whoami",
            lambda *a, **k: {"Account": "123456789012", "Arn": "arn:aws:iam::123456789012:root"},
        )
        result = runner.invoke(app, ["whoami"])
        assert result.exit_code == 0
        assert "123456789012" in result.stdout


class TestSecretsPrefix:
    def test_prefix_is_required(self):
        result = runner.invoke(app, ["secrets", "list"])
        assert result.exit_code != 0

    def test_prefix_can_come_from_the_environment(self, ssm, monkeypatch):
        monkeypatch.setenv("PDUM_SSM_PREFIX", "/myapp/")
        result = runner.invoke(app, ["secrets", "list"])
        assert result.exit_code == 0
        assert "API_KEY" in result.stdout

    def test_a_root_prefix_is_refused_with_a_readable_message(self):
        result = runner.invoke(app, ["secrets", "--prefix", "/", "list"])
        assert result.exit_code == 2
        assert "non-root prefix" in result.stdout


class TestSecretsCommands:
    def invoke(self, *args):
        return runner.invoke(app, ["secrets", "--prefix", "/myapp/", *args])

    def test_list(self, ssm):
        result = self.invoke("list")
        assert result.exit_code == 0
        assert result.stdout.split() == ["API_KEY", "OTHER"]

    def test_list_long_shows_metadata(self, ssm):
        result = self.invoke("list", "--long")
        assert result.exit_code == 0
        assert "secure" in result.stdout
        assert "2026-08-05" in result.stdout

    def test_get_prints_the_value_alone(self, ssm, monkeypatch):
        monkeypatch.delenv("API_KEY", raising=False)
        result = self.invoke("get", "API_KEY")
        assert result.exit_code == 0
        assert result.stdout.strip() == "sekrit"

    def test_get_missing_exits_nonzero(self, ssm, monkeypatch):
        monkeypatch.delenv("NOPE", raising=False)
        result = self.invoke("get", "NOPE")
        assert result.exit_code == 1
        assert "not found" in result.stdout

    def test_set_stores_securely_by_default(self, ssm):
        result = self.invoke("set", "NEW", "v")
        assert result.exit_code == 0
        assert ssm.parameters["/myapp/NEW"] == "v"
        assert ssm.types["/myapp/NEW"] == "SecureString"

    def test_set_plain(self, ssm):
        self.invoke("set", "NEW", "v", "--plain")
        assert ssm.types["/myapp/NEW"] == "String"

    def test_set_refuses_an_empty_value(self, ssm):
        result = runner.invoke(app, ["secrets", "--prefix", "/myapp/", "set", "EMPTY", "-"], input="")
        assert result.exit_code == 1
        assert "empty" in result.stdout
        assert "/myapp/EMPTY" not in ssm.parameters

    def test_set_reads_stdin(self, ssm):
        result = runner.invoke(app, ["secrets", "--prefix", "/myapp/", "set", "PIPED", "-"], input="from-stdin\n")
        assert result.exit_code == 0
        assert ssm.parameters["/myapp/PIPED"] == "from-stdin"

    def test_rm_requires_confirmation(self, ssm):
        result = runner.invoke(app, ["secrets", "--prefix", "/myapp/", "rm", "API_KEY"], input="n\n")
        assert result.exit_code != 0
        assert "/myapp/API_KEY" in ssm.parameters

    def test_rm_with_yes(self, ssm):
        result = self.invoke("rm", "API_KEY", "--yes")
        assert result.exit_code == 0
        assert "/myapp/API_KEY" not in ssm.parameters

    def test_export_emits_key_value_lines(self, ssm, monkeypatch):
        monkeypatch.delenv("API_KEY", raising=False)
        monkeypatch.delenv("OTHER", raising=False)
        result = self.invoke("export")
        assert "API_KEY=sekrit" in result.stdout


class TestSecretsImport:
    def write(self, tmp_path, text):
        path = tmp_path / ".secrets"
        path.write_text(text)
        return path

    def test_import_skips_aws_bootstrap_keys(self, ssm, tmp_path):
        path = self.write(
            tmp_path,
            "# comment\nAPI_KEY=abc\nAWS_ACCESS_KEY_ID=AKIAnope\nAWS_PROFILE=whatever\nEMPTY=\nQUOTED='q'\n",
        )
        result = runner.invoke(app, ["secrets", "--prefix", "/myapp/", "import", str(path)])
        assert result.exit_code == 0
        # Credentials needed to *reach* the store must never be pushed into it.
        assert "/myapp/AWS_ACCESS_KEY_ID" not in ssm.parameters
        assert "/myapp/AWS_PROFILE" not in ssm.parameters
        assert ssm.parameters["/myapp/API_KEY"] == "abc"
        assert ssm.parameters["/myapp/QUOTED"] == "q"
        assert "/myapp/EMPTY" not in ssm.parameters

    def test_dry_run_writes_nothing(self, ssm, tmp_path):
        path = self.write(tmp_path, "BRAND_NEW=1\n")
        result = runner.invoke(app, ["secrets", "--prefix", "/myapp/", "import", str(path), "--dry-run"])
        assert result.exit_code == 0
        assert "would import" in result.stdout
        assert "/myapp/BRAND_NEW" not in ssm.parameters

    def test_missing_file_exits_nonzero(self, ssm, tmp_path):
        result = runner.invoke(app, ["secrets", "--prefix", "/myapp/", "import", str(tmp_path / "absent")])
        assert result.exit_code == 1
        assert "no such file" in result.stdout


class TestQuotasCommands:
    def test_status_classifies_each_target(self, quotas):
        result = runner.invoke(app, ["quotas", "status", "--region", "us-east-1"])
        assert result.exit_code == 0
        assert "satisfied" in result.stdout
        assert "open" in result.stdout
        assert "to-submit" in result.stdout
        assert "1 open, 1 to submit" in result.stdout

    def test_history_shows_status_and_case(self, quotas):
        result = runner.invoke(app, ["quotas", "history", "--region", "us-east-1"])
        assert result.exit_code == 0
        assert "CASE_OPENED" in result.stdout
        assert "case-L-BBB" in result.stdout

    def test_history_open_filter(self, quotas):
        result = runner.invoke(app, ["quotas", "history", "--open"])
        assert result.exit_code == 0
        assert "1 CASE_OPENED" in result.stdout

    def test_request_submits_only_the_gap(self, quotas):
        result = runner.invoke(app, ["quotas", "request", "--region", "us-east-1"])
        assert result.exit_code == 0
        assert quotas.requested == [("L-CCC", 100.0)]
        assert "1 submitted, 2 skipped" in result.stdout

    def test_request_dry_run_writes_nothing(self, quotas):
        result = runner.invoke(app, ["quotas", "request", "--dry-run"])
        assert result.exit_code == 0
        assert quotas.requested == []

    def test_request_reports_the_account_cap(self, monkeypatch):
        targets = [quotas_lib.QuotaTarget(f"q{i}", f"L-{i:03d}", 100) for i in range(4)]
        fake = FakeQuotasClient(applied={t.code: 0 for t in targets}, cap_after=1)
        monkeypatch.setattr("pdum.aws.quotas.client", lambda *a, **k: fake)
        monkeypatch.setattr(quotas_lib, "EC2_VCPU_TARGETS", targets)

        result = runner.invoke(app, ["quotas", "request"])
        assert result.exit_code == 0
        assert "open-request cap" in result.stdout
        assert len(fake.requested) == 1

    def test_targets_emits_reusable_json(self):
        result = runner.invoke(app, ["quotas", "targets"])
        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        assert {"label", "code", "value"} <= set(payload[0])

    def test_targets_file_round_trips(self, quotas, tmp_path):
        custom = tmp_path / "targets.json"
        custom.write_text(json.dumps([{"label": "custom", "code": "L-CCC", "value": 100}]))
        result = runner.invoke(app, ["quotas", "request", "--targets", str(custom)])
        assert result.exit_code == 0
        assert quotas.requested == [("L-CCC", 100.0)]
