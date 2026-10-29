"""Test-session settings that must be in place before the package is imported."""

import os

# The CLI builds its rich consoles at import time and reads COLUMNS then. On CI
# runners the long temporary paths otherwise wrap table cells and split strings
# such as ".env + .env.local" across lines, which the CLI tests assert on.
os.environ.setdefault("COLUMNS", "200")
