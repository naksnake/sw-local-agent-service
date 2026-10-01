# ADR-0019: No hosted CI; the checks run locally before a push

Status: accepted
Date: 2026-10-01

Accepted on the project owner's instruction of 2026-10-01: "remove ci", with the change
recorded here and in CLAUDE.md rather than left as a conflict with it.

## Context
`.github/workflows/ci.yml`, added in P0, ran four jobs on every pull request and every push
to `main`: `python` (ruff, the format check, `mypy --strict`, `pytest` with the 85% coverage
gate, then the credential grep over the log bundles of a 25-cycle run), `webui` (typecheck,
vitest, the production build, the Playwright smoke test), `preflight` (`install.sh` prints
its report and creates nothing) and `egress-drop` (the Python, WebUI and preflight checks
again inside `docker run --network none`). CLAUDE.md named that workflow as what enforces
INV-10, the §5.7 credential grep and §11's "egress-DROP green". The `bundle` and `deploy`
jobs of ADR-0004 were never added, so no fresh-host install ever ran in CI; the P4 job with
a real Xvfb display was never added either.

## Decision
- The repository carries no hosted CI workflow: `.github/workflows/ci.yml` is deleted. Its
  last version stays readable with `git show ef6db3b:.github/workflows/ci.yml`.
- The checks it ran are the checks before every push, run locally with the same commands
  (README, "Developing"): the locked environment, ruff, the format check, `mypy --strict`,
  `pytest` with its coverage gate, the credential grep with `python -m slas_hal.sinkcheck`,
  the WebUI typecheck, unit tests, build and smoke test, and `./install.sh
  --preflight-only` against an empty data root. `--preflight-only` replaces the workflow's
  plain `./install.sh`, which stopped after the preflight only because a hosted runner has
  no GPU; on a workstation with one it would go on to install.
- Egress-DROP is a release check: before a release, the Python, WebUI and preflight checks
  run again inside `docker run --network none`, as the removed job did. The checks
  docs/DEVELOPMENT_PLAN.md placed in CI that need more than a workstation (a GUI skill on a
  real Xvfb display in P4, the fresh-host install of ADR-0004) are release checks too.
- `tests/unit/test_repo_policy.py` keeps its rules for any workflow added later: actions
  pinned to a commit and images to a digest (INV-8). Bringing hosted CI back needs an ADR.
- Where an earlier ADR says a check runs "in CI" (0002, 0003, 0008, 0009, 0012), it now runs
  in the checks before a push, or before a release for the network-free and display runs.
  ADR-0004 is superseded; its runner design stays the recipe if a deploy test is built.

## Consequences
Easier: a push or a pull request waits on no hosted runner, and the project's checks depend
on no hosted service. Harder: nothing stops a push that skipped the checks, a pull request
shows no check results, and a reviewer relies on the author having run them; the
network-free run, the display run and the install path are proven only when someone runs
them, before a release at the latest.

## Invariants touched
What each invariant requires is unchanged; how often it is proven is what this ADR relaxes.
INV-10 now names the install tests and the preflight run before every push instead of CI.
INV-1 and INV-8 (builds succeed with networking disabled) are proven before a release
instead of on every pull request. INV-5 and INV-14: the credential grep runs with the
checks before a push.
