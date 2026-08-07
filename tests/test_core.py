"""Tests for ambient credential handling.

The point of :mod:`pdum.aws.core` is that it names no profile, region, or
account, so these tests assert on how the ambient environment is read rather
than on any AWS call.
"""

import boto3

from pdum import aws


def test_session_is_not_the_shared_default(monkeypatch):
    # A per-call session is what keeps each call honest about the current
    # environment; boto3's module-level default session would cache the first
    # credentials it resolved for the life of the process.
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    assert aws.session() is not aws.session()
    assert aws.session() is not boto3.DEFAULT_SESSION


def test_session_honours_an_explicit_region():
    assert aws.session(region="eu-west-1").region_name == "eu-west-1"


def test_session_takes_the_region_from_the_environment(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "ap-southeast-2")
    assert aws.session().region_name == "ap-southeast-2"


def test_no_profile_is_ever_pinned(monkeypatch):
    # The core invariant of this module: it must never pass a profile of its own
    # to boto3. Asserted by inspecting the constructor call rather than a
    # resolved profile name, so the test does not depend on what happens to be
    # in the machine's ~/.aws/config.
    captured: dict[str, object] = {}

    class SpySession:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(boto3, "Session", SpySession)
    aws.session()
    assert "profile_name" not in captured
    assert captured == {"region_name": None}


def test_public_surface_is_minimal():
    """``load_env`` is here on purpose; everything else stays in its own module.

    It is the one function a script is meant to reach for by name, in place of
    ``dotenv.load_dotenv()`` — so it earns the short import. Its companions are
    the report it returns and the error it raises, which a caller cannot use it
    without.
    """
    assert set(aws.__all__) == {
        "EnvReport",
        "NoSearchPath",
        "__version__",
        "client",
        "load_env",
        "resource",
        "session",
        "whoami",
    }


def test_client_and_resource_delegate_to_the_session(monkeypatch):
    calls: dict[str, object] = {}

    class SpySession:
        def __init__(self, **kwargs):
            calls["init"] = kwargs

        def client(self, name):
            calls["client"] = name
            return f"client:{name}"

        def resource(self, name):
            calls["resource"] = name
            return f"resource:{name}"

    monkeypatch.setattr(boto3, "Session", SpySession)
    assert aws.client("ssm") == "client:ssm"
    assert aws.resource("s3") == "resource:s3"
    assert calls["init"] == {"region_name": None}


def test_client_passes_an_explicit_region_through(monkeypatch):
    captured: dict[str, object] = {}

    class SpySession:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def client(self, name):
            return name

    monkeypatch.setattr(boto3, "Session", SpySession)
    aws.client("route53domains", region="us-east-1")
    assert captured == {"region_name": "us-east-1"}


def test_whoami_returns_the_sts_identity(monkeypatch):
    class FakeSts:
        def get_caller_identity(self):
            return {"Account": "123456789012", "Arn": "arn:aws:iam::123456789012:root", "UserId": "u"}

    class SpySession:
        def __init__(self, **kwargs):
            pass

        def client(self, name):
            assert name == "sts"
            return FakeSts()

    monkeypatch.setattr(boto3, "Session", SpySession)
    assert aws.whoami()["Account"] == "123456789012"
