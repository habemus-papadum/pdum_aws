"""``pdum-aws`` — manage SSM secrets and service quotas from the shell.

```console
$ pdum-aws whoami
$ pdum-aws secrets --prefix /myapp/ list
$ pdum-aws quotas status --region us-east-1
```

Credentials come from the ambient environment, exactly as in the library. There
is deliberately no ``--profile`` flag: select an account the standard way, so
this behaves like every other AWS tool on the box.

```console
$ AWS_PROFILE=my-account pdum-aws quotas status
```

The secrets commands need a prefix, which has no default. Pass ``--prefix`` or
set ``PDUM_SSM_PREFIX`` once in your shell.
"""

import json
import sys
from pathlib import Path
from typing import Any

import typer
from botocore.exceptions import BotoCoreError, ClientError
from rich.console import Console
from rich.table import Table

from pdum import aws
from pdum.aws import quotas as quotas_lib
from pdum.aws.secrets import SecretStore

console = Console()
#: Values go to stdout through a separate Console with markup disabled, so a
#: secret containing square brackets is never mangled and piping stays clean.
raw = Console(markup=False, highlight=False, soft_wrap=True)

app = typer.Typer(help="AWS utilities: SSM secrets and service quotas.", no_args_is_help=True, add_completion=False)
secrets_app = typer.Typer(help="Manage secrets in AWS SSM Parameter Store.", no_args_is_help=True)
quotas_app = typer.Typer(help="Inspect and request AWS service quotas.", no_args_is_help=True)
app.add_typer(secrets_app, name="secrets")
app.add_typer(quotas_app, name="quotas")

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

#: Statuses rendered in green / red by ``quotas history``.
_GOOD = {"APPROVED"}
_BAD = {"DENIED", "NOT_APPROVED", "INVALID_REQUEST"}


@app.command()
def whoami() -> None:
    """Print the AWS identity the current credentials resolve to."""
    identity = aws.whoami()
    console.print(f"[bold]account[/] {identity['Account']}", soft_wrap=True)
    console.print(f"[bold]arn[/]     {identity['Arn']}", soft_wrap=True)


# --------------------------------------------------------------------------- #
# secrets
# --------------------------------------------------------------------------- #


@secrets_app.callback()
def _secrets(
    ctx: typer.Context,
    prefix: str = typer.Option(
        ...,
        "--prefix",
        "-p",
        envvar="PDUM_SSM_PREFIX",
        help="SSM path prefix owning these secrets, e.g. /myapp/",
    ),
) -> None:
    """Build the store every secrets subcommand works against."""
    try:
        ctx.obj = SecretStore(prefix)
    except ValueError as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(2) from exc


@secrets_app.command("list")
def secrets_list(
    ctx: typer.Context,
    long: bool = typer.Option(False, "--long", "-l", help="show type, version and last-modified"),
) -> None:
    """List secret names in the store."""
    store: SecretStore = ctx.obj
    if not long:
        for name in sorted(store.names()):
            raw.print(name)
        return

    rows = sorted(store.describe(), key=lambda r: r["name"])
    if not rows:
        console.print(f"[yellow]no secrets under {store.prefix}[/]")
        return
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
    console.print(table)


@secrets_app.command("get")
def secrets_get(ctx: typer.Context, name: str = typer.Argument(..., help="secret name, without the prefix")) -> None:
    """Print one secret's value to stdout, verbatim."""
    store: SecretStore = ctx.obj
    try:
        raw.print(store.get(name, required=True))
    except KeyError as exc:
        console.print(f"[red]{exc.args[0]}[/]")
        raise typer.Exit(1) from exc


@secrets_app.command("set")
def secrets_set(
    ctx: typer.Context,
    name: str = typer.Argument(...),
    value: str = typer.Argument(None, help="value; omit or pass '-' to read stdin"),
    plain: bool = typer.Option(False, "--plain", help="store as String rather than SecureString"),
) -> None:
    """Set a secret, taking the value from the argument or stdin.

    Prefer stdin: a value passed as an argument lands in your shell history.
    """
    store: SecretStore = ctx.obj
    if value in (None, "-"):
        value = sys.stdin.read().rstrip("\n")
    if not value:
        console.print("[red]refusing to store an empty value[/]")
        raise typer.Exit(1)
    store.put(name, value, secure=not plain)
    console.print(f"[green]set[/] {store.path(name)}")


@secrets_app.command("rm")
def secrets_rm(
    ctx: typer.Context,
    name: str = typer.Argument(...),
    yes: bool = typer.Option(False, "--yes", "-y", help="skip the confirmation prompt"),
) -> None:
    """Delete a secret from the store."""
    store: SecretStore = ctx.obj
    if not yes:
        typer.confirm(f"delete {store.path(name)}?", abort=True)
    store.delete(name)
    console.print(f"[green]deleted[/] {store.path(name)}")


@secrets_app.command("import")
def secrets_import(
    ctx: typer.Context,
    path: Path = typer.Argument(Path(".secrets"), help="a KEY=VALUE file"),
    dry_run: bool = typer.Option(False, "--dry-run", help="show what would be pushed, write nothing"),
) -> None:
    """Push secrets from a KEY=VALUE file into SSM.

    AWS bootstrap keys are skipped; see :data:`SKIP_ON_IMPORT`.
    """
    store: SecretStore = ctx.obj
    if not path.exists():
        console.print(f"[red]no such file:[/] {path}")
        raise typer.Exit(1)
    console.print(f"[dim]reading {path}[/]")

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
    console.print(f"[green]{verb}[/] {len(pushed)} secret(s): {', '.join(pushed) or '—'}")
    if skipped:
        console.print(f"[dim]skipped AWS bootstrap keys: {', '.join(skipped)}[/]")


@secrets_app.command("export")
def secrets_export(ctx: typer.Context) -> None:
    """Print every secret as KEY=VALUE lines."""
    store: SecretStore = ctx.obj
    for name in sorted(store.names()):
        raw.print(f"{name}={store.get(name, required=True)}")


# --------------------------------------------------------------------------- #
# quotas
# --------------------------------------------------------------------------- #


def _load_targets(path: Path | None) -> list[quotas_lib.QuotaTarget]:
    """Read targets from a JSON file, or fall back to the bundled EC2 plan."""
    if path is None:
        return quotas_lib.EC2_VCPU_TARGETS
    data = json.loads(path.read_text())
    return [quotas_lib.QuotaTarget(item["label"], item["code"], item["value"]) for item in data]


def _regions(given: list[str] | None) -> list[str | None]:
    """Regions to act on; ``[None]`` means whatever the environment resolves."""
    return list(given) if given else [None]


def _heading(region: str | None) -> None:
    console.print(f"\n[bold]===== {region or 'ambient region'} =====[/]")


@quotas_app.command("status")
def quotas_status(
    region: list[str] = typer.Option(None, "--region", "-r", help="region to query (repeatable)"),
    service: str = typer.Option("ec2", "--service", help="Service Quotas service code"),
    targets_file: Path = typer.Option(None, "--targets", help="JSON target list; defaults to the bundled EC2 plan"),
) -> None:
    """Show current values against target values."""
    targets = _load_targets(targets_file)
    for reg in _regions(region):
        _heading(reg)
        statuses = quotas_lib.report(targets, service_code=service, region=reg)
        table = Table(box=None, pad_edge=False)
        table.add_column("QUOTA", style="bold")
        table.add_column("NOW", justify="right")
        table.add_column("TARGET", justify="right")
        table.add_column("STATE")
        for status in statuses:
            state = status.state
            if state == "satisfied":
                rendered = "[green]satisfied[/]"
            elif state == "open":
                rendered = f"[yellow]open[/] ({status.pending_status}, asked {status.pending_value:.0f})"
            else:
                rendered = "[red]to-submit[/]"
            table.add_row(
                status.target.label,
                "—" if status.current is None else f"{status.current:.0f}",
                f"{status.target.value:.0f}",
                rendered,
            )
        console.print(table)
        pending = sum(1 for s in statuses if s.has_open_request)
        todo = sum(1 for s in statuses if s.needs_submit)
        console.print(f"  [dim]{pending} open, {todo} to submit[/]")


@quotas_app.command("history")
def quotas_history(
    region: list[str] = typer.Option(None, "--region", "-r", help="region to query (repeatable)"),
    service: str = typer.Option("ec2", "--service", help="Service Quotas service code"),
    open_only: bool = typer.Option(False, "--open", help="show only requests still awaiting a decision"),
) -> None:
    """Show what AWS decided about past quota-increase requests."""
    for reg in _regions(region):
        _heading(reg)
        requests = quotas_lib.request_history(service_code=service, region=reg)
        if open_only:
            requests = [r for r in requests if r.is_open]
        if not requests:
            console.print("  [dim]no requests[/]")
            continue

        table = Table(box=None, pad_edge=False)
        table.add_column("QUOTA", style="bold")
        table.add_column("ASKED", justify="right")
        table.add_column("STATUS")
        table.add_column("UPDATED")
        table.add_column("CASE")
        for request in requests:
            if request.status in _GOOD:
                status = f"[green]{request.status}[/]"
            elif request.status in _BAD:
                status = f"[red]{request.status}[/]"
            else:
                status = f"[yellow]{request.status}[/]"
            table.add_row(
                request.name or request.code,
                f"{request.desired:.0f}",
                status,
                request.last_updated.strftime("%Y-%m-%d %H:%M") if request.last_updated else "—",
                request.case_id or "—",
            )
        console.print(table)

        counts: dict[str, int] = {}
        for request in requests:
            counts[request.status] = counts.get(request.status, 0) + 1
        console.print("  [dim]" + ", ".join(f"{n} {s}" for s, n in sorted(counts.items())) + "[/]")


@quotas_app.command("request")
def quotas_request(
    region: list[str] = typer.Option(None, "--region", "-r", help="region to act on (repeatable)"),
    service: str = typer.Option("ec2", "--service", help="Service Quotas service code"),
    targets_file: Path = typer.Option(None, "--targets", help="JSON target list; defaults to the bundled EC2 plan"),
    dry_run: bool = typer.Option(False, "--dry-run", help="show what would be requested, write nothing"),
) -> None:
    """Request increases for every target that needs one.

    Idempotent: quotas already satisfied, or already carrying an open request,
    are skipped. Stops when the account hits its open-request cap, so re-run
    once earlier cases are decided.
    """
    targets = _load_targets(targets_file)
    for reg in _regions(region):
        _heading(reg)
        results = quotas_lib.submit(targets, service_code=service, region=reg, dry_run=dry_run)
        capped = False
        for result in results:
            if result.outcome == "skipped":
                continue
            if result.outcome == "cap-reached":
                capped = True
                console.print(f"  [yellow]{result.target.label}[/] -> blocked: account is at its open-request cap")
                break
            colour = {"submitted": "green", "dry-run": "cyan", "error": "red"}[result.outcome]
            detail = f" {result.error}" if result.error else ""
            console.print(f"  [{colour}]{result.outcome}[/] {result.target.label} -> {result.target.value:.0f}{detail}")
        submitted = sum(1 for r in results if r.outcome in {"submitted", "dry-run"})
        skipped = sum(1 for r in results if r.outcome == "skipped")
        console.print(f"  [dim]{submitted} submitted, {skipped} skipped[/]")
        if capped:
            console.print("  [dim]re-run once some cases are decided[/]")


@quotas_app.command("targets")
def quotas_targets() -> None:
    """Print the bundled EC2 target plan as JSON, ready to edit and pass to --targets."""
    payload: list[dict[str, Any]] = [
        {"label": t.label, "code": t.code, "value": t.value} for t in quotas_lib.EC2_VCPU_TARGETS
    ]
    raw.print(json.dumps(payload, indent=2))


def main() -> None:
    """Console-script entry point.

    Wraps the app so credential and API problems print one readable line instead
    of a botocore traceback — a missing profile or an expired SSO session is the
    single most common way this tool fails.
    """
    try:
        app()
    except (BotoCoreError, ClientError) as exc:
        console.print(f"[red]{exc}[/]")
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
