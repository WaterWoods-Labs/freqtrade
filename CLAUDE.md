# WaterWoods UMX Claude Code instructions

## Source of truth

- This repository's UMX product integration branch is `umx`; it is not the official Freqtrade
  `develop` or `stable` mirror and not Binance Portfolio Margin.
- Read `docs/umx-maintenance.md` before changing product code, synchronization workflows, CI,
  dependencies, branches, tags, releases, or images.
- For generic Freqtrade behavior, consult the repository documentation and compare the current
  official upstream source. Recheck volatile GitHub facts instead of relying on saved notes.

## Product boundary

- Put UMX adapter and related core changes on a feature or fix branch created from
  `origin/umx` and return them through a pull request to `umx`.
- Never add Binance Portfolio Margin/PAPI code, configuration, fixtures, workflow behavior, or
  image references to this product.
- Treat `develop` and `stable` as read-only WaterWoods mirrors of official Freqtrade. Do not develop
  on them or merge `binance-portfolio-margin` into `umx`.

## Working method

- Use a task-specific worktree. Before editing, verify the branch, remotes, working tree, and
  upstream relation.
- Preserve unrelated changes. Do not stash, reset, rebase, clean, force-push, or resolve an
  upstream synchronization conflict unless the current request explicitly authorizes that work.
- Do not commit, push, create or merge a pull request, tag, release, or dispatch workflow without
  explicit current authorization.
- Keep Python code compatible with the version declared in `pyproject.toml`, follow the existing
  Ruff and test conventions, and add focused tests for behavior changes.

## Validation

- Run `pytest tests/exchange/test_umx.py` for focused adapter changes.
- Run the relevant broader tests and pre-commit checks when core, configuration, packaging, or
  workflow behavior changes.
- Preserve the UMX-only boundary enforced by `.github/workflows/umx-ci.yml` and require exact
  target-SHA CI before any release decision.

## Secrets and runtime safety

- Never read, print, copy, or commit exchange credentials, GitHub tokens, private keys, live
  configs, databases, logs, or raw account data.
- Do not start, stop, restart, inspect inside, or redeploy trading services unless the current user
  request explicitly authorizes the exact runtime operation.
- Runtime images use reviewed immutable digests. A source change does not authorize an image build,
  pull, pin update, release, or deployment.
