# WaterWoods Binance Portfolio Margin maintenance

This document is the source-of-truth maintenance policy for the standalone WaterWoods Binance
Portfolio Margin product. The formal product name is **WaterWoods Binance Portfolio Margin**. Its
repository identifiers use the full slug **`binance-portfolio-margin`**; do not invent or use a
shortened product name.

This document governs source, synchronization, CI, and release maintenance. It does not replace
the runtime safety procedure. Operators must use the
[Binance Portfolio Margin runbook](https://github.com/WaterWoods-Labs/team-freqtrade-runtime/blob/main/docs/binance-portfolio-margin-runbook.md)
for account checks, image pinning, credentials, networks, launch, reconciliation, and runtime
recovery. Do not copy credentials, credential values, runtime configuration, databases, logs, or
raw account responses into this repository or this document.

## Product and source boundary

The product source is the `binance-portfolio-margin` branch of
`WaterWoods-Labs/freqtrade`. A normal local checkout is named
`freqtrade-binance-portfolio-margin/`. Feature and fix branches start from
`origin/binance-portfolio-margin` and return to that branch through pull requests.

The product is based on official Freqtrade `develop`:

- `upstream/develop` is the official Freqtrade source.
- `origin/develop` is the WaterWoods fast-forward mirror used by product synchronization.
- The two mirror refs must agree before an upstream integration is trusted.
- Upstream integrations are merge commits reviewed independently for this product. Do not rebase
  or squash an upstream synchronization merge.

Binance Portfolio Margin/PAPI adapter changes, their configuration schema, and any required core
changes belong only on `binance-portfolio-margin`. They must not be developed or released from
`xcoin`. Conversely, this product must not contain an XCoin adapter, connector, API client,
configuration, workflow, test fixture, runtime fallback, or image reference.

The product boundary is enforced by
`scripts/validate_binance_portfolio_margin_boundary.py` and the resolver-level negative test
`tests/test_binance_portfolio_margin_boundary.py`. The negative test proves that a product
installation cannot resolve `exchange.name=xcoin`; the boundary is not merely a filename scan.
Run both checks whenever product routing or exchange resolution changes.

Never merge one product branch into the other. If both products need the same upstream change,
merge official `develop` independently into each product branch.

## Common core hook rules

`Exchange.validate_existing_positions` is an exchange-neutral extension point. Its base
implementation is intentionally a no-op. `FreqtradeBot` calls it:

1. during startup, after open-order recovery and a wallet refresh; and
2. after a completed exit, only when no remaining open trade has a pending order.

The base hook and bot call sites must not contain Binance- or XCoin-specific reconciliation logic.
The Binance Portfolio Margin override belongs in `freqtrade/exchange/binance.py`. An
exchange-specific implementation on another product branch remains owned by that branch.

Changes to the hook contract require:

- tests for the default no-op behavior and call ordering;
- tests for partial exits and pending-order deferral;
- Binance Portfolio Margin reconciliation tests;
- proof that ordinary Binance behavior is unchanged; and
- the complete upstream-compatible test suite when merged to the product branch.

Do not move PAPI routing or account-wide reconciliation into generic `Exchange` or
`FreqtradeBot` code merely to reduce the size of the Binance subclass.

## Upstream synchronization

The product synchronization branch is
`sync/binance-portfolio-margin-upstream-develop`. The repository workflow is
`.github/workflows/sync-binance-portfolio-margin.yml`.

The workflow:

1. fetches `origin/binance-portfolio-margin` and `origin/develop`;
2. exits without changes when the product already contains the mirror;
3. creates or refreshes the synchronization branch from the product branch;
4. merges `origin/develop` with `--no-ff`;
5. pushes only the disposable synchronization branch; and
6. creates a pull request targeting `binance-portfolio-margin`.

It never merges the pull request automatically. A conflict stops the workflow and requires manual
review. Conflict resolution must preserve the product boundary and must not copy a resolution from
`xcoin` without independently comparing it with official `develop`.

GitHub runs scheduled workflows only from the repository default branch. While the default branch
is `xcoin`, the same product synchronization workflow also exists there as repository-level
infrastructure. That copy explicitly operates on `binance-portfolio-margin` and does not make
XCoin source depend on PAPI. Keep the default-branch and product-branch workflow copies aligned;
change them through coordinated pull requests.

Synchronization uses the repository's organization-owned GitHub App. The workflow may reference
the configured variable and secret names, but maintainers must never print, document, or copy the
App private key or any minted installation token. Tokens are short-lived and scoped per step.

Before merging a synchronization pull request, verify mirror equality, review the complete product
delta against official `develop`, run full CI, and confirm that the boundary validator still
passes.

## Product CI

The authoritative workflow is
`.github/workflows/binance-portfolio-margin-ci.yml` and uses Python 3.12 on Ubuntu.
Pull requests targeting `binance-portfolio-margin` run this product workflow, while the upstream
`.github/workflows/ci.yml` pull-request matrix is restricted to the official `develop` and
`stable` branches. This avoids running the upstream matrix in parallel with product CI without
changing the product's focused, full, manual-dispatch, push, or release gates.

### Focused validation

Focused validation runs pre-commit, the product-boundary validator, and these suites:

- `tests/test_binance_portfolio_margin_boundary.py`
- `tests/test_configuration.py`
- `tests/exchange/test_binance.py`
- `tests/freqtradebot/test_freqtradebot.py`
- `tests/rpc/test_rpc.py`

It covers configuration, ordinary Binance behavior, PAPI routes and safety, bot behavior, RPC
behavior, and rejection of the other product.

### Full validation

The `Full upstream compatibility` job runs
`pytest --random-order -n auto`. It runs for:

- every push to `binance-portfolio-margin`, so the exact branch SHA receives a full result;
- a pull request whose head is
  `sync/binance-portfolio-margin-upstream-develop`; and
- a manual dispatch with `full=true`.

A normal feature pull request may leave full validation skipped when its scope is narrow. Run full
validation before merge whenever a change affects shared core behavior, dependency resolution,
build or packaging behavior, or more than the focused product surface.

The sole branch-protection status context is
`binance-portfolio-margin-required`. Its aggregate job requires focused validation to pass
and accepts full validation only when it either passes or is intentionally skipped. On product
pushes and synchronization pull requests, workflow conditions require the full job to run.

Do not rename the aggregate job or reuse an XCoin status context. Update the branch ruleset and
this document in the same governed change if the context must ever change.

## Governance and CODEOWNER review

`.github/CODEOWNERS` assigns every tracked path to
`@WaterWoods-Labs/waterwoods-maintainers`.

The live repository rulesets are authoritative. The intended active rules are:

- **binance-portfolio-margin-branch-safety**: blocks deletion and non-fast-forward updates,
  requires the strict `binance-portfolio-margin-required` check, requires the branch to be
  current, and requires review conversations to be resolved. It has no bypass.
- **binance-portfolio-margin-leader-approval**: requires a pull request, one approval, CODEOWNER
  review, and dismissal of stale approvals. The maintainer team has pull-request-only bypass; it
  does not bypass the safety ruleset.
- **Protect Binance Portfolio Margin release tags**: blocks deletion and non-fast-forward updates
  to `binance-portfolio-margin-*` tags.

Verify live rules before relying on them. Direct pushes, force-pushes, branch deletion, and tag
movement remain prohibited even if a temporary platform or plan limitation stops enforcing a
rule.

## Release policy

Release tags must match exactly:

`binance-portfolio-margin-YYYY.MM.DD.N`

where `N` is a positive integer without a leading zero. The release workflow is
`.github/workflows/binance-portfolio-margin-release.yml`.

Before creating a tag:

1. merge the reviewed change into `binance-portfolio-margin`;
2. wait for the product branch's push CI on that exact commit;
3. confirm both focused validation and `Full upstream compatibility` succeeded; and
4. create the next immutable annotated tag on that exact product commit.

The release workflow fails before building unless:

- the tag name matches the required pattern;
- the tag resolves to the event's exact SHA;
- the SHA is an ancestor of `origin/binance-portfolio-margin`; and
- a successful `binance-portfolio-margin-ci.yml` **push** run exists for that exact SHA
  and branch, with a successful `Full upstream compatibility` job.

A pull-request synthetic merge SHA, a run from another branch, a focused-only run, or a successful
run for a neighboring commit does not satisfy the release gate.

The release image is:

`ghcr.io/waterwoods-labs/freqtrade-binance-portfolio-margin`

The workflow publishes Linux amd64 and arm64 manifests with immutable release-tag and
`sha-<commit>` references. It enables an SBOM, maximum build provenance, and a registry
build-provenance attestation, then records the manifest digest in the GitHub release notes.

Never publish or consume `latest` for this product. Runtime deployments must use the reviewed
manifest digest:

`ghcr.io/waterwoods-labs/freqtrade-binance-portfolio-margin@sha256:<digest>`

If a released commit is defective, revert it through a reviewed product pull request, pass exact
SHA full CI, and issue a new incremented tag. Never delete, move, or reuse an existing tag.

## Recovery

Before a risky upstream integration or release change, create a local recovery branch from the
current verified `origin/binance-portfolio-margin` tip. Do not use a combined product tag or
the other product branch as a recovery base.

If synchronization fails:

- leave `binance-portfolio-margin` unchanged;
- inspect the disposable synchronization branch and failed workflow;
- resolve conflicts in a new or refreshed synchronization pull request;
- rerun focused and full validation; and
- delete the disposable branch only after its replacement and audit references are understood.

If the long-lived branch contains a bad merged change, use a reviewed revert pull request. Do not
rewrite published history. If a release workflow fails before publication, fix the cause and rerun
only after revalidating the same tag SHA; if the tag or source is wrong, preserve it and create the
next valid release after a reviewed corrective change.

Runtime recovery, position and order reconciliation, container rollback, configuration checkpoints,
and account safety remain governed exclusively by the
[runtime Portfolio Margin runbook](https://github.com/WaterWoods-Labs/team-freqtrade-runtime/blob/main/docs/binance-portfolio-margin-runbook.md).

## Legacy combined-build boundary

The immutable tags `xcoin-2026.07.30.1` and `xcoin-2026.07.31.1` are legacy
combined builds created before product separation. They contain both XCoin and Binance Portfolio
Margin/PAPI adaptations.

Retain those tags and their release records for provenance. Do not:

- delete, move, or retag them;
- use either tag as a source, recovery, or release baseline;
- deploy either XCoin image for a new Portfolio Margin workload;
- copy their PAPI code back to `xcoin`; or
- recreate them under the `binance-portfolio-margin-*` namespace.

All new Portfolio Margin source starts from `binance-portfolio-margin`, all new releases use
the product tag namespace, and all new deployments use the standalone product image by reviewed
digest.

## Maintainer verification

At minimum, a source or documentation pull request must leave these checks clean:

```sh
python scripts/validate_binance_portfolio_margin_boundary.py
pre-commit run --all-files
git diff --check
```

For source changes, also run the focused suites locally. For upstream synchronization, shared-core
changes, and release candidates, require the complete CI workflow described above. Record run and
release identifiers in project memory rather than hard-coding transient results into this policy.
