"""Support ``python -m pdum.aws.cli`` alongside the ``pdum-aws`` console script.

Needed because :mod:`pdum.aws.cli` is a package: the ``__name__ == "__main__"``
guard that served when it was a single module no longer runs.
"""

from pdum.aws.cli import main

main()
