"""Tests for mounting these command groups inside someone else's CLI.

Each test builds a small host application the way a downstream project would,
then drives it through :class:`~typer.testing.CliRunner`. Output is captured by
giving the host its own :class:`~rich.console.Console` over a buffer, which is
also the assertion that the ``console=`` argument is honoured: anything landing
in the buffer went through the host's console rather than a module-level one.
"""

import io
import re

import pytest
import typer
from rich.console import Console
from typer.testing import CliRunner

from pdum.aws import quotas as quotas_lib
from pdum.aws.cli import add_whoami, aws_errors, build_quotas_app, build_secrets_app
from pdum.aws.cli.secrets import store_from
from pdum.aws.secrets import SecretStore
from tests.fakes import FakeQuotasClient, FakeSsmClient, client_error

runner = CliRunner()

_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def plain(text: str) -> str:
    """Strip ANSI styling; see the note in :mod:`tests.test_cli`."""
    return _ANSI.sub("", text)


@pytest.fixture
def host():
    """A console the host application owns, plus the buffer it writes to."""
    buffer = io.StringIO()
    return Console(file=buffer, width=200), buffer


@pytest.fixture
def ssm(monkeypatch):
    """Install an in-memory SSM client; returns a callable that seeds it."""
    monkeypatch.delenv("PDUM_SSM_PREFIX", raising=False)
    monkeypatch.delenv("TOKEN", raising=False)

    def install(parameters: dict[str, str]) -> FakeSsmClient:
        fake = FakeSsmClient(parameters)
        monkeypatch.setattr("pdum.aws.secrets.client", lambda *a, **k: fake)
        return fake

    return install


def host_app(group: typer.Typer, name: str = "secrets") -> typer.Typer:
    """Wrap *group* in a host application, as a downstream project would."""
    app = typer.Typer()
    app.add_typer(group, name=name)
    return app


class TestSecretsPrefixDefaulting:
    def test_a_host_default_makes_the_flag_optional(self, host, ssm):
        console, buffer = host
        ssm({"/acme/TOKEN": "acme-value"})
        app = host_app(build_secrets_app(console=console, default_prefix="/acme/"))

        result = runner.invoke(app, ["secrets", "get", "TOKEN"])
        assert result.exit_code == 0
        assert plain(buffer.getvalue()).strip() == "acme-value"

    def test_flag_beats_environment_beats_host_default(self, host, ssm, monkeypatch):
        console, buffer = host
        ssm({"/flag/TOKEN": "from-flag", "/env/TOKEN": "from-env", "/host/TOKEN": "from-host"})
        app = host_app(build_secrets_app(console=console, default_prefix="/host/"))

        assert runner.invoke(app, ["secrets", "get", "TOKEN"]).exit_code == 0
        monkeypatch.setenv("PDUM_SSM_PREFIX", "/env/")
        assert runner.invoke(app, ["secrets", "get", "TOKEN"]).exit_code == 0
        assert runner.invoke(app, ["secrets", "--prefix", "/flag/", "get", "TOKEN"]).exit_code == 0

        assert plain(buffer.getvalue()).split() == ["from-host", "from-env", "from-flag"]

    def test_a_host_can_name_its_own_environment_variable(self, host, ssm, monkeypatch):
        console, buffer = host
        ssm({"/acme/TOKEN": "acme-value", "/other/TOKEN": "wrong-store"})
        app = host_app(build_secrets_app(console=console, envvar="ACME_SSM_PREFIX"))

        monkeypatch.setenv("ACME_SSM_PREFIX", "/acme/")
        monkeypatch.setenv("PDUM_SSM_PREFIX", "/other/")
        result = runner.invoke(app, ["secrets", "get", "TOKEN"])

        assert result.exit_code == 0
        assert plain(buffer.getvalue()).strip() == "acme-value"

    def test_environment_lookup_can_be_disabled(self, host, ssm, monkeypatch):
        console, _ = host
        ssm({"/env/TOKEN": "from-env"})
        app = host_app(build_secrets_app(console=console, envvar=None, default_prefix="/acme/"))

        monkeypatch.setenv("PDUM_SSM_PREFIX", "/env/")
        result = runner.invoke(app, ["secrets", "get", "TOKEN"])

        # Resolved to /acme/, where the secret does not exist, rather than /env/.
        assert result.exit_code == 1

    def test_a_pinned_prefix_removes_the_flag(self, host, ssm):
        console, buffer = host
        ssm({"/acme/TOKEN": "acme-value"})
        app = host_app(build_secrets_app(console=console, default_prefix="/acme/", expose_prefix_option=False))

        assert runner.invoke(app, ["secrets", "get", "TOKEN"]).exit_code == 0
        assert plain(buffer.getvalue()).strip() == "acme-value"

        assert "--prefix" not in plain(runner.invoke(app, ["secrets", "--help"]).stdout)
        assert runner.invoke(app, ["secrets", "--prefix", "/other/", "get", "TOKEN"]).exit_code == 2

    def test_a_callable_default_is_resolved_per_invocation(self, host, ssm):
        console, _ = host
        ssm({"/acme/TOKEN": "acme-value", "/flag/TOKEN": "flag-value"})
        calls: list[int] = []

        def read_config() -> str:
            calls.append(1)
            return "/acme/"

        app = host_app(build_secrets_app(console=console, default_prefix=read_config))
        # Deferred: a host reading its config file must not be made to do so at
        # import time, when the app is built.
        assert calls == []

        assert runner.invoke(app, ["secrets", "get", "TOKEN"]).exit_code == 0
        assert calls == [1]

        # And skipped entirely when the command line already answered.
        assert runner.invoke(app, ["secrets", "--prefix", "/flag/", "get", "TOKEN"]).exit_code == 0
        assert calls == [1]

    def test_an_unresolvable_prefix_names_the_environment_variable(self, host, ssm):
        console, buffer = host
        ssm({})
        app = host_app(build_secrets_app(console=console, envvar="ACME_SSM_PREFIX", default_prefix=lambda: None))

        result = runner.invoke(app, ["secrets", "get", "TOKEN"])

        assert result.exit_code == 2
        assert "ACME_SSM_PREFIX" in plain(buffer.getvalue())

    def test_a_root_prefix_is_still_refused(self, host, ssm):
        console, buffer = host
        ssm({})
        app = host_app(build_secrets_app(console=console))

        result = runner.invoke(app, ["secrets", "--prefix", "/", "get", "TOKEN"])

        assert result.exit_code == 2
        assert "non-root prefix" in plain(buffer.getvalue())


class TestSecretsHostIntegration:
    def test_store_factory_threads_host_configuration(self, host, ssm):
        console, buffer = host
        ssm({"/acme/TOKEN": "acme-value"})
        built: list[tuple[str, str | None]] = []

        def make_store(prefix: str) -> SecretStore:
            built.append((prefix, "eu-west-1"))
            return SecretStore(prefix, region="eu-west-1")

        app = host_app(build_secrets_app(console=console, default_prefix="/acme/", store_factory=make_store))

        assert runner.invoke(app, ["secrets", "get", "TOKEN"]).exit_code == 0
        assert built == [("/acme/", "eu-west-1")]
        assert plain(buffer.getvalue()).strip() == "acme-value"

    def test_the_host_context_object_survives(self, host, ssm):
        """The group must not shadow what the host put in ``ctx.obj``.

        This is why the store lives in ``ctx.meta`` under a namespaced key: a
        host whose own callback stashes configuration in ``ctx.obj`` still finds
        it there inside these commands, and can add commands of its own that use
        both it and the resolved store.
        """
        console, buffer = host
        ssm({"/acme/TOKEN": "acme-value"})
        group = build_secrets_app(console=console, default_prefix="/acme/")

        @group.command("whose")
        def whose(ctx: typer.Context) -> None:
            console.print(f"obj={ctx.obj} store={store_from(ctx).prefix}")

        app = typer.Typer()

        @app.callback()
        def root(ctx: typer.Context) -> None:
            ctx.obj = "host-configuration"

        app.add_typer(group, name="secrets")

        result = runner.invoke(app, ["secrets", "whose"])

        assert result.exit_code == 0
        assert "obj=host-configuration store=/acme/" in plain(buffer.getvalue())

    def test_the_group_can_be_mounted_under_any_name(self, host, ssm):
        console, buffer = host
        ssm({"/acme/TOKEN": "acme-value"})
        app = host_app(build_secrets_app(console=console, default_prefix="/acme/"), name="config")

        result = runner.invoke(app, ["config", "list"])

        assert result.exit_code == 0
        assert plain(buffer.getvalue()).strip() == "TOKEN"

    def test_two_groups_can_coexist_with_different_prefixes(self, host, ssm):
        """Nothing is shared between builds, so one app can serve two namespaces."""
        console, buffer = host
        ssm({"/acme/TOKEN": "acme-value", "/beta/TOKEN": "beta-value"})
        app = typer.Typer()
        app.add_typer(build_secrets_app(console=console, default_prefix="/acme/"), name="acme")
        app.add_typer(build_secrets_app(console=console, default_prefix="/beta/"), name="beta")

        assert runner.invoke(app, ["acme", "get", "TOKEN"]).exit_code == 0
        assert runner.invoke(app, ["beta", "get", "TOKEN"]).exit_code == 0
        assert plain(buffer.getvalue()).split() == ["acme-value", "beta-value"]


class RecordingQuotasClient(FakeQuotasClient):
    """A quotas fake that also records the service code it was asked about."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.services: list[str] = []

    def get_service_quota(self, ServiceCode: str, QuotaCode: str):  # noqa: N803
        self.services.append(ServiceCode)
        return super().get_service_quota(ServiceCode=ServiceCode, QuotaCode=QuotaCode)


@pytest.fixture
def quotas(monkeypatch):
    """An in-memory Service Quotas client, plus the regions it was built for."""
    fake = RecordingQuotasClient(applied={"L-HOST": 5, "L-BUNDLED": 5})
    regions: list[str | None] = []

    def fake_client(_name: str, region: str | None = None):
        regions.append(region)
        return fake

    monkeypatch.setattr("pdum.aws.quotas.client", fake_client)
    monkeypatch.setattr(quotas_lib, "EC2_VCPU_TARGETS", [quotas_lib.QuotaTarget("bundled", "L-BUNDLED", 100)])
    return fake, regions


HOST_TARGETS = [quotas_lib.QuotaTarget("host plan", "L-HOST", 100)]


class TestQuotasEmbedding:
    def test_host_targets_replace_the_bundled_plan(self, host, quotas):
        console, buffer = host
        app = host_app(build_quotas_app(console=console, default_targets=HOST_TARGETS), name="quotas")

        result = runner.invoke(app, ["quotas", "status"])

        assert result.exit_code == 0
        assert "host plan" in plain(buffer.getvalue())
        assert "bundled" not in plain(buffer.getvalue())

    def test_callable_targets_are_resolved_per_invocation(self, host, quotas):
        console, buffer = host
        calls: list[int] = []

        def read_plan() -> list[quotas_lib.QuotaTarget]:
            calls.append(1)
            return HOST_TARGETS

        app = host_app(build_quotas_app(console=console, default_targets=read_plan), name="quotas")
        assert calls == []

        assert runner.invoke(app, ["quotas", "status"]).exit_code == 0
        assert calls == [1]
        assert "host plan" in plain(buffer.getvalue())

    def test_the_targets_command_prints_the_host_plan(self, host, quotas):
        console, buffer = host
        app = host_app(build_quotas_app(console=console, default_targets=HOST_TARGETS), name="quotas")

        result = runner.invoke(app, ["quotas", "targets"])

        assert result.exit_code == 0
        assert '"code": "L-HOST"' in plain(buffer.getvalue())

    def test_host_regions_are_acted_on_in_turn(self, host, quotas):
        console, buffer = host
        _, regions = quotas
        app = host_app(
            build_quotas_app(console=console, default_targets=HOST_TARGETS, default_regions=["us-east-1", "eu-west-1"]),
            name="quotas",
        )

        result = runner.invoke(app, ["quotas", "status"])

        assert result.exit_code == 0
        assert set(regions) == {"us-east-1", "eu-west-1"}
        output = plain(buffer.getvalue())
        assert "us-east-1" in output and "eu-west-1" in output

    def test_the_region_flag_overrides_the_host_regions(self, host, quotas):
        console, _ = host
        fake, regions = quotas
        app = host_app(
            build_quotas_app(console=console, default_targets=HOST_TARGETS, default_regions=["us-east-1"]),
            name="quotas",
        )

        result = runner.invoke(app, ["quotas", "status", "--region", "ap-south-1"])

        assert result.exit_code == 0
        assert set(regions) == {"ap-south-1"}

    def test_a_host_service_code_is_used(self, host, quotas):
        console, _ = host
        fake, _regions = quotas
        app = host_app(
            build_quotas_app(console=console, default_service="rds", default_targets=HOST_TARGETS), name="quotas"
        )

        assert runner.invoke(app, ["quotas", "status"]).exit_code == 0
        assert fake.services == ["rds"]

    def test_hidden_options_stay_usable(self, host, quotas):
        console, _ = host
        fake, _regions = quotas
        app = host_app(
            build_quotas_app(
                console=console,
                default_service="rds",
                default_targets=HOST_TARGETS,
                show_service_option=False,
                show_targets_option=False,
            ),
            name="quotas",
        )

        help_text = plain(runner.invoke(app, ["quotas", "status", "--help"]).stdout)
        assert "--service" not in help_text
        assert "--targets" not in help_text
        assert "--region" in help_text

        assert runner.invoke(app, ["quotas", "status", "--service", "ec2"]).exit_code == 0
        assert fake.services == ["ec2"]

    def test_requests_go_through_the_host_console(self, host, quotas):
        console, buffer = host
        fake, _regions = quotas
        app = host_app(build_quotas_app(console=console, default_targets=HOST_TARGETS), name="quotas")

        result = runner.invoke(app, ["quotas", "request"])

        assert result.exit_code == 0
        assert fake.requested == [("L-HOST", 100.0)]
        assert "1 submitted, 0 skipped" in plain(buffer.getvalue())
        assert result.stdout == ""


class TestHostEntryPoint:
    def test_aws_errors_reports_one_line_and_exits_two(self, host):
        console, buffer = host

        with pytest.raises(SystemExit) as caught:
            with aws_errors(console):
                raise client_error("ExpiredTokenException", "GetCallerIdentity", "the security token has expired")

        assert caught.value.code == 2
        assert "the security token has expired" in plain(buffer.getvalue())
        assert "Traceback" not in plain(buffer.getvalue())

    def test_aws_errors_passes_other_exceptions_through(self, host):
        console, _ = host

        with pytest.raises(ZeroDivisionError):
            with aws_errors(console):
                raise ZeroDivisionError

    def test_whoami_attaches_under_a_host_chosen_name(self, host, monkeypatch):
        console, buffer = host
        monkeypatch.setattr(
            "pdum.aws.whoami",
            lambda *a, **k: {"Account": "123456789012", "Arn": "arn:aws:iam::123456789012:root"},
        )
        app = typer.Typer()
        add_whoami(app, console=console, name="identity")

        @app.command()
        def version() -> None:
            """A command of the host's own, so Typer keeps a multi-command app."""

        result = runner.invoke(app, ["identity"])

        assert result.exit_code == 0
        assert "123456789012" in plain(buffer.getvalue())
