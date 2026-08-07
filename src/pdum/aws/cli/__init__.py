"""``pdum-aws`` — manage SSM secrets and service quotas from the shell.

```console
$ pdum-aws whoami
$ pdum-aws secrets --path /myapp/ list
$ pdum-aws quotas status --region us-east-1
$ pdum-aws pdx-doctor
```

Installing the package also provides ``pdx``, a short form of ``pdum-aws pdx``:
it loads a project's secrets into the environment and hands the process over to
a command. See :mod:`pdum.aws.cli.pdx`.

```console
$ pdx npm run dev
```

Run inside a project, the CLI first loads the nearest ``.env`` and its
``.env.local`` overlay — the whole file, exactly as ``pdx`` reads it, never
overriding what the shell already set. A project whose ``.env`` names
``AWS_PROFILE`` and ``PDUM_SSM_PATH`` therefore gets every command bare: no
exports, no flags. Only the files are loaded, never the SSM store — fetching
secrets in order to manage secrets would be circular. ``pdx`` and
``pdx-doctor`` are excluded: they assemble the environment themselves and
report where every layer came from, and a pre-loaded ``.env`` would blur that
report.

Beyond that, credentials come from the ambient environment, exactly as in the
library. There is deliberately no ``--profile`` flag: select an account the
standard way — ``AWS_PROFILE`` in the project's ``.env`` or in the shell — so
this behaves like every other AWS tool on the box.

```console
$ AWS_PROFILE=my-account pdum-aws quotas status
```

The secrets commands need a search path — one or more SSM prefixes, colon-
separated, most specific first — which has no default: ``PDUM_SSM_PATH`` in
the ``.env`` or the shell, or ``--path`` per invocation.

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

import os

import typer

from pdum.aws.cli.identity import add_whoami
from pdum.aws.cli.output import DEFAULT_CONSOLE, DEFAULT_STDERR_CONSOLE, aws_errors, emit
from pdum.aws.cli.pdx import add_pdx, add_pdx_doctor
from pdum.aws.cli.quotas import build_quotas_app
from pdum.aws.cli.secrets import build_secrets_app
from pdum.aws.env import load_env_files

__all__ = [
    "add_pdx",
    "add_pdx_doctor",
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

app = typer.Typer(
    help="AWS utilities: SSM secrets, service quotas, and running commands with secrets loaded.",
    no_args_is_help=True,
    add_completion=False,
)
add_whoami(app, console=console)
app.add_typer(build_secrets_app(console=console), name="secrets")
app.add_typer(build_quotas_app(console=console), name="quotas")
# Diagnostics on stderr, because this one's stdout belongs to the program it execs.
add_pdx(app, console=DEFAULT_STDERR_CONSOLE)
add_pdx_doctor(app, console=console)

#: Commands that assemble the process environment themselves. They load the
#: ``.env`` pair as one layer among several and report where every variable
#: came from; loading it here first would make those variables look as though
#: the shell had set them.
_BUILDS_OWN_ENV = frozenset({"pdx", "pdx-doctor"})


@app.callback()
def _project_env(ctx: typer.Context) -> None:
    """Load the nearest ``.env`` and its overlay before the command runs.

    The whole file, exactly as ``pdx`` reads it, never overriding what the
    shell already set — so inside a project the bare commands work without
    exports. Only the files are loaded, never the SSM store: fetching secrets
    in order to manage secrets would be circular.
    """
    if ctx.invoked_subcommand not in _BUILDS_OWN_ENV:
        load_env_files(".env", os.environ)


def main() -> None:
    """Console-script entry point.

    Wraps the app so credential and API problems print one readable line instead
    of a botocore traceback — a missing profile or an expired SSO session is the
    single most common way this tool fails.
    """
    with aws_errors(console):
        app()
