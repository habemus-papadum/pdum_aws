"""Secrets in AWS SSM Parameter Store, resolved along a search path.

A :class:`SecretStore` is built from a **search path** of SSM prefixes, most
specific first — ``"/myapp/:/org/"`` — and maps a name like ``STRIPE_KEY``
onto the parameter ``<prefix>STRIPE_KEY`` in each layer. Reads resolve in this
order:

1. a real **environment variable** of that name, so a shell export, a ``.env``
   loaded by the caller, or a test fixture can override without touching AWS;
2. each **SSM prefix** in path order (decrypted), first hit wins;
3. the supplied ``default``, or a :class:`KeyError` when ``required=True``.

Layered prefixes work like layered config files (project, then org, then
global): a project stores only what is truly its own and inherits the rest,
so a secret shared by many projects lives in exactly one place. **Writes and
deletes always target the first prefix** — the layer the store belongs to —
never a fallback layer; mutating a shared layer requires constructing a store
whose path starts there.

Why Parameter Store rather than Secrets Manager: standard-tier parameters are
free where Secrets Manager is roughly $0.40 per secret per month, and both give
KMS encryption, IAM gating and CloudTrail audit. This module is the only thing
that knows which backend is in use, so switching later is a one-file change.

The path is **required** and has no default — a shared default would let two
unrelated projects collide in the same namespace, and would make
:meth:`SecretStore.names` return parameters the caller does not own.

```python
from pdum.aws.secrets import SecretStore

store = SecretStore("/myapp/:/org/")
store.put("STRIPE_KEY", "sk_live_...")   # written to /myapp/STRIPE_KEY
store.get("GOOGLE_CLIENT_ID")            # found at /org/GOOGLE_CLIENT_ID
```

This module does not read ``.env`` files. Env-first resolution looks at
``os.environ`` only; if you want ``.env`` support, call ``python-dotenv``
yourself before reading, which keeps that dependency and its import-time
filesystem access out of this library.
"""

import json
import os
from collections.abc import Sequence
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


def _normalise_path(path: str | Sequence[str]) -> tuple[str, ...]:
    """Return *path* as a tuple of normalised prefixes, most specific first.

    Accepts a colon-separated string (``"/myapp/:/org/"``) or a sequence of
    prefixes; each element is normalised by :func:`_normalise_prefix`.
    Duplicates are dropped, keeping the first occurrence.

    Raises
    ------
    ValueError
        If the path is empty, or any element is empty or root.
    """
    parts = [p for p in path.split(":") if p.strip()] if isinstance(path, str) else list(path)
    if not parts:
        raise ValueError(
            "SecretStore requires a search path of at least one prefix, "
            f"such as '/myapp/' or '/myapp/:/org/'; got {path!r}."
        )
    out: list[str] = []
    for part in parts:
        prefix = _normalise_prefix(part)
        if prefix not in out:
            out.append(prefix)
    return tuple(out)


class SecretStore:
    """A namespaced view of SSM Parameter Store, layered along a search path.

    Parameters
    ----------
    path : str or sequence of str
        SSM search path, most specific prefix first: a colon-separated string
        (``"/myapp/:/org/"``) or a sequence (``["/myapp/", "/org/"]``).
        Required; see the module docstring for why there is no default.
    region : str, optional
        Region for the SSM client. When ``None``, ``boto3`` resolves it from the
        environment or active profile.

    Attributes
    ----------
    prefixes : tuple of str
        The normalised search path, each element leading- and trailing-slashed.
    """

    def __init__(self, path: str | Sequence[str], *, region: str | None = None) -> None:
        self.prefixes = _normalise_path(path)
        self._region = region
        self._cache: dict[str, str | None] = {}
        self._ssm: Any = None

    def __repr__(self) -> str:
        return f"{type(self).__name__}(path={':'.join(self.prefixes)!r})"

    @property
    def prefix(self) -> str:
        """The first prefix on the path — the layer writes and deletes target."""
        return self.prefixes[0]

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
        """Return the full SSM parameter path for *name* in the write layer.

        Parameters
        ----------
        name : str
            Secret name without a prefix.

        Returns
        -------
        str
            The full path, e.g. ``"/myapp/STRIPE_KEY"``.
        """
        return self.prefix + name

    def _read_layer(self, prefix: str, name: str) -> str | None:
        """Fetch ``<prefix><name>`` from SSM, or ``None`` when absent."""
        from botocore.exceptions import ClientError

        try:
            response = self.ssm.get_parameter(Name=prefix + name, WithDecryption=True)
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ParameterNotFound":
                return None
            raise
        return response["Parameter"]["Value"]

    def get(self, name: str, *, default: str | None = None, required: bool = False) -> str | None:
        """Resolve a secret from the environment, then each prefix, then *default*.

        Successful and missing lookups are both cached for the life of the
        store, so repeated reads cost one walk of the path at most.

        Parameters
        ----------
        name : str
            Secret name without a prefix.
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
            value = None
            for prefix in self.prefixes:
                value = self._read_layer(prefix, name)
                if value is not None:
                    break
            self._cache[name] = value

        value = self._cache[name]
        if value is None:
            if required:
                searched = ", ".join(p + name for p in self.prefixes)
                raise KeyError(f"secret {name!r} not found in the environment or SSM at {searched}")
            return default
        return value

    def get_json(self, name: str, *, default: Any = None, required: bool = False) -> Any:
        """Resolve a secret and parse it as JSON.

        Parameters
        ----------
        name : str
            Secret name without a prefix.
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
        """Create or overwrite a secret in the write layer, updating the cache.

        Parameters
        ----------
        name : str
            Secret name without a prefix.
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
        """Delete a secret from the write layer and drop it from the cache.

        Only the first prefix is ever deleted from. When the name exists solely
        in a fallback layer, this raises rather than reaching down: deleting
        from a shared layer must be asked for explicitly, with a store whose
        path starts there.

        Parameters
        ----------
        name : str
            Secret name without a prefix.

        Raises
        ------
        KeyError
            If the secret is not in the write layer. The message names the
            fallback layer holding it, when there is one.
        """
        from botocore.exceptions import ClientError

        try:
            self.ssm.delete_parameter(Name=self.path(name))
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "ParameterNotFound":
                raise
            for fallback in self.prefixes[1:]:
                if self._read_layer(fallback, name) is not None:
                    raise KeyError(
                        f"secret {name!r} is not in the write layer {self.prefix}; it lives at "
                        f"{fallback + name} — use a store with path {fallback!r} to delete it there"
                    ) from exc
            raise KeyError(f"secret {name!r} not found under {':'.join(self.prefixes)}") from exc
        self._cache.pop(name, None)

    def names(self) -> list[str]:
        """List the secret names visible to this store, without prefixes.

        Names present in several layers appear once.

        Returns
        -------
        list of str
            Secret names, unsorted.
        """
        return [row["name"] for row in self.describe() if not row["shadowed"]]

    def describe(self) -> list[dict[str, Any]]:
        """List metadata for each secret on the search path.

        Deliberately does not decrypt: this reads names, types and timestamps
        only, so rendering a listing never pulls secret values over the wire.

        Returns
        -------
        list of dict
            One dict per parameter per layer, with ``name``, ``type``,
            ``version``, ``modified``, ``origin`` (the prefix holding it) and
            ``shadowed`` (true when an earlier layer also has the name, so
            resolution never reaches this row).
        """
        paginator = self.ssm.get_paginator("get_parameters_by_path")
        out: list[dict[str, Any]] = []
        seen: set[str] = set()
        for prefix in self.prefixes:
            for page in paginator.paginate(Path=prefix, Recursive=True):
                for p in page["Parameters"]:
                    name = p["Name"][len(prefix) :]
                    out.append(
                        {
                            "name": name,
                            "type": p["Type"],
                            "version": p["Version"],
                            "modified": p["LastModifiedDate"],
                            "origin": prefix,
                            "shadowed": name in seen,
                        }
                    )
            seen.update(row["name"] for row in out)
        return out

    def clear_cache(self) -> None:
        """Drop cached values so the next read goes back to SSM.

        Needed when another process has rotated a secret during this one's
        lifetime.
        """
        self._cache.clear()
