"""The ``secrets`` command group, as a factory other applications can embed.

:func:`build_secrets_app` returns a fresh :class:`typer.Typer` each call, so a
host CLI can mount these commands under its own name, render them through its
own console, and — the point of the exercise — supply the SSM search path its
users should not have to type:

```python
import typer

from pdum.aws.cli import build_secrets_app

app = typer.Typer()
app.add_typer(build_secrets_app(default_path="/acme/:/org/", envvar="ACME_SSM_PATH"), name="secrets")
```

``acme secrets list`` now works bare. The search path resolves in this order:

1. ``--path`` on the command line;
2. the environment variable named by *envvar*;
3. *default_path*, which may be a callable when the host reads it from a
   config file and wants that read deferred to invocation time;
4. otherwise an error — the library itself ships no default, because a shared
   one would let unrelated projects collide in one namespace.

A path is one or more SSM prefixes, colon-separated, most specific first —
reads fall back along it, writes and deletes target the first prefix only
(see :mod:`pdum.aws.secrets`). Pass ``expose_path_option=False`` to drop the
flag entirely and pin the namespace to *default_path*.
"""

import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.table import Table

from pdum.aws.cli.dotenv import read_env_file
from pdum.aws.cli.output import DEFAULT_CONSOLE, emit
from pdum.aws.secrets import SecretStore

__all__ = ["SKIP_ON_IMPORT", "STORE_KEY", "build_secrets_app", "store_from"]

#: Key under which the group callback stashes the store for its subcommands.
#:
#: ``ctx.meta`` rather than ``ctx.obj``: meta is one dict shared by the entire
#: Click context stack, so mounting this group inside a host application never
#: shadows whatever that application put in ``ctx.obj`` for its own commands.
STORE_KEY = "pdum.aws.secrets.store"

#: Keys never pushed into the store by ``secrets import``. Putting AWS
#: credentials into a store you need AWS credentials to read would be circular.
SKIP_ON_IMPORT = frozenset(
    {
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "AWS_DEFAULT_REGION",
        "AWS_REGION",
        "AWS_PROFILE",
    }
)

#: A search path supplied by the host: literal, deferred, or absent.
PathDefault = str | Callable[[], str | None] | None


def store_from(ctx: typer.Context) -> SecretStore:
    """Return the store this invocation resolved.

    Useful to host applications adding their own commands to a group built by
    :func:`build_secrets_app`, which then read the same search path as the rest.

    Parameters
    ----------
    ctx : typer.Context
        Context of a command inside the group.

    Returns
    -------
    SecretStore
        The store built by the group's callback.
    """
    return ctx.meta[STORE_KEY]


def _flag_default(default_path: PathDefault) -> Any:
    """Default for the ``--path`` option itself.

    With no host default the option stays *required*, which keeps Click's own
    "Missing option" error and the ``[required]`` marker in ``--help`` — an
    environment variable still satisfies it. A literal default is passed
    straight through so ``--help`` shows it. A callable cannot be rendered into
    help, so the option becomes optional and resolution happens at call time.
    """
    if default_path is None:
        return ...
    return None if callable(default_path) else default_path


def _describe_table(rows: list[dict[str, Any]], *, show_origin: bool) -> Table:
    """Render ``SecretStore.describe`` output as a table.

    Shadowed rows — reachable layers a more specific one hides — render dim,
    so the listing shows both what resolves and what it eclipses.
    """
    table = Table(box=None, pad_edge=False)
    table.add_column("NAME", style="bold")
    table.add_column("TYPE")
    table.add_column("VER", justify="right")
    table.add_column("MODIFIED")
    if show_origin:
        table.add_column("ORIGIN")
    for row in rows:
        cells = [
            row["name"],
            "secure" if row["type"] == "SecureString" else "plain",
            str(row["version"]),
            row["modified"].strftime("%Y-%m-%d %H:%M"),
        ]
        if show_origin:
            cells.append(row["origin"] + (" (shadowed)" if row["shadowed"] else ""))
        table.add_row(*cells, style="dim" if row["shadowed"] else None)
    return table


def build_secrets_app(
    *,
    console: Console | None = None,
    default_path: PathDefault = None,
    envvar: str | None = "PDUM_SSM_PATH",
    expose_path_option: bool = True,
    store_factory: Callable[[str], SecretStore] = SecretStore,
    help: str = "Manage secrets in AWS SSM Parameter Store.",
) -> typer.Typer:
    """Build a ``secrets`` command group.

    Parameters
    ----------
    console : rich.console.Console, optional
        Console every command in the group renders through; defaults to
        :data:`~pdum.aws.cli.output.DEFAULT_CONSOLE`.
    default_path : str or callable, optional
        SSM search path to use when neither the flag nor the environment
        supplies one. Pass a zero-argument callable to defer resolving it — a
        host reading the path from a config file wants that read to happen on
        invocation, not on import.
    envvar : str, optional
        Environment variable consulted before *default_path*. Pass ``None``
        to disable environment lookup entirely.
    expose_path_option : bool, default True
        Whether to offer ``--path``. ``False`` removes the flag, pinning the
        group to *default_path*.
    store_factory : callable, default :class:`~pdum.aws.secrets.SecretStore`
        Called with the resolved path to build the store. Override to thread
        host configuration through, e.g.
        ``lambda path: SecretStore(path, region=cfg.region)``.
    help : str
        Group help text.

    Returns
    -------
    typer.Typer
        A new application, ready to pass to ``add_typer``.
    """
    out = console or DEFAULT_CONSOLE
    app = typer.Typer(help=help, no_args_is_help=True)

    def configure(ctx: typer.Context, path: str | None) -> None:
        """Resolve the search path and put the store where subcommands find it."""
        if not path:
            path = default_path() if callable(default_path) else default_path
        if not path:
            source = f" or set {envvar}" if envvar else ""
            out.print(f"[red]no SSM search path configured: pass --path{source}[/]")
            raise typer.Exit(2)
        try:
            ctx.meta[STORE_KEY] = store_factory(path)
        except ValueError as exc:
            out.print(f"[red]{exc}[/]")
            raise typer.Exit(2) from exc

    if expose_path_option:

        @app.callback()
        def _configure(
            ctx: typer.Context,
            path: str = typer.Option(
                _flag_default(default_path),
                "--path",
                "-p",
                envvar=envvar,
                help="SSM search path: colon-separated prefixes, most specific first, e.g. /myapp/:/org/",
            ),
        ) -> None:
            """Build the store every secrets subcommand works against."""
            configure(ctx, path)

    else:

        @app.callback()
        def _configure_fixed(ctx: typer.Context) -> None:
            """Build the store every secrets subcommand works against."""
            configure(ctx, None)

    @app.command("list")
    def secrets_list(
        ctx: typer.Context,
        long: bool = typer.Option(False, "--long", "-l", help="show type, version, last-modified and origin"),
    ) -> None:
        """List secret names visible on the search path."""
        store = store_from(ctx)
        if not long:
            for name in sorted(store.names()):
                emit(out, name)
            return

        rows = sorted(store.describe(), key=lambda r: (r["name"], r["shadowed"]))
        if not rows:
            out.print(f"[yellow]no secrets under {':'.join(store.prefixes)}[/]")
            return
        out.print(_describe_table(rows, show_origin=len(store.prefixes) > 1))

    @app.command("get")
    def secrets_get(
        ctx: typer.Context, name: str = typer.Argument(..., help="secret name, without any prefix")
    ) -> None:
        """Print one secret's value to stdout, verbatim."""
        store = store_from(ctx)
        try:
            emit(out, store.get(name, required=True))
        except KeyError as exc:
            out.print(f"[red]{exc.args[0]}[/]")
            raise typer.Exit(1) from exc

    @app.command("set")
    def secrets_set(
        ctx: typer.Context,
        name: str = typer.Argument(...),
        value: str = typer.Argument(None, help="value; omit or pass '-' to read stdin"),
        plain: bool = typer.Option(False, "--plain", help="store as String rather than SecureString"),
    ) -> None:
        """Set a secret in the first prefix, taking the value from the argument or stdin.

        Prefer stdin: a value passed as an argument lands in your shell history.
        """
        store = store_from(ctx)
        if value in (None, "-"):
            value = sys.stdin.read().rstrip("\n")
        if not value:
            out.print("[red]refusing to store an empty value[/]")
            raise typer.Exit(1)
        store.put(name, value, secure=not plain)
        out.print(f"[green]set[/] {store.path(name)}")

    @app.command("rm")
    def secrets_rm(
        ctx: typer.Context,
        name: str = typer.Argument(...),
        yes: bool = typer.Option(False, "--yes", "-y", help="skip the confirmation prompt"),
    ) -> None:
        """Delete a secret from the first prefix.

        A secret living only in a fallback layer is refused, naming where it
        is: deleting from a shared layer takes an explicit ``--path`` there.
        """
        store = store_from(ctx)
        if not yes:
            typer.confirm(f"delete {store.path(name)}?", abort=True)
        try:
            store.delete(name)
        except KeyError as exc:
            out.print(f"[red]{exc.args[0]}[/]")
            raise typer.Exit(1) from exc
        out.print(f"[green]deleted[/] {store.path(name)}")

    @app.command("import")
    def secrets_import(
        ctx: typer.Context,
        path: Path = typer.Argument(Path(".secrets"), help="a KEY=VALUE file"),
        dry_run: bool = typer.Option(False, "--dry-run", help="show what would be pushed, write nothing"),
    ) -> None:
        """Push secrets from a KEY=VALUE file into the first prefix.

        AWS bootstrap keys are skipped; see :data:`SKIP_ON_IMPORT`.
        """
        store = store_from(ctx)
        if not path.exists():
            out.print(f"[red]no such file:[/] {path}")
            raise typer.Exit(1)
        out.print(f"[dim]reading {path}[/]")

        pushed: list[str] = []
        skipped: list[str] = []
        for key, value in read_env_file(path).items():
            if key in SKIP_ON_IMPORT:
                skipped.append(key)
                continue
            if not value:
                continue
            if not dry_run:
                store.put(key, value)
            pushed.append(key)

        verb = "would import" if dry_run else "imported"
        out.print(f"[green]{verb}[/] {len(pushed)} secret(s): {', '.join(pushed) or '—'}")
        if skipped:
            out.print(f"[dim]skipped AWS bootstrap keys: {', '.join(skipped)}[/]")

    @app.command("export")
    def secrets_export(ctx: typer.Context) -> None:
        """Print every visible secret as KEY=VALUE lines, resolved along the path."""
        store = store_from(ctx)
        for name in sorted(store.names()):
            emit(out, f"{name}={store.get(name, required=True)}")

    return app
