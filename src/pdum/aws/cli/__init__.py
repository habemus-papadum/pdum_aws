"""``pdum-aws`` — manage SSM secrets and service quotas from the shell.

```console
$ pdum-aws whoami
$ pdum-aws secrets --path /myapp/ list
$ pdum-aws quotas status --region us-east-1
```

Credentials come from the ambient environment, exactly as in the library. There
is deliberately no ``--profile`` flag: select an account the standard way, so
this behaves like every other AWS tool on the box.

```console
$ AWS_PROFILE=my-account pdum-aws quotas status
```

The secrets commands need a search path — one or more SSM prefixes, colon-
separated, most specific first — which has no default. Pass ``--path`` or set
``PDUM_SSM_PATH`` once in your shell.

Embedding these commands elsewhere
----------------------------------
Every group here is produced by a factory, so another application can mount the
same commands under its own name, carrying its own defaults and rendering
through its own console. That is the supported way to give an application a
``secrets`` subcommand without asking its users to type a search path:

```python
import typer
from rich.console import Console

from pdum.aws.cli import add_whoami, aws_errors, build_quotas_app, build_secrets_app

console = Console()
app = typer.Typer()
add_whoami(app, console=console)
app.add_typer(
    build_secrets_app(console=console, default_path="/acme/:/org/", envvar="ACME_SSM_PATH"),
    name="secrets",
)
app.add_typer(
    build_quotas_app(console=console, default_targets=ACME_TARGETS, default_regions=["us-east-1"]),
    name="quotas",
)


def main() -> None:
    with aws_errors(console):
        app()
```

See :func:`~pdum.aws.cli.secrets.build_secrets_app` and
:func:`~pdum.aws.cli.quotas.build_quotas_app` for the full set of knobs. The
groups keep their per-invocation state in ``ctx.meta`` under namespaced keys
rather than in ``ctx.obj``, so mounting them never disturbs what a host app
stores there for its own commands.
"""

import typer

from pdum.aws.cli.identity import add_whoami
from pdum.aws.cli.output import DEFAULT_CONSOLE, aws_errors, emit
from pdum.aws.cli.quotas import build_quotas_app
from pdum.aws.cli.secrets import build_secrets_app

__all__ = [
    "add_whoami",
    "app",
    "aws_errors",
    "build_quotas_app",
    "build_secrets_app",
    "console",
    "emit",
    "main",
]

#: Console this CLI's own commands render through. Embedding applications pass
#: their own to the factories instead.
console = DEFAULT_CONSOLE

app = typer.Typer(help="AWS utilities: SSM secrets and service quotas.", no_args_is_help=True, add_completion=False)
add_whoami(app, console=console)
app.add_typer(build_secrets_app(console=console), name="secrets")
app.add_typer(build_quotas_app(console=console), name="quotas")


def main() -> None:
    """Console-script entry point.

    Wraps the app so credential and API problems print one readable line instead
    of a botocore traceback — a missing profile or an expired SSO session is the
    single most common way this tool fails.
    """
    with aws_errors(console):
        app()
