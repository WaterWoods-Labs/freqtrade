# WaterWoods Binance Portfolio Margin agent instructions

## Source of truth

- This repository's standalone Binance Portfolio Margin product integration branch is
  `binance-portfolio-margin`; it is not the separate WaterWoods exchange product and not an
  ordinary Binance FAPI integration.
- Read `docs/binance-portfolio-margin-maintenance.md` before changing product code,
  synchronization workflows, CI, dependencies, branches, tags, releases, or images.
- Runtime operations are governed separately by the reviewed runbook in
  `WaterWoods-Labs/team-freqtrade-runtime`; source editing does not authorize runtime access.

## Product boundary

- Put Binance Portfolio Margin/PAPI adapter and related core changes on a feature or fix branch
  created from `origin/binance-portfolio-margin` and return them through a pull request to that
  branch.
- Never add code, configuration, fixtures, workflow behavior, or image references from the other
  WaterWoods product to this branch. Never route PAPI changes through another product branch.
- Integrate official `develop` independently through the reviewed synchronization branch. Never
  merge the two WaterWoods product branches into one another.
- Keep exchange-neutral hooks generic. Binance/PAPI routing and reconciliation belong in the
  Binance implementation, not generic core code.

## Working method

- Use a task-specific worktree. Before editing, verify the branch, remotes, working tree, mirror
  equality, and upstream relation.
- Preserve unrelated changes. Do not stash, reset, rebase, clean, force-push, or resolve an
  upstream synchronization conflict unless the current request explicitly authorizes that work.
- Do not commit, push, create or merge a pull request, tag, release, or dispatch workflow without
  explicit current authorization.
- Follow the existing Python, Ruff, pytest, and upstream Freqtrade conventions. Add focused tests
  for behavior changes and prove ordinary Binance behavior remains unchanged.

## Execution environment

- On operator workstations, run Python in the project's ephemeral non-trading Docker container.
  Use native Windows Git, GitHub authentication and filesystem tools; no host Python or venv
  without an explicit exception. Use the shared runtime Windows/container procedure for validation.
- Existing task authorization remains valid after interruption within its confirmed scope.

## Validation

- Run `python scripts/validate_binance_portfolio_margin_boundary.py` and
  `pytest tests/test_binance_portfolio_margin_boundary.py` for every product-boundary change.
- Run the focused Binance/configuration/bot/RPC suites described in the maintenance guide when the
  relevant surfaces change; run full upstream compatibility for shared core, packaging,
  dependency, synchronization, or release candidates.
- Require the policy-named exact target-SHA CI before any release decision.

## Secrets and runtime safety

- Never read, print, copy, or commit exchange credentials, GitHub tokens, private keys, live
  configs, databases, logs, reservation state, or raw account responses.
- Do not start, stop, restart, inspect inside, or redeploy trading services unless the current user
  request explicitly authorizes the exact runtime operation.
- Runtime images use reviewed immutable digests. A source change does not authorize an image build,
  pull, pin update, release, or deployment.
