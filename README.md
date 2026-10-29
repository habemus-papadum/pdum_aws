# pdum.aws

[![CI](https://github.com/habemus-papadum/pdum_aws/actions/workflows/ci.yml/badge.svg)](https://github.com/habemus-papadum/pdum_aws/actions/workflows/ci.yml)
[![Coverage](https://raw.githubusercontent.com/habemus-papadum/pdum_aws/python-coverage-comment-action-data/badge.svg)](https://htmlpreview.github.io/?https://github.com/habemus-papadum/pdum_aws/blob/python-coverage-comment-action-data/htmlcov/index.html)
[![Documentation](https://img.shields.io/badge/Documentation-blue.svg)](https://habemus-papadum.github.io/pdum_aws/)

[![PyPI](https://img.shields.io/pypi/v/habemus-papadum-aws.svg)](https://pypi.org/project/habemus-papadum-aws/)
[![Python 3.14+](https://img.shields.io/badge/python-3.14+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Code style: ruff](https://img.shields.io/badge/code%20style-ruff-000000.svg)](https://github.com/astral-sh/ruff)

AWS utils

## Installation

Install using pip:

```bash
pip install habemus-papadum-aws
```

Or using uv:

```bash
uv pip install habemus-papadum-aws
```

## Usage

Nothing here hardcodes a profile, region, or account. Credentials come from
`boto3`'s own resolution, so you pick an account the standard way:

```bash
AWS_PROFILE=my-account python -m my_script
```

### Identity

```python
from pdum import aws

print(aws.whoami()["Account"])
ssm = aws.client("ssm")
```

### Secrets

A `SecretStore` is a layered view of SSM Parameter Store, built from a search
path of prefixes — most specific first, like project/user/system config files.
Reads check the environment, then each prefix in order; writes and deletes
target the first prefix only, so a project stores what is its own and inherits
the rest from shared layers. The path is required — a shared default would let
unrelated projects collide in one namespace.

```python
from pdum.aws.secrets import SecretStore

store = SecretStore("/myapp/:/org/")
store.put("STRIPE_KEY", "sk_live_...")   # written to /myapp/STRIPE_KEY
store.get("GOOGLE_CLIENT_ID")            # falls back to /org/GOOGLE_CLIENT_ID
store.names()                            # union across layers, deduplicated
```

### The project environment, from a script

`load_env()` builds the same environment `pdx` would hand a command, for a
script that was not launched under it. Use it where you would otherwise call
`dotenv.load_dotenv()`:

```python
from pdum.aws import load_env

load_env()   # os.environ now carries the project's variables and its secrets
```

It does strictly more than `load_dotenv`: the same `.env` handling, plus the
`.env.local` overlay, plus the secrets on the SSM search path that the `.env`
usually names. Three layers, most specific first — what is already set, then the
files, then the store — with the files applied *before* the store is opened,
because that is where `AWS_PROFILE` lives.

Nothing runs on import; you decide when. The call returns a report of what each
layer contributed, raises `NoSearchPath` rather than quietly skipping the
secrets, and takes `environ=` if you want an environment computed without
touching this process's own:

```python
report = load_env(environ={})            # nothing is applied to os.environ
report.secrets.applied                   # ['API_KEY', 'DATABASE_URL']
report.env_files.already_set             # names the caller had already set
```

### Service quotas

A fresh AWS account can launch almost nothing — typically 5 vCPUs of standard
on-demand EC2 and zero of every accelerator family. These helpers report on that
and request increases, returning data rather than printing so callers own
presentation.

```python
from pdum.aws import quotas

for status in quotas.report(quotas.EC2_VCPU_TARGETS, region="us-east-1"):
    print(status.target.label, status.current, status.state)

results = quotas.submit(quotas.EC2_VCPU_TARGETS, region="us-east-1")
```

`submit` is idempotent: quotas already satisfied, or already carrying an open
request, are skipped rather than resubmitted. It stops cleanly when the account
hits its undocumented cap of ~20 simultaneously open requests, so the workflow
is submit, wait for cases to be decided, submit again.

## Command line

Installing the package provides `pdum-aws`. Run inside a project, it first
loads the nearest `.env` and its `.env.local` overlay — the whole file, exactly
as `pdx` reads it, never overriding what the shell already set — so a project
whose `.env` names `AWS_PROFILE` and `PDUM_SSM_PATH` gets every command bare.
Only the files are loaded, never the store; `pdx` and `pdx-doctor` assemble
the environment themselves. There is deliberately no `--profile` flag — pick
an account the standard way, so this behaves like every other AWS tool on the
box.

```bash
AWS_PROFILE=my-account pdum-aws whoami   # outside any project
```

### Secrets

The search path has no default: `PDUM_SSM_PATH` — one or more prefixes,
colon-separated, most specific first — in the `.env` or the shell, or `--path`
per invocation.

```bash
pdum-aws secrets list                  # names only
pdum-aws secrets list --long           # type, version, last modified
pdum-aws secrets get API_KEY           # value alone, safe to pipe
printf %s 'sk_live_...' | pdum-aws secrets set API_KEY -
pdum-aws secrets rm API_KEY
pdum-aws secrets import .secrets --dry-run
pdum-aws secrets export
```

Reads fall back along the path; `set`, `rm` and `import` touch only the first
prefix, and `rm` refuses a name living solely in a fallback layer rather than
reaching down (`list --long` shows each secret's origin). `set` reads stdin
when the value is omitted or given as `-`; prefer that, since a value passed as
an argument lands in your shell history. `import` refuses to push AWS bootstrap
keys (`AWS_ACCESS_KEY_ID`, `AWS_PROFILE`, …) — storing the credentials you need
in order to reach the store would be circular.

### Quotas

```bash
pdum-aws quotas status  --region us-east-1 --region us-west-2
pdum-aws quotas history --region us-east-1        # what AWS decided
pdum-aws quotas request --region us-east-1 --dry-run
pdum-aws quotas request --region us-east-1
```

`--region` is repeatable; omit it to use whatever the environment resolves.
`quotas targets` prints the bundled EC2 plan as JSON so you can edit it and pass
it back with `--targets`:

```bash
pdum-aws quotas targets > my-targets.json
pdum-aws quotas request --targets my-targets.json
```

Do not infer a quota's value from its request status. AWS also raises limits on
young accounts automatically, independently of any request — `quotas status`
shows the applied value, which is the number that matters.

### Running a command with the secrets loaded

Installing the package also provides `pdx`, which puts a project's secrets in
the environment and hands the process over to a command:

```bash
pdx npm run dev
pdx python -m my_service --port 8080
pdx -- ls -la
```

**`pdx` takes no options of its own.** Everything after the name is the command
to run, so none of *its* flags can ever be mistaken for one of pdx's, and `--`
is available but never required. The single exception is a leading `--help`,
which click reserves; `pdx python --help` still reaches Python.

Configuration therefore comes entirely from the environment, in three layers,
most specific first:

1. **the environment you already have** — never overwritten, so
   `STRIPE_KEY=sk_test_... pdx ./run-tests` overrides for that one run, and a CI
   job's injected variables are never undone;
2. **the nearest `.env`, overlaid with an adjacent `.env.local`**, searched for
   upward from the working directory, so `pdx` works from anywhere inside a
   project;
3. **the SSM store**, on the search path named by `PDUM_SSM_PATH`.

The *whole* `.env` is loaded, not just the search path — and it is applied
before the store is opened, because that is where the credentials for reaching
the store live:

```ini
# .env, at the top of the project — committed
AWS_PROFILE=team-account
AWS_REGION=us-east-1
PDUM_SSM_PATH=/myapp/:/org/
```

```ini
# .env.local, beside it — gitignored, yours alone
AWS_PROFILE=my-sandbox
```

Only `.env` is searched for; the overlay is taken from *beside* whatever was
found, never searched for separately, so one directory always wins and there is
never a question of which `.env.local` applies. A `.env.local` with no `.env` to
anchor it is therefore ignored. The two are parsed as one document rather than
merged as two, so `BUCKET=assets-${STAGE}` in the local file can refer to a
`STAGE` defined in the committed one.

Values are interpolated, as any other `.env` consumer would, so `BUCKET=assets-${STAGE}`
works. (`secrets import` deliberately does *not* interpolate: those values are
going into permanent storage, where a silently expanded `${...}` cannot be
recovered.)

Because the file is a layer above the store, a name set in `.env` wins over the
same name in SSM — which is how you override one secret locally without touching
the shared store.

The handover is a real `execvp`, not a subprocess: the command keeps pdx's PID,
so signals, job control, exit status and the terminal belong to it directly,
with nothing left in the middle to forward them. It inherits the environment all
three layers built up.

`pdx` is a thin command around [`load_env()`](#the-project-environment-from-a-script) —
a Python script wanting the same environment should call that directly rather
than shelling out.

`pdx-doctor` shows what that adds up to without running anything:

```console
$ pdum-aws pdx-doctor
.env /work/myapp/.env + .env.local
  sets AWS_PROFILE, AWS_REGION, PDUM_SSM_PATH
  kept from the environment SHARED
search path /myapp/:/org/  (from /work/myapp/.env)
NAME      LAYER    PDX WOULD
API_KEY   /myapp/  set it
ORG_ONLY  /org/    set it
SHARED    /myapp/  keep the environment's
  2 to set it, 1 to keep the environment's
```

It answers what `secrets list` cannot, because the questions are about this
process rather than about the store: which `.env` was found and what it
contributed, which search path resolved and *from where*, and which names your
environment already holds — the reason a program run under `pdx` can see a value
that is not the one in SSM. It reports names and origins only, never values, and
never decrypts; use `secrets get` for a value.

## Embedding these commands in your own CLI

Each group is produced by a factory, so another application can mount the same
commands under its own name — carrying its own defaults, rendering through its
own console. That is how you give an app a `secrets` subcommand without asking
its users to type a search path:

```python
import typer
from rich.console import Console

from pdum.aws.cli import add_whoami, aws_errors, build_quotas_app, build_secrets_app

console = Console()

app = typer.Typer(help="acme — the whole product.", no_args_is_help=True)
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

`acme secrets list` now works bare, and `acme secrets --help` shows
`[default: /acme/:/org/]` and `[env var: ACME_SSM_PATH]` rather than this
library's. The search path resolves in order: `--path`, then the environment
variable named by `envvar`, then `default_path`, then an error. Pass a
zero-argument callable as `default_path` when the host reads it from a config
file and wants that read deferred to invocation, or `expose_path_option=False`
to drop the flag and pin the namespace.

`build_quotas_app` takes `default_service`, `default_targets` (a list or a
callable) and `default_regions` on the same terms, plus `show_service_option` /
`show_targets_option` to keep flags out of `--help` that a host's users have no
business changing. Both factories accept `store_factory` / `default_targets`
callables as the seam for host configuration — e.g.
`store_factory=lambda prefix: SecretStore(prefix, region=cfg.region)`.

`pdx` and `pdx-doctor` attach the same way, as single commands rather than
groups, and take the same `default_path` / `envvar` / `env_file` arguments:

```python
from pdum.aws.cli import add_pdx, add_pdx_doctor

add_pdx(app, default_path="/acme/:/org/", envvar="ACME_SSM_PATH")
add_pdx_doctor(app, console=console, default_path="/acme/:/org/", envvar="ACME_SSM_PATH")
```

Give `add_pdx` a console that writes to **stderr** — the default does. Its
stdout belongs to the program it execs, and a diagnostic printed there would
corrupt that program's output the moment anyone piped it.

Two details worth knowing. `aws_errors` is the wrapper that turns an expired SSO
session into one readable line instead of a botocore traceback; wrap your entry
point in it to get the same treatment. And the groups keep their per-invocation
state in `ctx.meta` under namespaced keys rather than in `ctx.obj`, so mounting
them never disturbs what your own callback stores there — a host command can
reach both, via `pdum.aws.cli.secrets.store_from(ctx)` and `ctx.obj`.

## Development

This project uses [UV](https://docs.astral.sh/uv/) for dependency management.

### Setup

```bash
# Install UV if you haven't already
curl -LsSf https://astral.sh/uv/install.sh | sh

# Clone the repository
git clone https://github.com/habemus-papadum/pdum_aws.git
cd pdum_aws

# Provision the entire toolchain (uv sync, pre-commit hooks)
./scripts/setup.sh
```

**Important for Development**:
- `./scripts/setup.sh` is idempotent—rerun it after pulling dependency changes
- Use `uv sync --frozen` to ensure the lockfile is respected when installing Python deps

### Running Tests

```bash
# Run all tests
uv run pytest

# Run a specific test file
uv run pytest tests/test_example.py

# Run a specific test function
uv run pytest tests/test_example.py::test_version

# Run tests with coverage
uv run pytest --cov=src/pdum/aws --cov-report=xml --cov-report=term
```

### Code Quality

```bash
# Check code with ruff
uv run ruff check .

# Format code with ruff
uv run ruff format .

# Fix auto-fixable issues
uv run ruff check --fix .
```

### Documentation

```bash
# Serve documentation locally (auto-reloads on changes)
uv run mkdocs serve

# Build documentation
uv run mkdocs build

# Test demo notebooks (if you have notebooks in docs/demos/)
./scripts/test_notebooks.sh
```

**Important**: After making any changes to demo notebooks, run `./scripts/test_notebooks.sh` to verify they execute without errors.

### Building

```bash
# Build Python 
./scripts/build.sh

# Or build just the Python distribution artifacts
uv build
```

### Releasing

Releases run from the `release` GitHub Actions workflow, which calls
[ship](https://github.com/habemus-papadum/pdum_ship):

```bash
# Plan first (the default), then release for real
gh workflow run release.yml -f bump=minor -f dry_run=true
gh workflow run release.yml -f bump=minor -f dry_run=false
```

Each release is tagged `habemus-papadum-aws-v<version>` on the commit it was
built from. Between releases `pyproject.toml` carries `<last release>+dev`;
`ship` sets the release version, builds, tests, publishes to PyPI, tags, and
restores the development marker. To see the plan locally:

```bash
uv run ship release habemus-papadum-aws --bump minor --dry-run
```

### Automation scripts

- `./scripts/setup.sh` – bootstrap uv and pre-commit hooks
- `./scripts/build.sh` – sync the environment the way CI does
- `./scripts/test_notebooks.sh` – execute demo notebooks (uses `./scripts/nb.sh` under the hood)

## License

MIT License - see LICENSE file for details.
