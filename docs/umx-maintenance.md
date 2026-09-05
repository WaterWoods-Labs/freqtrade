# WaterWoods UMX maintenance

The `umx` branch is the WaterWoods UMX-only product. It keeps upstream Freqtrade history
separate from the UMX implementation and must not contain WaterWoods Binance Portfolio Margin
or PAPI product code.

## Product boundary

Binance Portfolio Margin/PAPI is a separate product. Its source changes start from
`origin/binance-portfolio-margin` in the separate `freqtrade-binance-portfolio-margin/` checkout
and return to that branch through their own pull requests, CI, and release process. Never start a
PAPI change from `umx`, merge the product branches into each other, or publish PAPI functionality
through a UMX image.

UMX CI enforces this boundary. The standard Binance adapter must remain aligned with the official
`develop` mirror, and WaterWoods PAPI markers are forbidden from UMX source and tests.

## Branches

- `develop` and `stable` are read-only, fast-forward mirrors of the corresponding branches in
  `freqtrade/freqtrade`.
- `umx` is the default branch and the only long-lived UMX product integration branch.
- `feature/*` and `fix/*` branches start from `umx` and return through pull requests.
- `sync/umx-upstream-develop` is managed by automation and must never be merged without UMX CI.
- `binance-portfolio-margin` is an independent product branch, not a UMX integration branch.

Do not commit UMX changes to `develop` or `stable`. Do not force-push any long-lived branch.

The scheduled and release workflows require the protected `umx` branch to be the repository
default. UMX CI targets only `umx`. The completed branch migration is recorded in
[the product cutover](https://github.com/WaterWoods-Labs/freqtrade/pull/44) and
[the CI cutover](https://github.com/WaterWoods-Labs/freqtrade/pull/45).

## Adapter contract

The [UMX adapter guide](umx-adapter.md) is the maintained contract for capabilities,
configuration, endpoint mapping, and trading behavior. Update that guide when the implementation
changes; keep validation and release procedures here.

## Scheduled product workflow placement

GitHub evaluates scheduled workflows from the repository's default branch. Because `umx` is the
default branch, `.github/workflows/sync-binance-portfolio-margin.yml` is present there solely as
repository-level scheduling infrastructure for the independent `binance-portfolio-margin` branch.

Its presence on `umx` must not be interpreted as shared product code or permission to route PAPI
changes through UMX. The workflow targets only the independent Portfolio Margin branch and its
dedicated synchronization branch.

## Upstream synchronization

`Sync official mirror branches` fast-forwards the mirror branches every day. `Prepare UMX upstream
sync` creates or updates a pull request that merges `develop` into `umx` every week. A divergence
in either mirror is reported without rewriting history. Synchronization pull requests are never
merged automatically and require UMX CI review.

The UMX synchronization workflow may automatically resolve exactly one known conflict:
`.github/dependabot.yml`. It preserves the reviewed UMX version of that file. Any additional or
different conflict fails closed and requires manual review; automation must not choose a resolution
for source, tests, workflows, or other policy files.

The synchronization workflows authenticate through the existing organization-level mirror-sync
GitHub App installation for `WaterWoods-Labs/freqtrade`. Do not infer or document a new display
name from the repository credentials alone. Before renaming, creating, reinstalling, or revoking
an App or installation, verify its current display name, App and installation IDs, repository
scope, permissions, and an exact workflow run in the live organization settings. The installation
grants only the repository permissions required by the synchronization:

- Contents: read and write
- Pull requests: read and write
- Workflows: read and write

The workflows store the App client ID in the `MIRROR_SYNC_APP_CLIENT_ID` repository variable and its
private key in the `MIRROR_SYNC_APP_PRIVATE_KEY` repository secret. They mint short-lived
installation tokens at runtime and request only the permission subset needed by each step:
Contents and Workflows for Git pushes, and Contents and Pull requests for pull request creation.
Installation tokens are never saved as repository secrets. Full UMX CI dispatch continues to use
the short-lived built-in `GITHUB_TOKEN`.

The organization-level policy intentionally prevents the built-in `GITHUB_TOKEN` from creating or
approving pull requests, so synchronization pull requests also use a short-lived App installation
token. Failure issue creation remains best-effort with the built-in credential so a notification
failure cannot hide the original mirror error.

Never store the App private key in Git, documentation, chat, command output, or logs. Rotate the
private key through the organization-owned App settings, update the repository secret, validate
the official mirror, UMX, and independent Portfolio Margin synchronization workflows, and only
then delete the previous App key.

## CI and release gates

Every pull request into `umx` and every push to `umx` runs focused UMX validation, including
the product-boundary check. A push to `umx`, an automated upstream synchronization pull request,
or an explicit full dispatch also runs the complete upstream compatibility suite.

The release workflow accepts only a tag whose commit belongs to `origin/umx`. It then requires a
successful `UMX CI` push run for that exact tag SHA and a successful `Full upstream compatibility`
job in that run. A successful run for another commit or a focused-only run does not satisfy the
release gate. Moving an existing release tag is prohibited and does not substitute for exact-SHA
CI evidence.

For local adapter changes, run `pytest tests/exchange/test_umx.py` and relevant static checks in
the workspace's ephemeral Docker validation environment. Core changes also require their related
regression tests. Keep software tests with their source; strategy research, account probes, and
acceptance reports belong in the workspace's runtime-data and artifact directories.

## Releases

Create an immutable annotated release tag matching `umx-YYYY.MM.DD.N` only after the `umx`
commit has passed focused and full CI plus dry-run review:

```bash
git switch umx
git pull --ff-only origin umx
git tag -a umx-YYYY.MM.DD.N -m "UMX YYYY.MM.DD.N"
git push origin umx-YYYY.MM.DD.N
```

The release workflow publishes a multi-architecture image to
`ghcr.io/waterwoods-labs/freqtrade-umx`, with release-tag and commit-SHA tags, provenance, and an
image digest recorded in the release. Never move, delete, or reuse a release tag. Runtime
configuration must use the reported `ghcr.io/waterwoods-labs/freqtrade-umx@sha256:...` digest,
never `latest` or another mutable tag.

The immutable tags `xcoin-2026.07.30.1` and `xcoin-2026.07.31.1`, and their
`ghcr.io/waterwoods-labs/freqtrade-xcoin` images, are historical artifacts from before the hard
cut. Retain those names for provenance; never retag them as UMX or use them for a new deployment.
Keep the existing `xcoin-*` tag ruleset permanently for those immutable historical tags and the
separate `umx-*` ruleset for current releases. New UMX releases and images use only the `umx-*` and
`freqtrade-umx` names. PAPI releases use the independent product branch and image.
