"""Reading ``.env`` files, for the CLI layer only.

The library never reads ``.env`` — see :mod:`pdum.aws.secrets` — because that
would put filesystem access behind a plain ``import``. Commands are a different
matter: ``secrets import`` reads such a file by definition, and ``pdx`` looks in
one for the search path to use. Applications load ``.env``; libraries do not.

Both go through ``python-dotenv`` rather than a hand-rolled ``KEY=VALUE`` split,
because the format has more corners than it appears to: ``export`` prefixes,
inline comments, single versus double quoting, and quoted values spanning
several lines. A naive split reads

```ini
PDUM_SSM_PATH=/myapp/:/org/   # the project's own layer
```

as a prefix with a comment glued onto the end, and SSM would take that at face
value.

Interpolation of ``${VAR}`` is the caller's choice, because the two callers want
opposite things. Loading a project's environment should expand, as every other
``.env`` consumer does. Importing into the store should not: a value on its way
into permanent storage must arrive exactly as written, or the password
``p@ss${word}word`` silently becomes ``p@ssword`` and nothing records that it
ever said more.
"""

from pathlib import Path

from dotenv import dotenv_values, find_dotenv

__all__ = ["find_env_file", "read_env_file"]


def read_env_file(path: Path | str, *, interpolate: bool = True) -> dict[str, str]:
    """Read *path* as a ``.env`` file.

    Parameters
    ----------
    path : pathlib.Path or str
        File to read.
    interpolate : bool, default True
        Expand ``${VAR}`` against the environment and earlier entries, as
        ``python-dotenv`` does by default. Pass ``False`` when the values are
        headed somewhere permanent; see the module docstring.

    Returns
    -------
    dict of str to str
        Names mapped to values. A bare name declared with no ``=`` at all is
        dropped rather than reported as ``None``, so every value here is a
        string the caller can use directly.
    """
    values = dotenv_values(path, interpolate=interpolate)
    return {key: value for key, value in values.items() if value is not None}


def find_env_file(name: str = ".env") -> Path | None:
    """Find *name* in the working directory or the nearest ancestor holding one.

    Searching upward is what lets ``pdx`` run from anywhere inside a project and
    still find that project's file, the way ``git`` finds its repository. A
    *name* with a directory component is taken literally instead, so an explicit
    ``--env-file ../other/.env`` is never second-guessed.

    Parameters
    ----------
    name : str, default ".env"
        File name to search for, or a path to use as given.

    Returns
    -------
    pathlib.Path or None
        The file found, or ``None`` when there is none.
    """
    candidate = Path(name)
    if candidate.parent != Path():
        return candidate if candidate.is_file() else None
    # usecwd=True is essential: python-dotenv otherwise walks up from the file
    # that called it, which here would be this library rather than the project.
    found = find_dotenv(candidate.name, usecwd=True)
    return Path(found) if found else None
