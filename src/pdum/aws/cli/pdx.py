"""``pdx`` — run a command with a project's secrets in its environment.

```console
$ pdx npm run dev
$ pdx python -m my_service --port 8080
$ pdx -- ls -la
```

**``pdx`` has no options of its own.** Everything after the name is the command
to run, so no flag of yours can ever collide with one of its own, and ``--`` is
available but never required. The only exception is a leading ``--help``, which
click reserves; ``pdx python --help`` still reaches Python, because parsing
stops at the first bare word.

Configuration therefore comes entirely from the environment: the search path is
``PDUM_SSM_PATH`` if set, otherwise a ``PDUM_SSM_PATH=`` line in the nearest
``.env`` — searched upward from the working directory, so this works from
anywhere inside a project. Every secret on that path is copied into the
environment, and then the process is **replaced** by the command, a real
``execvp``: same PID, so signals, job control, exit status and the terminal all
belong to the program you asked for, with nothing left in the middle to forward
them.

Variables already set are left alone. Your shell is more specific than the
store, so ``STRIPE_KEY=sk_test_... pdx ./run-tests`` overrides for that one run,
and a CI job's injected variables are never undone. ``pdx-doctor`` shows what
that adds up to without running anything.

Only the ``PDUM_SSM_PATH`` line of a ``.env`` is read. The file says *where the
secrets live*; it is not a second source of secrets, since then a value's origin
would be ambiguous. Use ``secrets import`` to move a ``.env`` into the store.

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
from dataclasses import dataclass
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
    "load_environment",
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
    """What :func:`load_environment` did.

    Attributes
    ----------
    applied : list of str
        Names taken from the store and set in the environment.
    already_set : list of str
        Names the store holds that the environment already had, so were kept.
    skipped : list of str
        Names unusable as environment variables, e.g. a nested ``db/PASSWORD``.
    """

    applied: list[str]
    already_set: list[str]
    skipped: list[str]


def resolve_search_path(
    *,
    envvar: str | None,
    env_file: str,
    default_path: PathDefault,
    environ: Mapping[str, str],
) -> tuple[str | None, str]:
    """Work out which SSM search path to use, and say where it came from.

    The environment wins over the file, so a variable exported for one command
    overrides the project's own setting the way it would for any other tool.

    Parameters
    ----------
    envvar : str, optional
        Variable holding the path, and the key looked for in the file. ``None``
        disables both.
    env_file : str
        File to search for upward from the working directory.
    default_path : str or callable, optional
        Fallback when neither supplies one; a callable is resolved here.
    environ : mapping
        Environment to consult, normally ``os.environ``.

    Returns
    -------
    tuple of (str or None, str)
        The path, and a short description of its source for reporting.
    """
    if envvar and environ.get(envvar):
        return environ[envvar], f"${envvar}"
    if envvar:
        found = find_env_file(env_file)
        if found is not None and (value := read_env_file(found).get(envvar)):
            return value, str(found)
    resolved = default_path() if callable(default_path) else default_path
    return (resolved, "built-in default") if resolved else (None, "")


def load_environment(store: SecretStore, environ: MutableMapping[str, str]) -> LoadReport:
    """Copy *store* into *environ*, without disturbing what is already there.

    Parameters
    ----------
    store : SecretStore
        Store to read, in one bulk pass.
    environ : mutable mapping
        Environment to update, normally ``os.environ``.

    Returns
    -------
    LoadReport
        What was applied, kept and skipped.
    """
    applied: list[str] = []
    already_set: list[str] = []
    skipped: list[str] = []
    for name, value in sorted(store.read_all().items()):
        if not _ENV_NAME.match(name):
            skipped.append(name)
        elif environ.get(name):
            # Truthiness, not membership, to agree with SecretStore.get, where
            # an empty variable reads as unset.
            already_set.append(name)
        else:
            environ[name] = value
            applied.append(name)
    return LoadReport(applied, already_set, skipped)


def _no_path_message(envvar: str | None, env_file: str) -> str:
    """Explain every way the search path could have been supplied."""
    ways = [f"set {envvar}", f"add {envvar}=/myapp/:/org/ to {env_file}"] if envvar else []
    return f"[red]no SSM search path configured:[/] {', or '.join(ways) or 'this command was built without one'}"


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
        Search path used when neither the environment nor the ``.env`` supplies
        one. A callable is resolved at invocation.
    envvar : str, optional
        Environment variable holding the search path, and the key looked for in
        the ``.env``. ``None`` disables both, leaving only *default_path*.
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
        """Run a command with this project's secrets in its environment.

        Takes no options of its own: everything here is the command to run.
        Variables already set are left alone.
        """
        argv = list(command or [])
        if not argv:
            out.print("[red]nothing to run:[/] pdx takes the command to exec, e.g. [bold]pdx npm start[/]")
            raise typer.Exit(2)

        path, _ = resolve_search_path(envvar=envvar, env_file=env_file, default_path=default_path, environ=os.environ)
        if not path:
            out.print(_no_path_message(envvar, env_file))
            raise typer.Exit(2)
        try:
            store = store_factory(path)
        except ValueError as exc:
            out.print(f"[red]{exc}[/]")
            raise typer.Exit(2) from exc

        load_environment(store, os.environ)
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
    process rather than about the store: which search path resolves and *from
    where*, and which names the environment already holds — the reason a program
    run under ``pdx`` can see a value that is not the one in SSM.

    Deliberately reports names and origins only, never values: it works from
    :meth:`~pdum.aws.secrets.SecretStore.describe`, which does not decrypt, so
    running it pulls no secret over the wire. Use ``secrets get`` when the value
    is what you actually want.

    Parameters are as for :func:`add_pdx`, except that *console* defaults to
    stdout — here the report is the output.
    """
    out = console or DEFAULT_CONSOLE

    @app.command(name)
    def pdx_doctor() -> None:
        """Explain what pdx would put in the environment, without running anything."""
        path, source = resolve_search_path(
            envvar=envvar, env_file=env_file, default_path=default_path, environ=os.environ
        )
        if not path:
            out.print(_no_path_message(envvar, env_file))
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
            name_ = row["name"]
            if not _ENV_NAME.match(name_):
                verdict, style = "skip it", "yellow"
            elif os.environ.get(name_):
                verdict, style = "keep the environment's", "cyan"
            else:
                verdict, style = "set it", "green"
            counts[verdict] += 1
            table.add_row(name_, row["origin"], f"[{style}]{verdict}[/]")
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
