"""Ambient AWS sessions and clients.

This module deliberately knows nothing about named profiles. ``boto3`` already
resolves credentials from ``AWS_PROFILE``, ``AWS_ACCESS_KEY_ID`` /
``AWS_SECRET_ACCESS_KEY``, SSO token caches, ECS/EC2 instance roles and
``~/.aws/config`` — so the correct behaviour for a reusable library is to let it,
and to select an account by setting ``AWS_PROFILE`` in the environment:

```console
$ AWS_PROFILE=my-account python -m my_script
```

Nothing here hardcodes a profile, a region, or an account. A library that picks
a default profile is a library that silently talks to the wrong account when it
is reused somewhere else.

Region is likewise left to ``boto3``, which reads ``AWS_REGION``,
``AWS_DEFAULT_REGION`` and the profile's ``region``. Pass ``region=`` only for
APIs that are pinned to one region regardless of where the caller lives — Route
53 Domains and CloudFront ACM certificates being the usual examples.
"""

from typing import Any

import boto3

__all__ = ["client", "resource", "session", "whoami"]


def session(region: str | None = None) -> boto3.Session:
    """Create a session on the ambient credential chain.

    A fresh :class:`boto3.Session` is created per call rather than reusing
    ``boto3``'s module-level default session, which caches the credentials it
    resolved on first use. Building a new one keeps each call honest about the
    current environment — it picks up a re-exported ``AWS_PROFILE`` or a
    refreshed SSO token instead of serving stale credentials for the life of the
    process.

    Parameters
    ----------
    region : str, optional
        Region to pin the session to. When ``None``, ``boto3`` resolves it from
        the environment or the active profile.

    Returns
    -------
    boto3.Session
        A session bound to whatever credentials the environment provides.
    """
    return boto3.Session(region_name=region)


def client(service: str, region: str | None = None) -> Any:
    """Create a service client on the ambient credential chain.

    Parameters
    ----------
    service : str
        Service name, e.g. ``"ssm"``, ``"ec2"``, ``"service-quotas"``.
    region : str, optional
        Region to pin the client to. See :func:`session`.

    Returns
    -------
    Any
        A ``botocore`` client for *service*.
    """
    return session(region).client(service)


def resource(service: str, region: str | None = None) -> Any:
    """Create a service resource on the ambient credential chain.

    Parameters
    ----------
    service : str
        Service name, e.g. ``"s3"``, ``"dynamodb"``.
    region : str, optional
        Region to pin the resource to. See :func:`session`.

    Returns
    -------
    Any
        A ``boto3`` resource for *service*.
    """
    return session(region).resource(service)


def whoami(region: str | None = None) -> dict[str, str]:
    """Return the identity the current credentials resolve to.

    Useful as a cheap credential check before doing anything destructive, and as
    the fastest way to confirm which account is actually in play.

    Parameters
    ----------
    region : str, optional
        Region for the STS call. See :func:`session`.

    Returns
    -------
    dict of str to str
        The ``sts:GetCallerIdentity`` response, with ``Account``, ``Arn`` and
        ``UserId`` keys.
    """
    return client("sts", region).get_caller_identity()
