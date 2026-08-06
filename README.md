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

A `SecretStore` is a namespaced view of SSM Parameter Store. Reads check the
environment first, so a shell export or test fixture overrides without touching
AWS. The prefix is required — a shared default would let unrelated projects
collide in one namespace.

```python
from pdum.aws.secrets import SecretStore

store = SecretStore("/myapp/")
store.put("STRIPE_KEY", "sk_live_...")
store.get("STRIPE_KEY", required=True)
store.names()
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

Installing the package provides `pdum-aws`. There is deliberately no `--profile`
flag — pick an account the standard way, so this behaves like every other AWS
tool on the box.

```bash
AWS_PROFILE=my-account pdum-aws whoami
```

### Secrets

The prefix has no default. Pass `--prefix` or set `PDUM_SSM_PREFIX` once.

```bash
export PDUM_SSM_PREFIX=/myapp/

pdum-aws secrets list                  # names only
pdum-aws secrets list --long           # type, version, last modified
pdum-aws secrets get API_KEY           # value alone, safe to pipe
printf %s 'sk_live_...' | pdum-aws secrets set API_KEY -
pdum-aws secrets rm API_KEY
pdum-aws secrets import .secrets --dry-run
pdum-aws secrets export
```

`set` reads stdin when the value is omitted or given as `-`; prefer that, since
a value passed as an argument lands in your shell history. `import` refuses to
push AWS bootstrap keys (`AWS_ACCESS_KEY_ID`, `AWS_PROFILE`, …) — storing the
credentials you need in order to reach the store would be circular.

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

## Embedding these commands in your own CLI

Each group is produced by a factory, so another application can mount the same
commands under its own name — carrying its own defaults, rendering through its
own console. That is how you give an app a `secrets` subcommand without asking
its users to type a prefix:

```python
import typer
from rich.console import Console

from pdum.aws.cli import add_whoami, aws_errors, build_quotas_app, build_secrets_app

console = Console()

app = typer.Typer(help="acme — the whole product.", no_args_is_help=True)
add_whoami(app, console=console)
app.add_typer(
    build_secrets_app(console=console, default_prefix="/acme/", envvar="ACME_SSM_PREFIX"),
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
`[default: /acme/]` and `[env var: ACME_SSM_PREFIX]` rather than this library's.
The prefix resolves in order: `--prefix`, then the environment variable named by
`envvar`, then `default_prefix`, then an error. Pass a zero-argument callable as
`default_prefix` when the host reads it from a config file and wants that read
deferred to invocation, or `expose_prefix_option=False` to drop the flag and pin
the namespace.

`build_quotas_app` takes `default_service`, `default_targets` (a list or a
callable) and `default_regions` on the same terms, plus `show_service_option` /
`show_targets_option` to keep flags out of `--help` that a host's users have no
business changing. Both factories accept `store_factory` / `default_targets`
callables as the seam for host configuration — e.g.
`store_factory=lambda prefix: SecretStore(prefix, region=cfg.region)`.

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

### Publishing

```bash
# Build and publish to PyPI (requires credentials)
./scripts/publish.sh
```

### Automation scripts

- `./scripts/setup.sh` – bootstrap uv, pnpm, widget bundle, and pre-commit hooks
- `./scripts/build.sh` – reproduce the release build locally
- `./scripts/pre-release.sh` – run the full battery of quality checks
- `./scripts/release.sh` – orchestrate the release (creates tags, publishes to PyPI/GitHub)
- `./scripts/test_notebooks.sh` – execute demo notebooks (uses `./scripts/nb.sh` under the hood)

## License

MIT License - see LICENSE file for details.
