"""Building a process environment from ``.env`` files and SSM, in layers.

This is what ``pdx`` does, available to any script that wants the same
environment without being launched under it:

```python
from pdum.aws import load_env

load_env()   # os.environ now carries the project's variables and its secrets
```

Use it where you would otherwise call ``dotenv.load_dotenv()``. It does strictly
more: the same ``.env`` handling, plus the ``.env.local`` overlay, plus the
secrets from the SSM search path that the ``.env`` usually names.

Three layers, most specific first
---------------------------------
1. **the environment you already have** — never overwritten, so a variable
   exported for one run wins, and a CI job's injected variables are never
   undone;
2. **the nearest ``.env``, overlaid with an adjacent ``.env.local``** — searched
   for upward from the working directory, so this works from anywhere inside a
   project. The *whole* file is loaded, not just the SSM path: a project's
   ``.env`` is where ``AWS_PROFILE`` and ``AWS_REGION`` live too, and without
   those the store cannot be reached at all. That is why these are applied
   **before** the store is opened;
3. **the SSM store**, on the search path named by ``PDUM_SSM_PATH`` — which is
   itself usually set by that ``.env``.

Nothing here runs on import. :mod:`pdum.aws.secrets` says that a library must
not read ``.env`` behind a plain ``import``, and that still holds — this module
provides the function and lets the application decide when to call it. Being
explicit is also what makes the ordering above something you can rely on.

``.env`` and ``.env.local``
---------------------------
Only ``.env`` is searched for; the overlay is taken from *beside* whatever was
found, never searched for separately, so one directory always wins and there is
never a question of which ``.env.local`` applies. A ``.env.local`` with no
``.env`` to anchor it is ignored.

The two are read as **one** document rather than as two dicts to merge, so that
a ``${VAR}`` in the local file can refer to a name defined in the committed one
— which is most of the point of having a local file. Merging parsed results
would leave that reference unresolved.

Parsing goes through ``python-dotenv`` rather than a hand-rolled ``KEY=VALUE``
split, because the format has more corners than it appears to: ``export``
prefixes, inline comments, single versus double quoting, and quoted values
spanning several lines. A naive split reads

```ini
PDUM_SSM_PATH=/myapp/:/org/   # the project's own layer
```

as a prefix with a comment glued onto the end, and SSM would take that at face
value.

Interpolation of ``${VAR}`` is the caller's choice, because the two callers want
opposite things. Loading an environment should expand, as every other ``.env``
consumer does. ``secrets import`` should not: a value on its way into permanent
storage must arrive exactly as written, or the password ``p@ss${word}word``
silently becomes ``p@ssword`` and nothing records that it ever said more.
"""

import io
import os
import re
from collections.abc import Callable, Mapping, MutableMapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from dotenv import dotenv_values, find_dotenv

from pdum.aws.secrets import SecretStore

__all__ = [
    "DEFAULT_ENVVAR",
    "LOCAL_SUFFIX",
    "EnvReport",
    "LoadReport",
    "NoSearchPath",
    "PathDefault",
    "find_env_file",
    "find_env_files",
    "is_env_name",
    "load_env",
    "load_env_files",
    "load_secrets",
    "read_env_file",
    "read_env_files",
    "resolve_search_path",
]

#: Environment variable naming the SSM search path.
DEFAULT_ENVVAR = "PDUM_SSM_PATH"

#: Appended to the file name to form the uncommitted overlay: ``.env.local``.
LOCAL_SUFFIX = ".local"

#: Names a shell can actually use. A store may hold nested parameters, whose
#: names carry a slash (``db/PASSWORD``); exporting one would make a variable no
#: program could name, so it is reported and skipped instead.
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")

#: A search path supplied by the caller: literal, deferred, or absent.
PathDefault = str | Callable[[], str | None] | None


def is_env_name(name: str) -> bool:
    """Whether *name* can be used as an environment variable.

    A store may hold nested parameters, whose names carry a slash — a secret at
    ``/myapp/db/PASSWORD`` is named ``db/PASSWORD``. Exporting that would make a
    variable no program could name, so such names are reported and skipped
    rather than set.

    Parameters
    ----------
    name : str
        Candidate variable name.

    Returns
    -------
    bool
        True when a shell could name it.
    """
    return _ENV_NAME.match(name) is not None


class NoSearchPath(LookupError):
    """No SSM search path could be resolved, so no secrets could be loaded.

    Raised rather than passed over: a process that quietly continues without the
    secrets it asked for fails later, somewhere less informative.
    """


@dataclass(frozen=True, slots=True)
class LoadReport:
    """What one layer contributed to the environment.

    Attributes
    ----------
    source : str
        Where the values came from, for reporting: a file path, or a search
        path.
    applied : list of str
        Names this layer set.
    already_set : list of str
        Names it offered that the environment already had, so were kept. A more
        specific layer had already won.
    skipped : list of str
        Names unusable as environment variables, e.g. a nested ``db/PASSWORD``.
    """

    source: str
    applied: list[str]
    already_set: list[str]
    skipped: list[str]


@dataclass(frozen=True, slots=True)
class EnvReport:
    """What :func:`load_env` did, layer by layer.

    Attributes
    ----------
    env_files : LoadReport or None
        What the ``.env`` and its overlay contributed, or ``None`` when there
        was no such file.
    path : str
        The SSM search path that was used.
    path_source : str
        Where that path came from, for reporting.
    secrets : LoadReport
        What the store contributed.
    """

    env_files: LoadReport | None
    path: str
    path_source: str
    secrets: LoadReport


# --------------------------------------------------------------------------- #
# reading files
# --------------------------------------------------------------------------- #


def read_env_files(paths: Sequence[Path | str], *, interpolate: bool = True) -> dict[str, str]:
    """Read several ``.env`` files as though they were one, later files winning.

    The files are concatenated and parsed once, so interpolation reaches across
    them and a repeated name resolves the way a repeated name inside a single
    file does — the last one wins.

    Parameters
    ----------
    paths : sequence of pathlib.Path or str
        Files to read, least specific first.
    interpolate : bool, default True
        Expand ``${VAR}`` against the environment and earlier entries, as
        ``python-dotenv`` does by default. Pass ``False`` when the values are
        headed somewhere permanent; see the module docstring.

    Returns
    -------
    dict of str to str
        Names mapped to values. A bare name declared with no ``=`` at all is
        dropped rather than reported as ``None``, so every value here is a
        string the caller can use directly.
    """
    # Explicit utf-8, and an explicit separator: Path.read_text would otherwise
    # use the locale encoding, and a file with no trailing newline would glue
    # its last line onto the next file's first.
    text = "\n".join(Path(path).read_text(encoding="utf-8") for path in paths)
    values = dotenv_values(stream=io.StringIO(text), interpolate=interpolate)
    return {key: value for key, value in values.items() if value is not None}


def read_env_file(path: Path | str, *, interpolate: bool = True) -> dict[str, str]:
    """Read one ``.env`` file. See :func:`read_env_files`."""
    return read_env_files([path], interpolate=interpolate)


def find_env_file(name: str = ".env") -> Path | None:
    """Find *name* in the working directory or the nearest ancestor holding one.

    Searching upward is what lets this run from anywhere inside a project and
    still find that project's file, the way ``git`` finds its repository. A
    *name* with a directory component is taken literally instead, so an explicit
    ``config/.env`` is never second-guessed.

    Parameters
    ----------
    name : str, default ".env"
        File name to search for, or a path to use as given.

    Returns
    -------
    pathlib.Path or None
        The file found, or ``None`` when there is none.
    """
    candidate = Path(name)
    if candidate.parent != Path():
        return candidate if candidate.is_file() else None
    # usecwd=True is essential: python-dotenv otherwise walks up from the file
    # that called it, which here would be this library rather than the project.
    found = find_dotenv(candidate.name, usecwd=True)
    return Path(found) if found else None


def find_env_files(name: str = ".env") -> list[Path]:
    """Find *name* and its adjacent ``.local`` overlay, least specific first.

    Parameters
    ----------
    name : str, default ".env"
        File name to search for, or a path to use as given.

    Returns
    -------
    list of pathlib.Path
        Empty when *name* was not found, otherwise the file and, if it exists,
        its ``.local`` neighbour.
    """
    base = find_env_file(name)
    if base is None:
        return []
    local = base.with_name(base.name + LOCAL_SUFFIX)
    return [base, local] if local.is_file() else [base]


# --------------------------------------------------------------------------- #
# applying them
# --------------------------------------------------------------------------- #


def _apply(values: Mapping[str, str], environ: MutableMapping[str, str], source: str) -> LoadReport:
    """Add *values* to *environ* without disturbing what is already there."""
    applied: list[str] = []
    already_set: list[str] = []
    skipped: list[str] = []
    for name, value in sorted(values.items()):
        if not is_env_name(name):
            skipped.append(name)
        elif environ.get(name):
            # Truthiness, not membership, to agree with SecretStore.get, where
            # an empty variable reads as unset.
            already_set.append(name)
        else:
            environ[name] = value
            applied.append(name)
    return LoadReport(source, applied, already_set, skipped)


def load_env_files(env_file: str, environ: MutableMapping[str, str]) -> LoadReport | None:
    """Apply the nearest ``.env``, and its ``.local`` overlay, to *environ*.

    Every variable in the files is loaded, not only the SSM search path. Call
    this before opening a store, or the store may be opened against the wrong
    account, or no account at all — see the module docstring.

    Parameters
    ----------
    env_file : str
        File name to search for upward from the working directory, or a path
        with a directory component to use as given.
    environ : mutable mapping
        Environment to update, normally ``os.environ``.

    Returns
    -------
    LoadReport or None
        What the files contributed, or ``None`` when there is no such file.
    """
    found = find_env_files(env_file)
    if not found:
        return None
    base, *overlay = found
    source = f"{base} + {overlay[0].name}" if overlay else str(base)
    return _apply(read_env_files(found, interpolate=True), environ, source)


def load_secrets(store: SecretStore, environ: MutableMapping[str, str]) -> LoadReport:
    """Apply *store* to *environ*, in one bulk read, keeping what is already set.

    Parameters
    ----------
    store : SecretStore
        Store to read.
    environ : mutable mapping
        Environment to update, normally ``os.environ``.

    Returns
    -------
    LoadReport
        What the store contributed.
    """
    return _apply(store.read_all(), environ, ":".join(store.prefixes))


def resolve_search_path(
    *,
    envvar: str | None = DEFAULT_ENVVAR,
    default_path: PathDefault = None,
    environ: Mapping[str, str],
    env_file_report: LoadReport | None = None,
) -> tuple[str | None, str]:
    """Work out which SSM search path to use, and say where it came from.

    Call after :func:`load_env_files`, so that a path set by a file is already
    in *environ* and needs no separate lookup — it is one environment variable
    among all the others by this point. *env_file_report* is used only to
    describe the source precisely.

    Parameters
    ----------
    envvar : str, optional
        Variable holding the path. ``None`` leaves only *default_path*.
    default_path : str or callable, optional
        Fallback when the environment has none; a callable is resolved here.
    environ : mapping
        Environment to consult, normally ``os.environ``.
    env_file_report : LoadReport, optional
        Result of :func:`load_env_files`, to tell a path that came from a file
        from one that was already exported.

    Returns
    -------
    tuple of (str or None, str)
        The path, and a short description of its source for reporting.
    """
    if envvar and environ.get(envvar):
        if env_file_report is not None and envvar in env_file_report.applied:
            return environ[envvar], env_file_report.source
        return environ[envvar], f"${envvar}"
    resolved = default_path() if callable(default_path) else default_path
    return (resolved, "built-in default") if resolved else (None, "")


def no_search_path_message(envvar: str | None, env_file: str, found: LoadReport | None) -> str:
    """Explain every way the search path could have been supplied."""
    if not envvar:
        return "no SSM search path configured, and no default was supplied"
    where = f"add {envvar}= to {found.source}" if found else f"add {envvar}= to a {env_file} in this project"
    return f"no SSM search path configured: set {envvar}, or {where}"


def load_env(
    *,
    environ: MutableMapping[str, str] | None = None,
    env_file: str = ".env",
    envvar: str | None = DEFAULT_ENVVAR,
    default_path: PathDefault = None,
    store_factory: Callable[[str], SecretStore] = SecretStore,
) -> EnvReport:
    """Load the project's ``.env`` files and its secrets into the environment.

    The drop-in for ``dotenv.load_dotenv()`` in a project that keeps its secrets
    in SSM. Layers are applied most-specific-last-wins-least: whatever is
    already set stays, then the ``.env`` pair fills gaps, then the store fills
    what remains. See the module docstring for why the order matters.

    Parameters
    ----------
    environ : mutable mapping, optional
        Environment to update; defaults to ``os.environ``. Pass a plain dict to
        compute an environment without touching this process's own.
    env_file : str, default ".env"
        File to search for upward from the working directory, and the base name
        of the ``.local`` overlay beside it. A name with a directory component
        is used as given.
    envvar : str, optional
        Variable naming the SSM search path. ``None`` leaves only
        *default_path*.
    default_path : str or callable, optional
        Search path to fall back on when the environment, including the
        ``.env``, has none. A callable is resolved here.
    store_factory : callable, default :class:`~pdum.aws.secrets.SecretStore`
        Called with the resolved path to build the store. Override to pin a
        region, e.g. ``lambda path: SecretStore(path, region="us-east-1")``.

    Returns
    -------
    EnvReport
        What each layer contributed. Ignore it for the common case; it is there
        for reporting and for tests.

    Raises
    ------
    NoSearchPath
        If no search path could be resolved. Loading a ``.env`` and silently
        skipping the secrets would leave a process that fails later, somewhere
        less informative.
    ValueError
        If the resolved path is not a usable SSM prefix — a root path, say.
    """
    target = os.environ if environ is None else environ
    from_files = load_env_files(env_file, target)
    path, source = resolve_search_path(
        envvar=envvar, default_path=default_path, environ=target, env_file_report=from_files
    )
    if not path:
        raise NoSearchPath(no_search_path_message(envvar, env_file, from_files))
    return EnvReport(
        env_files=from_files,
        path=path,
        path_source=source,
        secrets=load_secrets(store_factory(path), target),
    )
