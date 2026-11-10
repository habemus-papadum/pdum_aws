# Weekly dependency maintenance

The `weekly-dependencies` workflow runs each Tuesday at 08:07 UTC and can
be dispatched manually with `gh workflow run deps-refresh.yml`. It uses the
repository's locked Ship 0.12.0 or newer and the shared refresh action. First-party
packages are selected by `[tool.ship] ours`; normal maintenance retains manifest
floors and third-party compatibility constraints.

## Verification scope

`[tool.ship] refresh-test` owns the maintenance checks independently of release tests:

    uv run --locked pytest -q

The existing offline suite covers local CLI/configuration and AWS helper contracts using fixtures and intercepted provider calls. It does not certify live AWS API behavior or provision infrastructure.

## Outcomes and recovery

The shared action verifies the baseline before refreshing. A passing candidate
commits only intended lock/Ship-skill updates, provided main has not advanced,
and dispatches ordinary CI for that new commit. No change means no commit.
A failed valid candidate retains the exact proposed changes and diagnostics for
a draft PR; a baseline, resolution or authentication failure remains a failed
run with its report. Existing human/agent PR work is preserved. This workflow
never publishes a package or deploys a service.

The same steps can be inspected locally from an isolated clean checkout:

```sh
uv run --locked ship deps check
uv run --locked ship deps verify
uv run --locked ship deps refresh --report-dir .ship-refresh
```

Use the Node/pnpm and software prerequisites from the workflow where applicable.
Local refresh restores failed changes by default; the report retains the candidate.
Do not mistake a successful maintenance suite for checks outside the scope above.

## Rollout evidence

Prepared November 10, 2026. Manual workflow verification and any follow-ups are
recorded after the shared action release and pilot have passed.

The original frozen-lock baseline passed 229 Python tests. These checks used the existing lock before the shared Ship tooling upgrade; final workflow evidence follows below.

## Manual verification, November 10

[Run 42863798658](https://github.com/habemus-papadum/pdum_aws/actions/runs/42863798658) completed successfully. The retained integration report records `unchanged` at phase `complete`, after the configured baseline installation, policy and maintenance checks.

The first-party resolution was already current, so the job made no empty commit and created no repair PR. Future candidate failures still use the shared draft-PR/report path; this run exercised the no-change path.

The shared action's failed-candidate draft-PR path was separately exercised in its [integration smoke](https://github.com/habemus-papadum/pdum_ship/actions/runs/42859986953). Repository-level Actions PR creation is enabled; the default token permission remains read unless a workflow explicitly requests more.
