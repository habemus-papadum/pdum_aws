"""Console plumbing shared by every command group.

Each group is produced by a factory taking a ``console``, so a host CLI can pass
its own themed :class:`~rich.console.Console` and have these commands render
through it — one theme, one width, one output stream for the whole application.
Omitting it falls back to :data:`DEFAULT_CONSOLE`.

```python
from rich.console import Console

from pdum.aws.cli import build_secrets_app

app.add_typer(build_secrets_app(console=Console(stderr=True)), name="secrets")
```
"""

from collections.abc import Iterator
from contextlib import contextmanager

from botocore.exceptions import BotoCoreError, ClientError
from rich.console import Console

__all__ = ["DEFAULT_CONSOLE", "aws_errors", "emit"]

#: Used by any group built without an explicit ``console``. A module-level
#: singleton rather than a fresh Console per factory call, so that two groups
#: built separately still share one width and one output stream.
DEFAULT_CONSOLE = Console()


def emit(console: Console, text: str) -> None:
    """Print *text* verbatim, as data rather than as a message.

    Markup, syntax highlighting and wrapping are all disabled for this one call,
    so a secret containing square brackets is never mangled, a long value is
    never broken across lines, and piping stays clean. Use this for anything a
    caller might redirect into a file or another process; use
    ``console.print`` directly for messages addressed to a human.

    Parameters
    ----------
    console : rich.console.Console
        Console to write through.
    text : str
        The value to write.
    """
    console.print(text, markup=False, highlight=False, soft_wrap=True)


@contextmanager
def aws_errors(console: Console | None = None) -> Iterator[None]:
    """Turn credential and API failures into one readable line.

    A missing profile or an expired SSO session is the single most common way
    these commands fail, and a botocore traceback is a poor way to say so. Host
    applications embedding a group should wrap their own entry point in this to
    get the same treatment:

    ```python
    def main() -> None:
        with aws_errors(my_console):
            app()
    ```

    Parameters
    ----------
    console : rich.console.Console, optional
        Console to report through; defaults to :data:`DEFAULT_CONSOLE`.

    Raises
    ------
    SystemExit
        With status 2, in place of the original ``botocore`` exception.
    """
    try:
        yield
    except (BotoCoreError, ClientError) as exc:
        (console or DEFAULT_CONSOLE).print(f"[red]{exc}[/]")
        raise SystemExit(2) from exc
