"""Tests for ``pdx`` and ``pdx-doctor``.

Most run against in-memory AWS clients with :func:`os.execvp` stubbed out, so
the environment handed to the command can be inspected without this process
actually being replaced. One test at the end does the real thing in a
subprocess, since a stubbed ``execvp`` proves nothing about whether the exec
itself works.
"""

import os
import re
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from pdum.aws.cli import add_pdx, add_pdx_doctor
from pdum.aws.cli.pdx import EXIT_NOT_EXECUTABLE, EXIT_NOT_FOUND
from pdum.aws.env import load_env_files, load_secrets, resolve_search_path
from pdum.aws.secrets import SecretStore
from tests.fakes import FakeSsmClient

runner = CliRunner()

_ANSI = re.compile(r"\x1b\[[0-9;]*m")

SECRETS = {"/myapp/API_KEY": "from-myapp", "/myapp/SHARED": "myapp-wins", "/org/SHARED": "org-loses"}


def plain(text: str) -> str:
    """Strip ANSI styling; see the note in :mod:`tests.test_cli`."""
    return _ANSI.sub("", text)


@pytest.fixture(autouse=True)
def _clean_environment(monkeypatch, tmp_path):
    """An isolated environment and a directory with no ``.env`` above it.

    The whole environment is snapshotted and restored, not just a list of
    names: the code under test writes into ``os.environ`` directly, which
    ``monkeypatch`` cannot undo, so a ``.env`` loaded by one test would
    otherwise still be set for the next one.

    The working directory matters too. ``pdx`` searches *upward* for ``.env``,
    so a test run from inside this repository would find the repository's own.
    """
    saved = dict(os.environ)
    monkeypatch.delenv("PDUM_SSM_PATH", raising=False)
    for name in ("API_KEY", "SHARED", "OTHER", "AWS_PROFILE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)
    yield
    os.environ.clear()
    os.environ.update(saved)


@pytest.fixture
def ssm(monkeypatch):
    """Install an in-memory SSM client holding :data:`SECRETS`."""
    fake = FakeSsmClient(SECRETS)
    monkeypatch.setattr("pdum.aws.secrets.client", lambda *a, **k: fake)
    return fake


@pytest.fixture
def execs(monkeypatch):
    """Capture what would have been exec'd, instead of replacing this process."""
    calls: list[tuple[str, list[str], dict[str, str]]] = []

    def fake_execvp(file: str, args: list[str]) -> None:
        calls.append((file, list(args), dict(os.environ)))
        raise SystemExit(0)

    monkeypatch.setattr(os, "execvp", fake_execvp)
    return calls


def pdx_app(**kwargs) -> typer.Typer:
    """A standalone one-command app, as the ``pdx`` console script builds."""
    app = typer.Typer()
    add_pdx(app, **kwargs)
    return app


def write_env(tmp_path, body: str, name: str = ".env"):
    path = tmp_path / name
    path.write_text(textwrap.dedent(body))
    return path


class TestPassthrough:
    """pdx has no options of its own, so nothing can be mistaken for one."""

    def test_the_whole_tail_is_the_command(self, ssm, execs, monkeypatch):
        monkeypatch.setenv("PDUM_SSM_PATH", "/myapp/")
        result = runner.invoke(pdx_app(), ["npm", "run", "build", "--watch"])

        assert result.exit_code == 0
        file, argv, _env = execs[0]
        assert file == "npm"
        assert argv == ["npm", "run", "build", "--watch"]

    @pytest.mark.parametrize(
        "args",
        [
            ["python", "-c", "print(1)"],
            ["ls", "-la"],
            ["--", "ls", "-la"],
            ["prog", "--path", "/not/pdx/business/"],
            ["prog", "--verbose", "--dry-run"],
            ["prog", "--help"],
        ],
    )
    def test_flags_belonging_to_the_command_are_never_parsed(self, ssm, execs, monkeypatch, args):
        monkeypatch.setenv("PDUM_SSM_PATH", "/myapp/")
        result = runner.invoke(pdx_app(), args)

        assert result.exit_code == 0
        expected = [a for a in args if a != "--"]
        assert execs[0][1] == expected

    def test_a_leading_help_is_still_reserved(self, ssm, execs):
        result = runner.invoke(pdx_app(), ["--help"])

        assert result.exit_code == 0
        assert "COMMAND" in plain(result.stdout)
        assert execs == []

    def test_no_command_is_a_usage_error(self, ssm, execs):
        result = runner.invoke(pdx_app(), [])

        assert result.exit_code == 2
        assert execs == []


class TestEnvironmentLoading:
    def test_secrets_reach_the_command(self, ssm, execs, monkeypatch):
        monkeypatch.setenv("PDUM_SSM_PATH", "/myapp/")
        assert runner.invoke(pdx_app(), ["prog"]).exit_code == 0

        _file, _argv, env = execs[0]
        assert env["API_KEY"] == "from-myapp"

    def test_the_environment_wins_over_the_store(self, ssm, execs, monkeypatch):
        """A variable exported for one run must not be undone by SSM."""
        monkeypatch.setenv("PDUM_SSM_PATH", "/myapp/")
        monkeypatch.setenv("API_KEY", "from-the-shell")
        assert runner.invoke(pdx_app(), ["prog"]).exit_code == 0

        assert execs[0][2]["API_KEY"] == "from-the-shell"

    def test_an_empty_variable_counts_as_unset(self, ssm, execs, monkeypatch):
        monkeypatch.setenv("PDUM_SSM_PATH", "/myapp/")
        monkeypatch.setenv("API_KEY", "")
        assert runner.invoke(pdx_app(), ["prog"]).exit_code == 0

        assert execs[0][2]["API_KEY"] == "from-myapp"

    def test_the_first_layer_wins_across_the_search_path(self, ssm, execs, monkeypatch):
        monkeypatch.setenv("PDUM_SSM_PATH", "/myapp/:/org/")
        assert runner.invoke(pdx_app(), ["prog"]).exit_code == 0

        assert execs[0][2]["SHARED"] == "myapp-wins"

    def test_names_unusable_as_variables_are_skipped(self, monkeypatch, execs):
        fake = FakeSsmClient({"/myapp/OK": "yes", "/myapp/db/PASSWORD": "nested"})
        monkeypatch.setattr("pdum.aws.secrets.client", lambda *a, **k: fake)
        monkeypatch.setenv("PDUM_SSM_PATH", "/myapp/")

        assert runner.invoke(pdx_app(), ["prog"]).exit_code == 0
        env = execs[0][2]
        assert env["OK"] == "yes"
        assert "db/PASSWORD" not in env

    def test_load_secrets_reports_each_outcome(self, ssm):
        environ = {"SHARED": "already-here"}
        report = load_secrets(SecretStore("/myapp/:/org/"), environ)

        assert report.applied == ["API_KEY"]
        assert report.already_set == ["SHARED"]
        assert report.skipped == []
        assert report.source == "/myapp/:/org/"
        assert environ["SHARED"] == "already-here"

    def test_the_whole_store_is_read_in_one_pass(self, ssm, execs, monkeypatch):
        """One paginated call per layer, not one API call per secret."""
        calls: list[str] = []
        original = ssm.get_parameter
        ssm.get_parameter = lambda **kw: (calls.append(kw["Name"]), original(**kw))[1]
        monkeypatch.setenv("PDUM_SSM_PATH", "/myapp/")

        assert runner.invoke(pdx_app(), ["prog"]).exit_code == 0
        assert calls == []


class TestEnvFileLoading:
    """The whole file is the project's environment, not just the SSM path."""

    def test_every_variable_in_the_file_reaches_the_command(self, ssm, execs, tmp_path):
        write_env(
            tmp_path,
            """
            PDUM_SSM_PATH=/myapp/
            AWS_PROFILE=project-account
            AWS_REGION=eu-west-1
            FEATURE_FLAG=on
            """,
        )

        assert runner.invoke(pdx_app(), ["prog"]).exit_code == 0
        env = execs[0][2]
        assert env["AWS_PROFILE"] == "project-account"
        assert env["AWS_REGION"] == "eu-west-1"
        assert env["FEATURE_FLAG"] == "on"

    def test_the_file_is_applied_before_the_store_is_opened(self, execs, monkeypatch, tmp_path):
        """Without this ordering the store is unreachable, or reads the wrong account.

        ``AWS_PROFILE`` lives in the project's ``.env`` next to the search path,
        so it has to be in the environment by the time the SSM client is built.
        """
        profiles: list[str | None] = []
        fake = FakeSsmClient(SECRETS)

        def client(*a, **k):
            profiles.append(os.environ.get("AWS_PROFILE"))
            return fake

        monkeypatch.setattr("pdum.aws.secrets.client", client)
        write_env(tmp_path, "AWS_PROFILE=project-account\nPDUM_SSM_PATH=/myapp/\n")

        assert runner.invoke(pdx_app(), ["prog"]).exit_code == 0
        assert profiles == ["project-account"]
        assert execs[0][2]["API_KEY"] == "from-myapp"

    def test_the_environment_beats_the_file(self, ssm, execs, tmp_path, monkeypatch):
        write_env(tmp_path, "PDUM_SSM_PATH=/myapp/\nAWS_PROFILE=from-file\n")
        monkeypatch.setenv("AWS_PROFILE", "from-the-shell")

        assert runner.invoke(pdx_app(), ["prog"]).exit_code == 0
        assert execs[0][2]["AWS_PROFILE"] == "from-the-shell"

    def test_the_file_beats_the_store(self, ssm, execs, tmp_path):
        """Layered most specific first, so a local override sticks."""
        write_env(tmp_path, "PDUM_SSM_PATH=/myapp/\nAPI_KEY=overridden-locally\n")

        assert runner.invoke(pdx_app(), ["prog"]).exit_code == 0
        assert execs[0][2]["API_KEY"] == "overridden-locally"

    def test_values_are_interpolated_as_any_env_consumer_would(self, ssm, execs, tmp_path, monkeypatch):
        monkeypatch.setenv("STAGE", "dev")
        write_env(tmp_path, "PDUM_SSM_PATH=/myapp/\nBUCKET=assets-${STAGE}\n")

        assert runner.invoke(pdx_app(), ["prog"]).exit_code == 0
        assert execs[0][2]["BUCKET"] == "assets-dev"

    def test_load_env_files_reports_what_it_did(self, tmp_path):
        write_env(tmp_path, "A=1\nB=2\n")
        environ = {"B": "already-here"}

        report = load_env_files(".env", environ)

        assert report.applied == ["A"]
        assert report.already_set == ["B"]
        assert report.source.endswith(".env")
        assert environ == {"A": "1", "B": "already-here"}

    def test_load_env_files_is_none_when_there_is_no_file(self, tmp_path):
        assert load_env_files(".env", {}) is None


class TestLocalOverlay:
    """``.env.local`` overlays the ``.env`` it sits beside."""

    def test_local_wins_over_the_committed_file(self, ssm, execs, tmp_path):
        write_env(tmp_path, "PDUM_SSM_PATH=/myapp/\nAWS_PROFILE=team-account\nSTAGE=prod\n")
        write_env(tmp_path, "AWS_PROFILE=my-sandbox\n", name=".env.local")

        assert runner.invoke(pdx_app(), ["prog"]).exit_code == 0
        env = execs[0][2]
        assert env["AWS_PROFILE"] == "my-sandbox"
        assert env["STAGE"] == "prod"  # untouched by the overlay

    def test_local_can_redirect_the_search_path(self, ssm, execs, tmp_path):
        write_env(tmp_path, "PDUM_SSM_PATH=/decoy/\n")
        write_env(tmp_path, "PDUM_SSM_PATH=/myapp/\n", name=".env.local")

        assert runner.invoke(pdx_app(), ["prog"]).exit_code == 0
        assert execs[0][2]["API_KEY"] == "from-myapp"

    def test_interpolation_reaches_across_the_two_files(self, ssm, execs, tmp_path):
        """Why they are parsed as one document rather than merged as two dicts."""
        write_env(tmp_path, "PDUM_SSM_PATH=/myapp/\nSTAGE=prod\n")
        write_env(tmp_path, "BUCKET=assets-${STAGE}\n", name=".env.local")

        assert runner.invoke(pdx_app(), ["prog"]).exit_code == 0
        assert execs[0][2]["BUCKET"] == "assets-prod"

    def test_the_shell_still_beats_both(self, ssm, execs, tmp_path, monkeypatch):
        write_env(tmp_path, "PDUM_SSM_PATH=/myapp/\nAWS_PROFILE=team-account\n")
        write_env(tmp_path, "AWS_PROFILE=my-sandbox\n", name=".env.local")
        monkeypatch.setenv("AWS_PROFILE", "from-the-shell")

        assert runner.invoke(pdx_app(), ["prog"]).exit_code == 0
        assert execs[0][2]["AWS_PROFILE"] == "from-the-shell"

    def test_the_overlay_is_taken_from_beside_the_file_that_was_found(self, ssm, execs, tmp_path, monkeypatch):
        """The search is for ``.env`` alone, so one directory always wins."""
        write_env(tmp_path, "PDUM_SSM_PATH=/myapp/\nWHICH=root\n")
        write_env(tmp_path, "WHICH=root-local\n", name=".env.local")
        nested = tmp_path / "services" / "api"
        nested.mkdir(parents=True)
        # A stray local file down here has no .env to anchor it, so it is ignored.
        write_env(nested, "WHICH=nested-local\n", name=".env.local")
        monkeypatch.chdir(nested)

        assert runner.invoke(pdx_app(), ["prog"]).exit_code == 0
        assert execs[0][2]["WHICH"] == "root-local"

    def test_a_lone_local_file_is_not_used(self, ssm, execs, tmp_path):
        write_env(tmp_path, "PDUM_SSM_PATH=/myapp/\n", name=".env.local")

        result = runner.invoke(pdx_app(), ["prog"])

        # Nothing anchored the search, so no path was found.
        assert result.exit_code == 2
        assert execs == []

    def test_the_report_names_both_files(self, tmp_path):
        write_env(tmp_path, "A=1\n")
        write_env(tmp_path, "B=2\n", name=".env.local")

        report = load_env_files(".env", {})

        assert report.applied == ["A", "B"]
        assert report.source.endswith(".env + .env.local")

    def test_a_host_naming_its_own_file_gets_the_matching_overlay(self, ssm, execs, tmp_path):
        write_env(tmp_path, "ACME_SSM_PATH=/decoy/\n", name="acme.env")
        write_env(tmp_path, "ACME_SSM_PATH=/myapp/\n", name="acme.env.local")

        app = pdx_app(envvar="ACME_SSM_PATH", env_file="acme.env")

        assert runner.invoke(app, ["prog"]).exit_code == 0
        assert execs[0][2]["API_KEY"] == "from-myapp"


class TestSearchPathResolution:
    def test_the_environment_is_used_first(self, ssm, execs, monkeypatch, tmp_path):
        write_env(tmp_path, "PDUM_SSM_PATH=/from-file/\n")
        monkeypatch.setenv("PDUM_SSM_PATH", "/myapp/")

        assert runner.invoke(pdx_app(), ["prog"]).exit_code == 0
        assert execs[0][2]["API_KEY"] == "from-myapp"

    def test_the_env_file_is_the_fallback(self, ssm, execs, tmp_path):
        write_env(tmp_path, "PDUM_SSM_PATH=/myapp/\n")

        assert runner.invoke(pdx_app(), ["prog"]).exit_code == 0
        assert execs[0][2]["API_KEY"] == "from-myapp"

    def test_the_env_file_is_found_from_a_subdirectory(self, ssm, execs, tmp_path, monkeypatch):
        write_env(tmp_path, "PDUM_SSM_PATH=/myapp/\n")
        nested = tmp_path / "services" / "api"
        nested.mkdir(parents=True)
        monkeypatch.chdir(nested)

        assert runner.invoke(pdx_app(), ["prog"]).exit_code == 0
        assert execs[0][2]["API_KEY"] == "from-myapp"

    def test_an_inline_comment_is_not_part_of_the_path(self, ssm, execs, tmp_path):
        """The reason this defers to python-dotenv rather than splitting on '='."""
        write_env(tmp_path, "PDUM_SSM_PATH=/myapp/   # the project's own layer\n")

        assert runner.invoke(pdx_app(), ["prog"]).exit_code == 0
        assert execs[0][2]["API_KEY"] == "from-myapp"

    def test_an_exported_line_is_understood(self, ssm, execs, tmp_path):
        write_env(tmp_path, "export PDUM_SSM_PATH='/myapp/'\n")

        assert runner.invoke(pdx_app(), ["prog"]).exit_code == 0
        assert execs[0][2]["API_KEY"] == "from-myapp"

    def test_a_host_default_is_the_last_resort(self, ssm, execs):
        assert runner.invoke(pdx_app(default_path="/myapp/"), ["prog"]).exit_code == 0
        assert execs[0][2]["API_KEY"] == "from-myapp"

    def test_a_host_names_its_own_variable_and_file(self, ssm, execs, tmp_path, monkeypatch):
        write_env(tmp_path, "ACME_SSM_PATH=/myapp/\n", name="acme.env")
        monkeypatch.setenv("PDUM_SSM_PATH", "/ignored/")

        app = pdx_app(envvar="ACME_SSM_PATH", env_file="acme.env")
        assert runner.invoke(app, ["prog"]).exit_code == 0
        assert execs[0][2]["API_KEY"] == "from-myapp"

    def test_an_env_file_with_a_directory_is_taken_literally(self, ssm, execs, tmp_path):
        """An explicit path is never second-guessed by the upward search."""
        elsewhere = tmp_path / "config"
        elsewhere.mkdir()
        write_env(elsewhere, "PDUM_SSM_PATH=/myapp/\n")
        # A nearer .env that the upward search would have found first.
        write_env(tmp_path, "PDUM_SSM_PATH=/decoy/\n")

        assert runner.invoke(pdx_app(env_file="config/.env"), ["prog"]).exit_code == 0
        assert execs[0][2]["API_KEY"] == "from-myapp"

    def test_an_env_file_that_is_not_there_is_not_fatal_by_itself(self, ssm, execs, tmp_path):
        write_env(tmp_path, "PDUM_SSM_PATH=/decoy/\n")

        result = runner.invoke(pdx_app(env_file="config/absent.env"), ["prog"])

        # No file, so nothing supplied the path — not even the decoy, which the
        # upward search would have found had the name been a bare one.
        assert result.exit_code == 2
        assert execs == []

    def test_no_path_anywhere_explains_the_options(self, ssm, execs):
        result = runner.invoke(pdx_app(), ["prog"])

        assert result.exit_code == 2
        assert execs == []
        assert "PDUM_SSM_PATH" in plain(result.output)

    def test_a_root_path_is_refused(self, ssm, execs, monkeypatch):
        monkeypatch.setenv("PDUM_SSM_PATH", "/")
        result = runner.invoke(pdx_app(), ["prog"])

        assert result.exit_code == 2
        assert execs == []

    def test_resolution_reports_its_source(self, tmp_path):
        kwargs = {"envvar": "PDUM_SSM_PATH", "default_path": "/fallback/"}
        write_env(tmp_path, "PDUM_SSM_PATH=/from-file/\n")

        already_exported = {"PDUM_SSM_PATH": "/from-env/"}
        assert resolve_search_path(**kwargs, environ=already_exported) == ("/from-env/", "$PDUM_SSM_PATH")

        # After the file has been folded in, the path is one variable among the
        # rest; only the report distinguishes where it came from.
        environ: dict[str, str] = {}
        report = load_env_files(".env", environ)
        path, source = resolve_search_path(**kwargs, environ=environ, env_file_report=report)
        assert (path, source.endswith(".env")) == ("/from-file/", True)

        assert resolve_search_path(**kwargs, environ={}) == ("/fallback/", "built-in default")
        assert resolve_search_path(**kwargs | {"default_path": None}, environ={}) == (None, "")


class TestExecFailures:
    def test_a_missing_command_exits_127(self, ssm, monkeypatch):
        monkeypatch.setenv("PDUM_SSM_PATH", "/myapp/")
        result = runner.invoke(pdx_app(), ["definitely-not-a-real-program-9273"])

        assert result.exit_code == EXIT_NOT_FOUND
        assert "command not found" in plain(result.output)

    def test_an_unrunnable_command_exits_126(self, ssm, monkeypatch, tmp_path):
        not_executable = tmp_path / "script.sh"
        not_executable.write_text("#!/bin/sh\necho hi\n")
        monkeypatch.setenv("PDUM_SSM_PATH", "/myapp/")

        result = runner.invoke(pdx_app(), [str(not_executable)])

        assert result.exit_code == EXIT_NOT_EXECUTABLE


class TestDoctor:
    def doctor_app(self, **kwargs) -> typer.Typer:
        app = typer.Typer()
        add_pdx_doctor(app, **kwargs)

        @app.command()
        def other() -> None:
            """Keeps the app multi-command, as a host's would be."""

        return app

    def test_it_classifies_every_secret(self, ssm, monkeypatch):
        monkeypatch.setenv("PDUM_SSM_PATH", "/myapp/:/org/")
        monkeypatch.setenv("SHARED", "already-here")

        result = runner.invoke(self.doctor_app(), ["pdx-doctor"])
        output = plain(result.stdout)

        assert result.exit_code == 0
        assert "API_KEY" in output
        assert "set it" in output
        assert "keep the environment's" in output

    def test_it_names_the_source_of_the_search_path(self, ssm, tmp_path):
        write_env(tmp_path, "PDUM_SSM_PATH=/myapp/\n")

        result = runner.invoke(self.doctor_app(), ["pdx-doctor"])

        assert result.exit_code == 0
        assert ".env" in plain(result.stdout)

    def test_it_reports_what_the_env_file_contributed(self, ssm, tmp_path, monkeypatch):
        write_env(tmp_path, "PDUM_SSM_PATH=/myapp/\nAWS_PROFILE=from-file\nAWS_REGION=eu-west-1\n")
        monkeypatch.setenv("AWS_REGION", "already-here")

        result = runner.invoke(self.doctor_app(), ["pdx-doctor"])
        output = plain(result.stdout)

        assert result.exit_code == 0
        assert "AWS_PROFILE" in output
        assert "kept from the environment" in output
        assert "AWS_REGION" in output

    def test_it_names_both_files_when_there_is_an_overlay(self, ssm, tmp_path):
        write_env(tmp_path, "PDUM_SSM_PATH=/myapp/\n")
        write_env(tmp_path, "AWS_PROFILE=my-sandbox\n", name=".env.local")

        result = runner.invoke(self.doctor_app(), ["pdx-doctor"])
        output = plain(result.stdout)

        assert result.exit_code == 0
        assert ".env + .env.local" in output
        assert "AWS_PROFILE" in output

    def test_it_says_when_there_is_no_env_file(self, ssm, monkeypatch):
        monkeypatch.setenv("PDUM_SSM_PATH", "/myapp/")

        result = runner.invoke(self.doctor_app(), ["pdx-doctor"])

        assert result.exit_code == 0
        assert "no .env found" in plain(result.stdout)

    def test_it_shows_which_layer_holds_each_secret(self, ssm, monkeypatch):
        monkeypatch.setenv("PDUM_SSM_PATH", "/myapp/:/org/")

        result = runner.invoke(self.doctor_app(), ["pdx-doctor"])
        output = plain(result.stdout)

        assert "/myapp/" in output
        # SHARED resolves in the first layer, so the shadowed /org/ row is gone.
        assert output.count("SHARED") == 1

    def test_it_never_decrypts(self, ssm, monkeypatch):
        """Values are not the doctor's business, and it must not fetch them."""
        monkeypatch.setenv("PDUM_SSM_PATH", "/myapp/")

        result = runner.invoke(self.doctor_app(), ["pdx-doctor"])

        assert result.exit_code == 0
        for value in SECRETS.values():
            assert value not in plain(result.stdout)

    def test_no_path_is_a_usage_error(self, ssm):
        result = runner.invoke(self.doctor_app(), ["pdx-doctor"])

        assert result.exit_code == 2
        assert "PDUM_SSM_PATH" in plain(result.output)

    def test_a_root_path_is_refused(self, ssm, monkeypatch):
        monkeypatch.setenv("PDUM_SSM_PATH", "/")

        result = runner.invoke(self.doctor_app(), ["pdx-doctor"])

        assert result.exit_code == 2
        assert "non-root prefix" in plain(result.output)

    def test_an_empty_store_says_so(self, monkeypatch):
        monkeypatch.setattr("pdum.aws.secrets.client", lambda *a, **k: FakeSsmClient({}))
        monkeypatch.setenv("PDUM_SSM_PATH", "/empty/")

        result = runner.invoke(self.doctor_app(), ["pdx-doctor"])

        assert result.exit_code == 0
        assert "no secrets under" in plain(result.stdout)

    def test_it_flags_names_that_cannot_be_variables(self, monkeypatch):
        monkeypatch.setattr("pdum.aws.secrets.client", lambda *a, **k: FakeSsmClient({"/myapp/db/PASSWORD": "x"}))
        monkeypatch.setenv("PDUM_SSM_PATH", "/myapp/")

        result = runner.invoke(self.doctor_app(), ["pdx-doctor"])

        assert result.exit_code == 0
        assert "skip it" in plain(result.stdout)


class TestRealExec:
    """The one test where the process really is replaced."""

    def test_exec_hands_over_pid_environment_and_exit_status(self, tmp_path):
        script = tmp_path / "wrapper.py"
        script.write_text(
            textwrap.dedent(
                """
                import os, sys
                from unittest.mock import patch
                from tests.fakes import FakeSsmClient

                print(f"wrapper-pid={os.getpid()}", file=sys.stderr)
                fake = FakeSsmClient({"/myapp/API_KEY": "from-ssm"})
                with patch("pdum.aws.secrets.client", lambda *a, **k: fake):
                    from pdum.aws.cli.pdx import main
                    main()
                """
            )
        )
        probe = tmp_path / "probe.py"
        probe.write_text(
            textwrap.dedent(
                """
                import os, sys
                print(f"pid={os.getpid()} key={os.environ.get('API_KEY')} args={sys.argv[1:]}")
                sys.exit(3)
                """
            )
        )

        env = {
            **os.environ,
            "PDUM_SSM_PATH": "/myapp/",
            # Not os.getcwd(): the autouse fixture has moved us to a tmp dir.
            "PYTHONPATH": str(Path(__file__).resolve().parent.parent),
            "AWS_DEFAULT_REGION": "us-east-1",
        }
        env.pop("API_KEY", None)
        done = subprocess.run(
            [sys.executable, str(script), sys.executable, str(probe), "--flag-of-its-own"],
            capture_output=True,
            text=True,
            env=env,
        )

        assert done.returncode == 3, done.stderr
        assert "key=from-ssm" in done.stdout
        assert "args=['--flag-of-its-own']" in done.stdout

        # The distinguishing property of exec over a subprocess: the wrapper did
        # not stay around to relay the exit status, it *became* the program.
        wrapper_pid = re.search(r"wrapper-pid=(\d+)", done.stderr).group(1)
        assert f"pid={wrapper_pid}" in done.stdout
