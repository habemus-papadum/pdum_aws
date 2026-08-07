"""``pdx`` — run a command with the project's environment and secrets loaded.

```console
$ pdx npm run dev
$ pdx python -m my_service --port 8080
$ pdx -- ls -la
```

**``pdx`` has no options of its own.** Everything after the name is the command
to run, so no flag of yours can collide with one of its own, and ``--`` is
available but never required. The only exception is a leading ``--help``, which
click reserves; ``pdx python --help`` still reaches Python, because parsing
stops at the first bare word.

Three layers, most specific first
---------------------------------
1. **the environment you already have** — never overwritten, so
   ``STRIPE_KEY=sk_test_... pdx ./run-tests`` overrides for that one run and a
   CI job's injected variables are never undone;
2. **the nearest ``.env``**, searched for upward from the working directory, so
   this works from anywhere inside a project. The *whole* file is loaded, not
   just the SSM path — a project's ``.env`` is where ``AWS_PROFILE`` and
   ``AWS_REGION`` live too, and without those the store cannot be reached at
   all. This is also why the file is applied *before* the store is opened;
3. **the SSM store**, on the search path named by ``PDUM_SSM_PATH`` — which is
   itself usually set by that ``.env``.

Then the process is **replaced** by the command, a real ``execvp``: same PID, so
signals, job control, exit status and the terminal all belong to the program you
asked for, with nothing left in the middle to forward them. It inherits the
environment all three layers built up.

``pdx-doctor`` shows what that adds up to without running anything.

Both commands are attachable, so an application can offer the same thing under
its own name and defaults:

```python
from pdum.aws.cli import add_pdx, add_pdx_doctor

add_pdx(app, default_path="/acme/:/org/", envvar="ACME_SSM_PATH")
add_pdx_doctor(app, default_path="/acme/:/org/", envvar="ACME_SSM_PATH")
```
"""

import os
import re
import sys
from collections.abc import Callable, Mapping, MutableMapping
from dataclasses import dataclass, field
from typing import Any, NoReturn

import typer
from rich.console import Console
from rich.table import Table

from pdum.aws.cli.dotenv import find_env_file, read_env_file
from pdum.aws.cli.output import DEFAULT_CONSOLE, DEFAULT_STDERR_CONSOLE, aws_errors
from pdum.aws.secrets import SecretStore

__all__ = [
    "LoadReport",
    "add_pdx",
    "add_pdx_doctor",
    "build_pdx_app",
    "load_env_file",
    "load_secrets",
    "main",
    "resolve_search_path",
]

#: Names a shell can actually use. A store may hold nested parameters, whose
#: names carry a slash (``db/PASSWORD``); exporting one would make a variable no
#: program could name, so it is reported and skipped instead.
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")

#: Click settings that keep this command's parser off the wrapped command line.
#: ``allow_interspersed_args`` stops option parsing at the first bare word, and
#: ``ignore_unknown_options`` keeps ``pdx python -c ...`` from being read as an
#: unknown ``-c`` flag. Together they make ``--`` optional rather than required.
_PASSTHROUGH = {"ignore_unknown_options": True, "allow_interspersed_args": False}

#: The codes a shell uses for these two conditions, worth matching so that
#: ``pdx some-typo`` is diagnosable exactly as a bare ``some-typo`` would be.
EXIT_NOT_FOUND = 127
EXIT_NOT_EXECUTABLE = 126

#: A search path supplied by the host: literal, deferred, or absent.
PathDefault = str | Callable[[], str | None] | None


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
    applied: list[str] = field(default_factory=list)
    already_set: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)


def _apply(values: Mapping[str, str], environ: MutableMapping[str, str], source: str) -> LoadReport:
    """Add *values* to *environ* without disturbing what is already there."""
    applied: list[str] = []
    already_set: list[str] = []
    skipped: list[str] = []
    for name, value in sorted(values.items()):
        if not _ENV_NAME.match(name):
            skipped.append(name)
        elif environ.get(name):
            # Truthiness, not membership, to agree with SecretStore.get, where
            # an empty variable reads as unset.
            already_set.append(name)
        else:
            environ[name] = value
            applied.append(name)
    return LoadReport(source, applied, already_set, skipped)


def load_env_file(env_file: str, environ: MutableMapping[str, str]) -> LoadReport | None:
    """Apply the nearest ``.env`` to *environ*, in full.

    Every variable in the file is loaded, not only the SSM search path: the file
    is a project's environment, and the credentials for reaching the store —
    ``AWS_PROFILE``, ``AWS_REGION`` — are ordinarily in it alongside the path.
    Call this before opening a store, or the store may be opened against the
    wrong account, or no account at all.

    Values are interpolated, as they would be by any other ``.env`` consumer, so
    ``BASE=${HOME}/app`` works. (``secrets import`` deliberately does not
    interpolate; there the values are being written to permanent storage, where
    a silently expanded ``${...}`` cannot be recovered.)

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
        What the file contributed, or ``None`` when there is no such file.
    """
    found = find_env_file(env_file)
    if found is None:
        return None
    return _apply(read_env_file(found, interpolate=True), environ, str(found))


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
    envvar: str | None,
    default_path: PathDefault,
    environ: Mapping[str, str],
    env_file_report: LoadReport | None = None,
) -> tuple[str | None, str]:
    """Work out which SSM search path to use, and say where it came from.

    Call after :func:`load_env_file`, so that a path set by the file is already
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
        Result of :func:`load_env_file`, to tell a path that came from the file
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


def _no_path_message(envvar: str | None, env_file: str, found: LoadReport | None) -> str:
    """Explain every way the search path could have been supplied."""
    if not envvar:
        return "[red]no SSM search path configured:[/] this command was built without one"
    where = f"add {envvar}= to {found.source}" if found else f"add {envvar}= to a {env_file} in this project"
    return f"[red]no SSM search path configured:[/] set {envvar}, or {where}"


def _exec(argv: list[str], console: Console) -> NoReturn:
    """Replace this process with *argv*.

    Buffers are flushed first: ``exec`` runs no ``atexit`` handlers and flushes
    nothing still sitting in userspace, so anything already written would
    otherwise be lost.

    Raises
    ------
    SystemExit
        :data:`EXIT_NOT_FOUND` when the command is not on ``PATH``,
        :data:`EXIT_NOT_EXECUTABLE` when it is there but cannot be run.
    """
    sys.stdout.flush()
    sys.stderr.flush()
    if hasattr(console.file, "flush"):
        console.file.flush()
    try:
        os.execvp(argv[0], argv)
    except FileNotFoundError:
        console.print(f"[red]{argv[0]}: command not found[/]")
        raise SystemExit(EXIT_NOT_FOUND) from None
    except OSError as exc:
        console.print(f"[red]{argv[0]}: {exc.strerror or exc}[/]")
        raise SystemExit(EXIT_NOT_EXECUTABLE) from None


def add_pdx(
    app: typer.Typer,
    *,
    console: Console | None = None,
    default_path: PathDefault = None,
    envvar: str | None = "PDUM_SSM_PATH",
    env_file: str = ".env",
    store_factory: Callable[[str], SecretStore] = SecretStore,
    name: str = "pdx",
) -> None:
    """Register a ``pdx`` command on *app*.

    Parameters
    ----------
    app : typer.Typer
        Application to register the command on.
    console : rich.console.Console, optional
        Console for this command's own diagnostics. Defaults to
        :data:`~pdum.aws.cli.output.DEFAULT_STDERR_CONSOLE`; whatever you pass
        should write to stderr, since stdout belongs to the wrapped program.
    default_path : str or callable, optional
        Search path used when the environment, including the ``.env``, has
        none. A callable is resolved at invocation.
    envvar : str, optional
        Environment variable holding the search path. ``None`` leaves only
        *default_path*; the ``.env`` is still loaded either way.
    env_file : str, default ".env"
        File searched for upward from the working directory. A name with a
        directory component is used as given.
    store_factory : callable, default :class:`~pdum.aws.secrets.SecretStore`
        Called with the resolved path to build the store.
    name : str, default "pdx"
        Name to register the command under.
    """
    out = console or DEFAULT_STDERR_CONSOLE

    @app.command(name, context_settings=_PASSTHROUGH)
    def pdx(
        command: list[str] = typer.Argument(None, metavar="COMMAND [ARGS]...", help="program to run, with arguments"),
    ) -> None:
        """Run a command with the project's environment and secrets loaded.

        Takes no options of its own: everything here is the command to run.
        Variables already set are left alone.
        """
        argv = list(command or [])
        if not argv:
            out.print("[red]nothing to run:[/] pdx takes the command to exec, e.g. [bold]pdx npm start[/]")
            raise typer.Exit(2)

        from_file = load_env_file(env_file, os.environ)
        path, _ = resolve_search_path(
            envvar=envvar, default_path=default_path, environ=os.environ, env_file_report=from_file
        )
        if not path:
            out.print(_no_path_message(envvar, env_file, from_file))
            raise typer.Exit(2)
        try:
            store = store_factory(path)
        except ValueError as exc:
            out.print(f"[red]{exc}[/]")
            raise typer.Exit(2) from exc

        load_secrets(store, os.environ)
        _exec(argv, out)


def add_pdx_doctor(
    app: typer.Typer,
    *,
    console: Console | None = None,
    default_path: PathDefault = None,
    envvar: str | None = "PDUM_SSM_PATH",
    env_file: str = ".env",
    store_factory: Callable[[str], SecretStore] = SecretStore,
    name: str = "pdx-doctor",
) -> None:
    """Register a command explaining what :func:`add_pdx` would do.

    Answers the questions ``secrets list`` cannot, because they are about this
    process rather than about the store: which ``.env`` was found and what it
    contributed, which search path resolved and from where, and which names the
    environment already holds — the reason a program run under ``pdx`` can see a
    value that is not the one in SSM.

    It applies the ``.env`` exactly as ``pdx`` does, since that is what decides
    the account the store is read from. Secrets themselves are reported by name
    and layer only: this works from
    :meth:`~pdum.aws.secrets.SecretStore.describe`, which does not decrypt, so
    running it pulls no secret value over the wire. Use ``secrets get`` when the
    value is what you want.

    Parameters are as for :func:`add_pdx`, except that *console* defaults to
    stdout — here the report is the output.
    """
    out = console or DEFAULT_CONSOLE

    @app.command(name)
    def pdx_doctor() -> None:
        """Explain what pdx would put in the environment, without running anything."""
        from_file = load_env_file(env_file, os.environ)
        if from_file is None:
            out.print(f"[dim]no {env_file} found in this directory or above it[/]")
        else:
            out.print(f"[bold].env[/] {from_file.source}")
            out.print(f"  [green]sets[/] {', '.join(from_file.applied) or '—'}")
            if from_file.already_set:
                out.print(f"  [cyan]kept from the environment[/] {', '.join(from_file.already_set)}")

        path, source = resolve_search_path(
            envvar=envvar, default_path=default_path, environ=os.environ, env_file_report=from_file
        )
        if not path:
            out.print(_no_path_message(envvar, env_file, from_file))
            raise typer.Exit(2)
        out.print(f"[bold]search path[/] {path}  [dim](from {source})[/]")
        try:
            store = store_factory(path)
        except ValueError as exc:
            out.print(f"[red]{exc}[/]")
            raise typer.Exit(2) from exc

        rows = sorted((r for r in store.describe() if not r["shadowed"]), key=lambda r: r["name"])
        if not rows:
            out.print(f"[yellow]no secrets under {path}[/]")
            return

        table = Table(box=None, pad_edge=False)
        table.add_column("NAME", style="bold")
        table.add_column("LAYER")
        table.add_column("PDX WOULD")
        counts = {"set it": 0, "keep the environment's": 0, "skip it": 0}
        for row in rows:
            secret = row["name"]
            if not _ENV_NAME.match(secret):
                verdict, style = "skip it", "yellow"
            elif os.environ.get(secret):
                verdict, style = "keep the environment's", "cyan"
            else:
                verdict, style = "set it", "green"
            counts[verdict] += 1
            table.add_row(secret, row["origin"], f"[{style}]{verdict}[/]")
        out.print(table)
        out.print("  [dim]" + ", ".join(f"{n} to {verdict}" for verdict, n in counts.items() if n) + "[/]")


def build_pdx_app(**kwargs: Any) -> typer.Typer:
    """Build a standalone one-command application around :func:`add_pdx`.

    Parameters
    ----------
    **kwargs
        Forwarded to :func:`add_pdx`.

    Returns
    -------
    typer.Typer
        An application whose only command is ``pdx``, so it is invoked as
        ``pdx COMMAND [ARGS]...`` rather than ``pdx pdx ...``.
    """
    app = typer.Typer(add_completion=False)
    add_pdx(app, **kwargs)
    return app


app = build_pdx_app()


def main() -> None:
    """Console-script entry point for ``pdx``."""
    with aws_errors(DEFAULT_STDERR_CONSOLE):
        app()
