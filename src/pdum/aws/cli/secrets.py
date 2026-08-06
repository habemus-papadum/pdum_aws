"""The ``secrets`` command group, as a factory other applications can embed.

:func:`build_secrets_app` returns a fresh :class:`typer.Typer` each call, so a
host CLI can mount these commands under its own name, render them through its
own console, and — the point of the exercise — supply the SSM prefix its users
should not have to type:

```python
import typer

from pdum.aws.cli import build_secrets_app

app = typer.Typer()
app.add_typer(build_secrets_app(default_prefix="/acme/", envvar="ACME_SSM_PREFIX"), name="secrets")
```

``acme secrets list`` now works bare. The prefix resolves in this order:

1. ``--prefix`` on the command line;
2. the environment variable named by *envvar*;
3. *default_prefix*, which may be a callable when the host reads it from a
   config file and wants that read deferred to invocation time;
4. otherwise an error — the library itself ships no default, because a shared
   one would let unrelated projects collide in one namespace.

Pass ``expose_prefix_option=False`` to drop the flag entirely and pin the
namespace to *default_prefix*.
"""

import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.table import Table

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

#: A prefix supplied by the host: literal, deferred, or absent.
PrefixDefault = str | Callable[[], str | None] | None


def store_from(ctx: typer.Context) -> SecretStore:
    """Return the store this invocation resolved.

    Useful to host applications adding their own commands to a group built by
    :func:`build_secrets_app`, which then read the same prefix as the rest.

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


def _flag_default(default_prefix: PrefixDefault) -> Any:
    """Default for the ``--prefix`` option itself.

    With no host default the option stays *required*, which keeps Click's own
    "Missing option" error and the ``[required]`` marker in ``--help`` — an
    environment variable still satisfies it. A literal default is passed
    straight through so ``--help`` shows it. A callable cannot be rendered into
    help, so the option becomes optional and resolution happens at call time.
    """
    if default_prefix is None:
        return ...
    return None if callable(default_prefix) else default_prefix


def _describe_table(rows: list[dict[str, Any]]) -> Table:
    """Render ``SecretStore.describe`` output as a table."""
    table = Table(box=None, pad_edge=False)
    table.add_column("NAME", style="bold")
    table.add_column("TYPE")
    table.add_column("VER", justify="right")
    table.add_column("MODIFIED")
    for row in rows:
        table.add_row(
            row["name"],
            "secure" if row["type"] == "SecureString" else "plain",
            str(row["version"]),
            row["modified"].strftime("%Y-%m-%d %H:%M"),
        )
    return table


def build_secrets_app(
    *,
    console: Console | None = None,
    default_prefix: PrefixDefault = None,
    envvar: str | None = "PDUM_SSM_PREFIX",
    expose_prefix_option: bool = True,
    store_factory: Callable[[str], SecretStore] = SecretStore,
    help: str = "Manage secrets in AWS SSM Parameter Store.",
) -> typer.Typer:
    """Build a ``secrets`` command group.

    Parameters
    ----------
    console : rich.console.Console, optional
        Console every command in the group renders through; defaults to
        :data:`~pdum.aws.cli.output.DEFAULT_CONSOLE`.
    default_prefix : str or callable, optional
        SSM prefix to use when neither the flag nor the environment supplies
        one. Pass a zero-argument callable to defer resolving it — a host
        reading the prefix from a config file wants that read to happen on
        invocation, not on import.
    envvar : str, optional
        Environment variable consulted before *default_prefix*. Pass ``None``
        to disable environment lookup entirely.
    expose_prefix_option : bool, default True
        Whether to offer ``--prefix``. ``False`` removes the flag, pinning the
        group to *default_prefix*.
    store_factory : callable, default :class:`~pdum.aws.secrets.SecretStore`
        Called with the resolved prefix to build the store. Override to thread
        host configuration through, e.g.
        ``lambda prefix: SecretStore(prefix, region=cfg.region)``.
    help : str
        Group help text.

    Returns
    -------
    typer.Typer
        A new application, ready to pass to ``add_typer``.
    """
    out = console or DEFAULT_CONSOLE
    app = typer.Typer(help=help, no_args_is_help=True)

    def configure(ctx: typer.Context, prefix: str | None) -> None:
        """Resolve the prefix and put the store where subcommands find it."""
        if not prefix:
            prefix = default_prefix() if callable(default_prefix) else default_prefix
        if not prefix:
            source = f" or set {envvar}" if envvar else ""
            out.print(f"[red]no SSM prefix configured: pass --prefix{source}[/]")
            raise typer.Exit(2)
        try:
            ctx.meta[STORE_KEY] = store_factory(prefix)
        except ValueError as exc:
            out.print(f"[red]{exc}[/]")
            raise typer.Exit(2) from exc

    if expose_prefix_option:

        @app.callback()
        def _configure(
            ctx: typer.Context,
            prefix: str = typer.Option(
                _flag_default(default_prefix),
                "--prefix",
                "-p",
                envvar=envvar,
                help="SSM path prefix owning these secrets, e.g. /myapp/",
            ),
        ) -> None:
            """Build the store every secrets subcommand works against."""
            configure(ctx, prefix)

    else:

        @app.callback()
        def _configure_fixed(ctx: typer.Context) -> None:
            """Build the store every secrets subcommand works against."""
            configure(ctx, None)

    @app.command("list")
    def secrets_list(
        ctx: typer.Context,
        long: bool = typer.Option(False, "--long", "-l", help="show type, version and last-modified"),
    ) -> None:
        """List secret names in the store."""
        store = store_from(ctx)
        if not long:
            for name in sorted(store.names()):
                emit(out, name)
            return

        rows = sorted(store.describe(), key=lambda r: r["name"])
        if not rows:
            out.print(f"[yellow]no secrets under {store.prefix}[/]")
            return
        out.print(_describe_table(rows))

    @app.command("get")
    def secrets_get(
        ctx: typer.Context, name: str = typer.Argument(..., help="secret name, without the prefix")
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
        """Set a secret, taking the value from the argument or stdin.

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
        """Delete a secret from the store."""
        store = store_from(ctx)
        if not yes:
            typer.confirm(f"delete {store.path(name)}?", abort=True)
        store.delete(name)
        out.print(f"[green]deleted[/] {store.path(name)}")

    @app.command("import")
    def secrets_import(
        ctx: typer.Context,
        path: Path = typer.Argument(Path(".secrets"), help="a KEY=VALUE file"),
        dry_run: bool = typer.Option(False, "--dry-run", help="show what would be pushed, write nothing"),
    ) -> None:
        """Push secrets from a KEY=VALUE file into SSM.

        AWS bootstrap keys are skipped; see :data:`SKIP_ON_IMPORT`.
        """
        store = store_from(ctx)
        if not path.exists():
            out.print(f"[red]no such file:[/] {path}")
            raise typer.Exit(1)
        out.print(f"[dim]reading {path}[/]")

        pushed: list[str] = []
        skipped: list[str] = []
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = (part.strip() for part in line.split("=", 1))
            value = value.strip("'\"")
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
        """Print every secret as KEY=VALUE lines."""
        store = store_from(ctx)
        for name in sorted(store.names()):
            emit(out, f"{name}={store.get(name, required=True)}")

    return app
