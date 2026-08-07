"""Tests for :func:`pdum.aws.load_env`, the script-facing entry point.

``pdx`` is one caller of this; :mod:`tests.test_cli_pdx` covers that side. Here
the function is used the way a script would use it, in place of
``dotenv.load_dotenv()``.
"""

import os
import textwrap

import pytest

from pdum.aws import load_env
from pdum.aws.env import (
    EnvReport,
    NoSearchPath,
    find_env_files,
    is_env_name,
    read_env_files,
)
from pdum.aws.secrets import SecretStore
from tests.fakes import FakeSsmClient

SECRETS = {"/myapp/API_KEY": "from-myapp", "/myapp/SHARED": "myapp-wins", "/org/SHARED": "org-loses"}


@pytest.fixture(autouse=True)
def _clean_environment(monkeypatch, tmp_path):
    """An isolated environment, and a directory with no ``.env`` above it.

    The whole environment is snapshotted: :func:`load_env` writes into
    ``os.environ`` directly by default, which ``monkeypatch`` cannot undo.
    """
    saved = dict(os.environ)
    for name in ("PDUM_SSM_PATH", "API_KEY", "SHARED", "AWS_PROFILE", "STAGE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)
    yield
    os.environ.clear()
    os.environ.update(saved)


@pytest.fixture
def ssm(monkeypatch):
    fake = FakeSsmClient(SECRETS)
    monkeypatch.setattr("pdum.aws.secrets.client", lambda *a, **k: fake)
    return fake


def write_env(tmp_path, body: str, name: str = ".env"):
    path = tmp_path / name
    path.write_text(textwrap.dedent(body))
    return path


class TestLoadEnvFromAScript:
    def test_it_replaces_load_dotenv_and_adds_the_secrets(self, ssm, tmp_path):
        """The whole point: one call, and the process environment is ready."""
        write_env(tmp_path, "PDUM_SSM_PATH=/myapp/\nAWS_PROFILE=project-account\n")

        load_env()

        assert os.environ["AWS_PROFILE"] == "project-account"
        assert os.environ["API_KEY"] == "from-myapp"

    def test_it_can_build_an_environment_without_touching_this_process(self, ssm, tmp_path):
        write_env(tmp_path, "PDUM_SSM_PATH=/myapp/\nAWS_PROFILE=project-account\n")
        environ: dict[str, str] = {}

        load_env(environ=environ)

        assert environ["API_KEY"] == "from-myapp"
        assert "API_KEY" not in os.environ

    def test_it_reports_what_each_layer_did(self, ssm, tmp_path):
        write_env(tmp_path, "PDUM_SSM_PATH=/myapp/:/org/\nAWS_PROFILE=project-account\n")
        environ = {"SHARED": "already-here"}

        report = load_env(environ=environ)

        assert isinstance(report, EnvReport)
        assert report.path == "/myapp/:/org/"
        assert report.path_source.endswith(".env")
        assert report.env_files.applied == ["AWS_PROFILE", "PDUM_SSM_PATH"]
        assert report.secrets.applied == ["API_KEY"]
        assert report.secrets.already_set == ["SHARED"]

    def test_the_search_path_can_come_from_the_environment_alone(self, ssm, monkeypatch):
        monkeypatch.setenv("PDUM_SSM_PATH", "/myapp/")

        report = load_env()

        assert report.env_files is None
        assert report.path_source == "$PDUM_SSM_PATH"
        assert os.environ["API_KEY"] == "from-myapp"

    def test_a_caller_can_supply_the_default_path(self, ssm):
        report = load_env(default_path="/myapp/")

        assert report.path_source == "built-in default"
        assert os.environ["API_KEY"] == "from-myapp"

    def test_a_callable_default_is_resolved_at_call_time(self, ssm):
        calls: list[int] = []

        def read_config() -> str:
            calls.append(1)
            return "/myapp/"

        load_env(default_path=read_config)
        assert calls == [1]

    def test_store_factory_is_the_seam_for_region(self, ssm, monkeypatch):
        monkeypatch.setenv("PDUM_SSM_PATH", "/myapp/")
        built: list[str] = []

        def make_store(path: str) -> SecretStore:
            built.append(path)
            return SecretStore(path, region="eu-west-1")

        load_env(store_factory=make_store)

        assert built == ["/myapp/"]
        assert os.environ["API_KEY"] == "from-myapp"

    def test_a_host_can_name_its_own_variable_and_file(self, ssm, tmp_path, monkeypatch):
        write_env(tmp_path, "ACME_SSM_PATH=/myapp/\n", name="acme.env")
        monkeypatch.setenv("PDUM_SSM_PATH", "/ignored/")

        load_env(envvar="ACME_SSM_PATH", env_file="acme.env")

        assert os.environ["API_KEY"] == "from-myapp"


class TestLayering:
    def test_the_caller_environment_is_never_overwritten(self, ssm, tmp_path):
        write_env(tmp_path, "PDUM_SSM_PATH=/myapp/\nAWS_PROFILE=from-file\n")
        environ = {"AWS_PROFILE": "from-the-shell", "API_KEY": "also-from-the-shell"}

        load_env(environ=environ)

        assert environ["AWS_PROFILE"] == "from-the-shell"
        assert environ["API_KEY"] == "also-from-the-shell"

    def test_the_env_file_beats_the_store(self, ssm, tmp_path):
        write_env(tmp_path, "PDUM_SSM_PATH=/myapp/\nAPI_KEY=overridden-locally\n")
        environ: dict[str, str] = {}

        load_env(environ=environ)

        assert environ["API_KEY"] == "overridden-locally"

    def test_the_local_overlay_beats_the_committed_file(self, ssm, tmp_path):
        write_env(tmp_path, "PDUM_SSM_PATH=/myapp/\nAWS_PROFILE=team-account\n")
        write_env(tmp_path, "AWS_PROFILE=my-sandbox\n", name=".env.local")
        environ: dict[str, str] = {}

        load_env(environ=environ)

        assert environ["AWS_PROFILE"] == "my-sandbox"

    def test_the_files_are_applied_before_the_store_is_opened(self, monkeypatch, tmp_path):
        """The ordering the whole design turns on.

        ``AWS_PROFILE`` comes from the project's ``.env``, so it has to be in
        the environment by the time the SSM client is built, or the store is
        read from the wrong account — or no account at all.
        """
        profiles: list[str | None] = []
        fake = FakeSsmClient(SECRETS)

        def client(*a, **k):
            profiles.append(os.environ.get("AWS_PROFILE"))
            return fake

        monkeypatch.setattr("pdum.aws.secrets.client", client)
        write_env(tmp_path, "AWS_PROFILE=project-account\nPDUM_SSM_PATH=/myapp/\n")

        load_env()

        assert profiles == ["project-account"]


class TestFailures:
    def test_no_search_path_raises_rather_than_passing_over_it(self, ssm, tmp_path):
        write_env(tmp_path, "AWS_PROFILE=project-account\n")

        with pytest.raises(NoSearchPath) as caught:
            load_env()

        assert "PDUM_SSM_PATH" in str(caught.value)
        # The .env was still applied before the failure, which is what makes the
        # message able to name the file it looked in.
        assert os.environ["AWS_PROFILE"] == "project-account"
        assert ".env" in str(caught.value)

    def test_it_is_a_lookup_error(self, ssm):
        with pytest.raises(LookupError):
            load_env()

    def test_a_root_path_is_refused(self, ssm, monkeypatch):
        monkeypatch.setenv("PDUM_SSM_PATH", "/")

        with pytest.raises(ValueError, match="non-root prefix"):
            load_env()

    def test_without_an_envvar_only_the_default_remains(self, ssm, monkeypatch):
        monkeypatch.setenv("PDUM_SSM_PATH", "/myapp/")

        with pytest.raises(NoSearchPath, match="no default was supplied"):
            load_env(envvar=None)


class TestPieces:
    def test_reading_two_files_interpolates_across_them(self, tmp_path):
        write_env(tmp_path, "STAGE=prod\n")
        write_env(tmp_path, "BUCKET=assets-${STAGE}\n", name=".env.local")

        merged = read_env_files(find_env_files())

        assert merged == {"STAGE": "prod", "BUCKET": "assets-prod"}

    def test_reading_without_interpolation_keeps_values_verbatim(self, tmp_path):
        path = write_env(tmp_path, "PASSWORD=p@ss${word}word\n")

        assert read_env_files([path], interpolate=False) == {"PASSWORD": "p@ss${word}word"}
        assert read_env_files([path]) == {"PASSWORD": "p@ssword"}

    def test_a_file_without_a_trailing_newline_does_not_swallow_the_next(self, tmp_path):
        (tmp_path / ".env").write_text("FIRST=1")
        (tmp_path / ".env.local").write_text("SECOND=2\n")

        assert read_env_files(find_env_files()) == {"FIRST": "1", "SECOND": "2"}

    @pytest.mark.parametrize("name", ["API_KEY", "_private", "a1"])
    def test_usable_variable_names(self, name):
        assert is_env_name(name)

    @pytest.mark.parametrize("name", ["db/PASSWORD", "1LEADING", "has-dash", "has space", ""])
    def test_unusable_variable_names(self, name):
        assert not is_env_name(name)

    def test_nothing_happens_on_import(self, tmp_path, monkeypatch):
        """A library must not read the filesystem behind a plain ``import``."""
        write_env(tmp_path, "PDUM_SSM_PATH=/myapp/\nSENTINEL=loaded\n")
        import importlib

        import pdum.aws.env

        importlib.reload(pdum.aws.env)

        assert "SENTINEL" not in os.environ
