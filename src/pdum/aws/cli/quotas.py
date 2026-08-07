"""The ``quotas`` command group, as a factory other applications can embed.

:func:`build_quotas_app` mirrors :func:`~pdum.aws.cli.secrets.build_secrets_app`:
a fresh :class:`typer.Typer` per call, rendering through a console the host
supplies, with the defaults its users should not have to type. Where the secrets
group needs a search path, this one needs a service code, a target plan and a
set of regions:

```python
import typer

from pdum.aws.cli import build_quotas_app

app = typer.Typer()
app.add_typer(
    build_quotas_app(
        default_service="ec2",
        default_targets=ACME_GPU_TARGETS,
        default_regions=["us-east-1", "us-west-2"],
    ),
    name="quotas",
)
```

``acme quotas status`` now reports the plan that application cares about, in the
regions it runs in. The flags remain available to override any of it; pass
``show_service_option=False`` / ``show_targets_option=False`` to keep them out
of ``--help`` when the host's users have no business changing them.
"""

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.table import Table

from pdum.aws import quotas as quotas_lib
from pdum.aws.cli.output import DEFAULT_CONSOLE, emit

__all__ = ["TargetsDefault", "build_quotas_app"]

#: A target plan supplied by the host: literal, deferred, or absent — absent
#: meaning :data:`~pdum.aws.quotas.EC2_VCPU_TARGETS`.
TargetsDefault = list[quotas_lib.QuotaTarget] | Callable[[], list[quotas_lib.QuotaTarget]] | None

#: Statuses rendered in green / red by ``quotas history``.
_GOOD = {"APPROVED"}
_BAD = {"DENIED", "NOT_APPROVED", "INVALID_REQUEST"}


def _load_targets_file(path: Path) -> list[quotas_lib.QuotaTarget]:
    """Read a target plan from a JSON file, as emitted by ``quotas targets``."""
    data = json.loads(path.read_text())
    return [quotas_lib.QuotaTarget(item["label"], item["code"], item["value"]) for item in data]


def _heading(console: Console, region: str | None) -> None:
    """Separate one region's output from the next."""
    console.print(f"\n[bold]===== {region or 'ambient region'} =====[/]")


def _status_table(statuses: list[quotas_lib.QuotaStatus]) -> Table:
    """Render current values against target values."""
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
    return table


def _history_table(requests: list[quotas_lib.QuotaRequest]) -> Table:
    """Render what AWS decided about past requests."""
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
    return table


def build_quotas_app(
    *,
    console: Console | None = None,
    default_service: str = "ec2",
    default_targets: TargetsDefault = None,
    default_regions: list[str] | None = None,
    show_service_option: bool = True,
    show_targets_option: bool = True,
    help: str = "Inspect and request AWS service quotas.",
) -> typer.Typer:
    """Build a ``quotas`` command group.

    Parameters
    ----------
    console : rich.console.Console, optional
        Console every command in the group renders through; defaults to
        :data:`~pdum.aws.cli.output.DEFAULT_CONSOLE`.
    default_service : str, default "ec2"
        Service Quotas service code used when ``--service`` is absent.
    default_targets : list of QuotaTarget or callable, optional
        Target plan used when ``--targets`` is absent. Pass a zero-argument
        callable to defer building it. ``None`` means
        :data:`~pdum.aws.quotas.EC2_VCPU_TARGETS`, which is an example to copy
        and edit rather than a recommendation — a host with a real workload
        should pass its own.
    default_regions : list of str, optional
        Regions acted on when ``--region`` is absent. ``None`` means the single
        region ``boto3`` resolves from the environment.
    show_service_option : bool, default True
        Whether ``--service`` appears in ``--help``. It keeps working either
        way; hiding it is for hosts whose plan only makes sense for one service.
    show_targets_option : bool, default True
        Whether ``--targets`` appears in ``--help``, on the same terms.
    help : str
        Group help text.

    Returns
    -------
    typer.Typer
        A new application, ready to pass to ``add_typer``.
    """
    out = console or DEFAULT_CONSOLE
    app = typer.Typer(help=help, no_args_is_help=True)

    def resolve_targets(path: Path | None) -> list[quotas_lib.QuotaTarget]:
        """Targets from the flag, else the host's plan, else the bundled one."""
        if path is not None:
            return _load_targets_file(path)
        if default_targets is None:
            # Read late so the module attribute stays the single source of truth.
            return quotas_lib.EC2_VCPU_TARGETS
        return default_targets() if callable(default_targets) else default_targets

    def resolve_regions(given: list[str] | None) -> list[str | None]:
        """Regions to act on; ``[None]`` means whatever the environment resolves."""
        return list(given) if given else [None]

    def region_option(verb: str) -> Any:
        return typer.Option(
            list(default_regions) if default_regions else None,
            "--region",
            "-r",
            help=f"region to {verb} (repeatable)",
        )

    def service_option() -> Any:
        return typer.Option(
            default_service,
            "--service",
            hidden=not show_service_option,
            help="Service Quotas service code",
        )

    def targets_option() -> Any:
        source = "the bundled EC2 plan" if default_targets is None else "this application's plan"
        return typer.Option(
            None,
            "--targets",
            hidden=not show_targets_option,
            help=f"JSON target list; defaults to {source}",
        )

    @app.command("status")
    def quotas_status(
        region: list[str] = region_option("query"),
        service: str = service_option(),
        targets_file: Path = targets_option(),
    ) -> None:
        """Show current values against target values."""
        targets = resolve_targets(targets_file)
        for reg in resolve_regions(region):
            _heading(out, reg)
            statuses = quotas_lib.report(targets, service_code=service, region=reg)
            out.print(_status_table(statuses))
            pending = sum(1 for s in statuses if s.has_open_request)
            todo = sum(1 for s in statuses if s.needs_submit)
            out.print(f"  [dim]{pending} open, {todo} to submit[/]")

    @app.command("history")
    def quotas_history(
        region: list[str] = region_option("query"),
        service: str = service_option(),
        open_only: bool = typer.Option(False, "--open", help="show only requests still awaiting a decision"),
    ) -> None:
        """Show what AWS decided about past quota-increase requests."""
        for reg in resolve_regions(region):
            _heading(out, reg)
            requests = quotas_lib.request_history(service_code=service, region=reg)
            if open_only:
                requests = [r for r in requests if r.is_open]
            if not requests:
                out.print("  [dim]no requests[/]")
                continue

            out.print(_history_table(requests))
            counts: dict[str, int] = {}
            for request in requests:
                counts[request.status] = counts.get(request.status, 0) + 1
            out.print("  [dim]" + ", ".join(f"{n} {s}" for s, n in sorted(counts.items())) + "[/]")

    @app.command("request")
    def quotas_request(
        region: list[str] = region_option("act on"),
        service: str = service_option(),
        targets_file: Path = targets_option(),
        dry_run: bool = typer.Option(False, "--dry-run", help="show what would be requested, write nothing"),
    ) -> None:
        """Request increases for every target that needs one.

        Idempotent: quotas already satisfied, or already carrying an open
        request, are skipped. Stops when the account hits its open-request cap,
        so re-run once earlier cases are decided.
        """
        targets = resolve_targets(targets_file)
        for reg in resolve_regions(region):
            _heading(out, reg)
            results = quotas_lib.submit(targets, service_code=service, region=reg, dry_run=dry_run)
            capped = False
            for result in results:
                if result.outcome == "skipped":
                    continue
                if result.outcome == "cap-reached":
                    capped = True
                    out.print(f"  [yellow]{result.target.label}[/] -> blocked: account is at its open-request cap")
                    break
                colour = {"submitted": "green", "dry-run": "cyan", "error": "red"}[result.outcome]
                detail = f" {result.error}" if result.error else ""
                out.print(f"  [{colour}]{result.outcome}[/] {result.target.label} -> {result.target.value:.0f}{detail}")
            submitted = sum(1 for r in results if r.outcome in {"submitted", "dry-run"})
            skipped = sum(1 for r in results if r.outcome == "skipped")
            out.print(f"  [dim]{submitted} submitted, {skipped} skipped[/]")
            if capped:
                out.print("  [dim]re-run once some cases are decided[/]")

    @app.command("targets")
    def quotas_targets() -> None:
        """Print this group's default target plan as JSON, ready to edit and pass to --targets."""
        payload: list[dict[str, Any]] = [
            {"label": t.label, "code": t.code, "value": t.value} for t in resolve_targets(None)
        ]
        emit(out, json.dumps(payload, indent=2))

    return app
