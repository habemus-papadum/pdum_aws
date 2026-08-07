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

The environment it hands over is built by :func:`pdum.aws.env.load_env`, in
three layers — what you already have, the nearest ``.env`` and its
``.env.local`` overlay, then the SSM store. That module is the place to read
about the ordering and why it matters; this one is only the command around it,
and a script wanting the same environment should call ``load_env`` directly
rather than shelling out to ``pdx``.

Then the process is **replaced** by the command, a real ``execvp``: same PID, so
signals, job control, exit status and the terminal all belong to the program you
asked for, with nothing left in the middle to forward them.

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
import sys
from collections.abc import Callable
from typing import Any, NoReturn

import typer
from rich.console import Console
from rich.table import Table

from pdum.aws.cli.output import DEFAULT_CONSOLE, DEFAULT_STDERR_CONSOLE, aws_errors
from pdum.aws.env import (
    DEFAULT_ENVVAR,
    NoSearchPath,
    PathDefault,
    is_env_name,
    load_env,
    load_env_files,
    no_search_path_message,
    resolve_search_path,
)
from pdum.aws.secrets import SecretStore

__all__ = ["add_pdx", "add_pdx_doctor", "build_pdx_app", "main"]

#: Click settings that keep this command's parser off the wrapped command line.
#: ``allow_interspersed_args`` stops option parsing at the first bare word, and
#: ``ignore_unknown_options`` keeps ``pdx python -c ...`` from being read as an
#: unknown ``-c`` flag. Together they make ``--`` optional rather than required.
_PASSTHROUGH = {"ignore_unknown_options": True, "allow_interspersed_args": False}

#: The codes a shell uses for these two conditions, worth matching so that
#: ``pdx some-typo`` is diagnosable exactly as a bare ``some-typo`` would be.
EXIT_NOT_FOUND = 127
EXIT_NOT_EXECUTABLE = 126


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
    envvar: str | None = DEFAULT_ENVVAR,
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
        File searched for upward from the working directory, and the base name
        of the ``.local`` overlay beside it. A name with a directory component
        is used as given.
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

        try:
            load_env(env_file=env_file, envvar=envvar, default_path=default_path, store_factory=store_factory)
        except (NoSearchPath, ValueError) as exc:
            out.print(f"[red]{exc}[/]")
            raise typer.Exit(2) from exc

        _exec(argv, out)


def add_pdx_doctor(
    app: typer.Typer,
    *,
    console: Console | None = None,
    default_path: PathDefault = None,
    envvar: str | None = DEFAULT_ENVVAR,
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
        from_file = load_env_files(env_file, os.environ)
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
            out.print(f"[red]{no_search_path_message(envvar, env_file, from_file)}[/]")
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
            if not is_env_name(secret):
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
