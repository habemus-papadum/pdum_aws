"""Secrets in AWS SSM Parameter Store, with env-first resolution.

A :class:`SecretStore` maps a name like ``STRIPE_KEY`` onto an SSM parameter at
``<prefix>STRIPE_KEY``, and resolves reads in this order:

1. a real **environment variable** of that name, so a shell export, a ``.env``
   loaded by the caller, or a test fixture can override without touching AWS;
2. the **SSM parameter** (decrypted);
3. the supplied ``default``, or a :class:`KeyError` when ``required=True``.

Why Parameter Store rather than Secrets Manager: standard-tier parameters are
free where Secrets Manager is roughly $0.40 per secret per month, and both give
KMS encryption, IAM gating and CloudTrail audit. This module is the only thing
that knows which backend is in use, so switching later is a one-file change.

The prefix is **required** and has no default — a shared default would let two
unrelated projects collide in the same namespace, and would make
:meth:`SecretStore.names` return parameters the caller does not own.

```python
from pdum.aws.secrets import SecretStore

store = SecretStore("/myapp/")
store.put("STRIPE_KEY", "sk_live_...")
store.get("STRIPE_KEY", required=True)
```

This module does not read ``.env`` files. Env-first resolution looks at
``os.environ`` only; if you want ``.env`` support, call ``python-dotenv``
yourself before reading, which keeps that dependency and its import-time
filesystem access out of this library.
"""

import json
import os
from typing import Any

from pdum.aws.core import client

__all__ = ["SecretStore"]


def _normalise_prefix(prefix: str) -> str:
    """Return *prefix* as a leading- and trailing-slashed path.

    Accepts ``myapp``, ``/myapp`` or ``/myapp/`` and returns ``/myapp/`` for all
    three, so callers need not remember the convention.

    Parameters
    ----------
    prefix : str
        The desired SSM path prefix.

    Returns
    -------
    str
        Normalised prefix, e.g. ``"/myapp/"``.

    Raises
    ------
    ValueError
        If *prefix* is empty, blank, or ``"/"``. A root prefix would make
        :meth:`SecretStore.names` enumerate every parameter in the account,
        including ones belonging to other applications.
    """
    trimmed = prefix.strip().strip("/")
    if not trimmed:
        raise ValueError(
            "SecretStore requires a non-root prefix such as '/myapp/'; "
            f"got {prefix!r}. A root prefix would span the whole account's Parameter Store."
        )
    return f"/{trimmed}/"


class SecretStore:
    """A namespaced view of SSM Parameter Store.

    Parameters
    ----------
    prefix : str
        SSM path prefix owning this store's parameters, e.g. ``"/myapp/"``.
        Required; see the module docstring for why there is no default.
    region : str, optional
        Region for the SSM client. When ``None``, ``boto3`` resolves it from the
        environment or active profile.

    Attributes
    ----------
    prefix : str
        The normalised prefix, always leading- and trailing-slashed.
    """

    def __init__(self, prefix: str, *, region: str | None = None) -> None:
        self.prefix = _normalise_prefix(prefix)
        self._region = region
        self._cache: dict[str, str | None] = {}
        self._ssm: Any = None

    def __repr__(self) -> str:
        return f"{type(self).__name__}(prefix={self.prefix!r})"

    @property
    def ssm(self) -> Any:
        """The SSM client, created lazily on first use.

        Deferred so that constructing a store, or resolving a secret that is
        already present in the environment, never requires credentials.
        """
        if self._ssm is None:
            self._ssm = client("ssm", self._region)
        return self._ssm

    def path(self, name: str) -> str:
        """Return the full SSM parameter path for *name*.

        Parameters
        ----------
        name : str
            Secret name without the prefix.

        Returns
        -------
        str
            The full path, e.g. ``"/myapp/STRIPE_KEY"``.
        """
        return self.prefix + name

    def get(self, name: str, *, default: str | None = None, required: bool = False) -> str | None:
        """Resolve a secret from the environment, then SSM, then *default*.

        Successful and missing lookups are both cached for the life of the
        store, so repeated reads cost one API call at most.

        Parameters
        ----------
        name : str
            Secret name without the prefix.
        default : str, optional
            Returned when the secret exists nowhere and ``required`` is false.
        required : bool, default False
            Raise instead of returning *default* when the secret is missing.

        Returns
        -------
        str or None
            The secret value, or *default* when absent.

        Raises
        ------
        KeyError
            If the secret is missing and ``required`` is true.
        """
        env_value = os.environ.get(name)
        if env_value:
            return env_value

        if name not in self._cache:
            from botocore.exceptions import ClientError

            try:
                response = self.ssm.get_parameter(Name=self.path(name), WithDecryption=True)
                self._cache[name] = response["Parameter"]["Value"]
            except ClientError as exc:
                if exc.response["Error"]["Code"] == "ParameterNotFound":
                    self._cache[name] = None
                else:
                    raise

        value = self._cache[name]
        if value is None:
            if required:
                raise KeyError(f"secret {name!r} not found in the environment or SSM at {self.path(name)}")
            return default
        return value

    def get_json(self, name: str, *, default: Any = None, required: bool = False) -> Any:
        """Resolve a secret and parse it as JSON.

        Parameters
        ----------
        name : str
            Secret name without the prefix.
        default : Any, optional
            Returned when the secret is absent and ``required`` is false.
        required : bool, default False
            Raise instead of returning *default* when the secret is missing.

        Returns
        -------
        Any
            The parsed JSON value, or *default* when absent.

        Raises
        ------
        KeyError
            If the secret is missing and ``required`` is true.
        json.JSONDecodeError
            If the stored value is not valid JSON.
        """
        raw = self.get(name, default=None, required=required)
        return json.loads(raw) if raw else default

    def put(self, name: str, value: str, *, secure: bool = True) -> None:
        """Create or overwrite a secret, updating the cache.

        Parameters
        ----------
        name : str
            Secret name without the prefix.
        value : str
            Value to store.
        secure : bool, default True
            Store as a KMS-encrypted ``SecureString``. Pass ``False`` for
            non-sensitive configuration that benefits from the same namespace.
        """
        self.ssm.put_parameter(
            Name=self.path(name),
            Value=value,
            Type="SecureString" if secure else "String",
            Overwrite=True,
        )
        self._cache[name] = value

    def delete(self, name: str) -> None:
        """Delete a secret and drop it from the cache.

        Parameters
        ----------
        name : str
            Secret name without the prefix.
        """
        self.ssm.delete_parameter(Name=self.path(name))
        self._cache.pop(name, None)

    def names(self) -> list[str]:
        """List the secret names in this store, without the prefix.

        Returns
        -------
        list of str
            Secret names, unsorted.
        """
        return [row["name"] for row in self.describe()]

    def describe(self) -> list[dict[str, Any]]:
        """List metadata for each secret in this store.

        Deliberately does not decrypt: this reads names, types and timestamps
        only, so rendering a listing never pulls secret values over the wire.

        Returns
        -------
        list of dict
            One dict per secret with ``name``, ``type``, ``version`` and
            ``modified`` keys.
        """
        paginator = self.ssm.get_paginator("get_parameters_by_path")
        out: list[dict[str, Any]] = []
        for page in paginator.paginate(Path=self.prefix, Recursive=True):
            out += [
                {
                    "name": p["Name"][len(self.prefix) :],
                    "type": p["Type"],
                    "version": p["Version"],
                    "modified": p["LastModifiedDate"],
                }
                for p in page["Parameters"]
            ]
        return out

    def clear_cache(self) -> None:
        """Drop cached values so the next read goes back to SSM.

        Needed when another process has rotated a secret during this one's
        lifetime.
        """
        self._cache.clear()
