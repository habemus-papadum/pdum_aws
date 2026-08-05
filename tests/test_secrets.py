"""Tests for the SSM-backed secret store.

These exercise everything that does not require AWS credentials: prefix
handling, env-first resolution, and the laziness of client construction.
"""

import pytest
from botocore.exceptions import ClientError

from pdum.aws.secrets import SecretStore
from tests.fakes import FakeSsmClient, client_error


class TestPrefix:
    """The prefix is required and normalised."""

    @pytest.mark.parametrize("given", ["myapp", "/myapp", "myapp/", "/myapp/", "  /myapp/  "])
    def test_normalises_to_slashed_form(self, given):
        assert SecretStore(given).prefix == "/myapp/"

    def test_nested_prefix_is_preserved(self):
        assert SecretStore("myapp/prod").prefix == "/myapp/prod/"

    @pytest.mark.parametrize("given", ["", "   ", "/", "//"])
    def test_root_or_empty_is_rejected(self, given):
        # A root prefix would enumerate every parameter in the account, so it is
        # refused rather than silently spanning other applications' secrets.
        with pytest.raises(ValueError, match="non-root prefix"):
            SecretStore(given)

    def test_path_joins_prefix_and_name(self):
        assert SecretStore("/myapp/").path("STRIPE_KEY") == "/myapp/STRIPE_KEY"

    def test_repr_shows_prefix(self):
        assert repr(SecretStore("/myapp/")) == "SecretStore(prefix='/myapp/')"


class TestEnvFirst:
    """Environment variables win over SSM, and skip AWS entirely."""

    def test_env_var_is_returned_without_touching_aws(self, monkeypatch):
        monkeypatch.setenv("MY_SECRET", "from-env")
        store = SecretStore("/myapp/")
        assert store.get("MY_SECRET") == "from-env"
        # The SSM client is built lazily; an env hit must not have created one.
        assert store._ssm is None

    def test_empty_env_var_does_not_shadow_ssm(self, monkeypatch):
        # An empty value is treated as unset, so an accidental `export X=` does
        # not mask a real stored secret with an empty string.
        monkeypatch.setenv("MY_SECRET", "")
        store = SecretStore("/myapp/")
        store._cache["MY_SECRET"] = "from-ssm"
        assert store.get("MY_SECRET") == "from-ssm"


class TestMissingSecrets:
    """Absent secrets return a default or raise, as asked."""

    def test_default_is_returned(self):
        store = SecretStore("/myapp/")
        store._cache["ABSENT"] = None
        assert store.get("ABSENT", default="fallback") == "fallback"

    def test_required_raises_keyerror_naming_the_path(self):
        store = SecretStore("/myapp/")
        store._cache["ABSENT"] = None
        with pytest.raises(KeyError, match="/myapp/ABSENT"):
            store.get("ABSENT", required=True)

    def test_get_json_parses_and_defaults(self):
        store = SecretStore("/myapp/")
        store._cache["BLOB"] = '{"a": 1}'
        store._cache["ABSENT"] = None
        assert store.get_json("BLOB") == {"a": 1}
        assert store.get_json("ABSENT", default={}) == {}


def test_clear_cache_forces_a_refetch():
    store = SecretStore("/myapp/")
    store._cache["K"] = "stale"
    store.clear_cache()
    assert store._cache == {}


class TestAgainstSsm:
    """Read/write paths, exercised against an in-memory SSM client."""

    @pytest.fixture
    def store(self, monkeypatch):
        fake = FakeSsmClient({"/myapp/EXISTING": "stored-value"})
        s = SecretStore("/myapp/")
        monkeypatch.setattr(s, "_ssm", fake)
        return s

    def test_get_reads_through_to_ssm(self, store, monkeypatch):
        monkeypatch.delenv("EXISTING", raising=False)
        assert store.get("EXISTING") == "stored-value"

    def test_get_caches_so_a_second_read_costs_nothing(self, store, monkeypatch):
        monkeypatch.delenv("EXISTING", raising=False)
        store.get("EXISTING")
        store._ssm.parameters["/myapp/EXISTING"] = "changed-underneath"
        assert store.get("EXISTING") == "stored-value"
        store.clear_cache()
        assert store.get("EXISTING") == "changed-underneath"

    def test_missing_parameter_is_cached_as_absent(self, store, monkeypatch):
        monkeypatch.delenv("NOPE", raising=False)
        assert store.get("NOPE", default="fallback") == "fallback"
        assert store._cache["NOPE"] is None

    def test_unexpected_client_errors_propagate(self, store, monkeypatch):
        # Only ParameterNotFound is swallowed; an auth failure must not be
        # silently reported as "secret absent".
        monkeypatch.delenv("BOOM", raising=False)

        def denied(Name, WithDecryption=False):  # noqa: N803
            raise client_error("AccessDeniedException", "GetParameter")

        store._ssm.get_parameter = denied
        with pytest.raises(ClientError, match="AccessDenied"):
            store.get("BOOM")

    def test_put_writes_prefixed_and_secure_by_default(self, store):
        store.put("NEW", "v")
        assert store._ssm.parameters["/myapp/NEW"] == "v"
        assert store._ssm.types["/myapp/NEW"] == "SecureString"

    def test_put_plain_is_opt_in(self, store):
        store.put("PLAIN", "v", secure=False)
        assert store._ssm.types["/myapp/PLAIN"] == "String"

    def test_put_updates_the_cache_immediately(self, store, monkeypatch):
        monkeypatch.delenv("NEW", raising=False)
        store.put("NEW", "v")
        store._ssm.parameters.clear()
        assert store.get("NEW") == "v"

    def test_delete_removes_from_ssm_and_cache(self, store, monkeypatch):
        monkeypatch.delenv("EXISTING", raising=False)
        store.get("EXISTING")
        store.delete("EXISTING")
        assert "/myapp/EXISTING" not in store._ssm.parameters
        assert "EXISTING" not in store._cache

    def test_names_strips_the_prefix(self, store):
        store.put("B", "2")
        store.put("A", "1")
        assert sorted(store.names()) == ["A", "B", "EXISTING"]

    def test_describe_reports_metadata_without_decrypting(self, store):
        rows = {r["name"]: r for r in store.describe()}
        assert rows["EXISTING"]["type"] == "SecureString"
        assert rows["EXISTING"]["version"] == 1
        assert "value" not in rows["EXISTING"]
