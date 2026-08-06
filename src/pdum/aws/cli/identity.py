"""The ``whoami`` command, attachable to any Typer application.

A single command rather than a group, so it is registered onto the host's app
directly instead of being added as a sub-app:

```python
import typer

from pdum.aws.cli import add_whoami

app = typer.Typer()
add_whoami(app, name="aws-identity")
```
"""

import typer
from rich.console import Console

from pdum import aws
from pdum.aws.cli.output import DEFAULT_CONSOLE

__all__ = ["add_whoami"]


def add_whoami(app: typer.Typer, *, console: Console | None = None, name: str = "whoami") -> None:
    """Register a ``whoami`` command on *app*.

    Parameters
    ----------
    app : typer.Typer
        Application to register the command on.
    console : rich.console.Console, optional
        Console to render through; defaults to
        :data:`~pdum.aws.cli.output.DEFAULT_CONSOLE`.
    name : str, default "whoami"
        Name to register the command under, in case the host already has one.
    """
    out = console or DEFAULT_CONSOLE

    @app.command(name)
    def whoami() -> None:
        """Print the AWS identity the current credentials resolve to."""
        identity = aws.whoami()
        out.print(f"[bold]account[/] {identity['Account']}", soft_wrap=True)
        out.print(f"[bold]arn[/]     {identity['Arn']}", soft_wrap=True)
