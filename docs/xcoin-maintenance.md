# WaterWoods XCoin maintenance

The `xcoin` branch is the WaterWoods XCoin-only product. It keeps upstream Freqtrade history
separate from the XCoin implementation and must not contain WaterWoods Binance Portfolio Margin
or PAPI product code.

## Product boundary

Binance Portfolio Margin/PAPI is a separate product. Its source changes start from
`origin/binance-portfolio-margin` in the separate `freqtrade-binance-portfolio-margin/` checkout
and return to that branch through their own pull requests, CI, and release process. Never start a
PAPI change from `xcoin`, merge the product branches into each other, or publish PAPI functionality
through an XCoin image.

XCoin CI enforces this boundary. The standard Binance adapter must remain aligned with the official
`develop` mirror, and WaterWoods PAPI markers are forbidden from XCoin source and tests.

## Branches

- `develop` and `stable` are read-only, fast-forward mirrors of the corresponding branches in
  `freqtrade/freqtrade`.
- `xcoin` is the default branch and the only long-lived XCoin product integration branch.
- `feature/*` and `fix/*` branches start from `xcoin` and return through pull requests.
- `sync/upstream-develop` is managed by automation and must never be merged without XCoin CI.
- `binance-portfolio-margin` is an independent product branch, not an XCoin integration branch.

Do not commit XCoin changes to `develop` or `stable`. Do not force-push any long-lived branch.

## Scheduled product workflow placement

GitHub evaluates scheduled workflows from the repository's default branch. Because `xcoin` is the
default branch, `.github/workflows/sync-binance-portfolio-margin.yml` is present there solely as
repository-level scheduling infrastructure for the independent `binance-portfolio-margin` branch.

Its presence on `xcoin` must not be interpreted as shared product code or permission to route PAPI
changes through XCoin. The workflow targets only the independent Portfolio Margin branch and its
dedicated synchronization branch.

## Upstream synchronization

`Sync official mirror branches` fast-forwards the mirror branches every day. `Prepare XCoin upstream
sync` creates or updates a pull request that merges `develop` into `xcoin` every week. A divergence
in either mirror is reported without rewriting history. Synchronization pull requests are never
merged automatically and require XCoin CI review.

The XCoin synchronization workflow may automatically resolve exactly one known conflict:
`.github/dependabot.yml`. It preserves the reviewed XCoin version of that file. Any additional or
different conflict fails closed and requires manual review; automation must not choose a resolution
for source, tests, workflows, or other policy files.

The synchronization workflows authenticate through a dedicated GitHub App installed only on
`WaterWoods-Labs/freqtrade`. The App is named `WaterWoods XCoin Mirror Sync` and the installation
grants only the repository permissions required by the synchronization:

- Contents: read and write
- Pull requests: read and write
- Workflows: read and write

The workflows store the App client ID in the `MIRROR_SYNC_APP_CLIENT_ID` repository variable and its
private key in the `MIRROR_SYNC_APP_PRIVATE_KEY` repository secret. They mint short-lived
installation tokens at runtime and request only the permission subset needed by each step:
Contents and Workflows for Git pushes, and Contents and Pull requests for pull request creation.
Installation tokens are never saved as repository secrets. Full XCoin CI dispatch continues to use
the short-lived built-in `GITHUB_TOKEN`.

The organization-level policy intentionally prevents the built-in `GITHUB_TOKEN` from creating or
approving pull requests, so synchronization pull requests also use a short-lived App installation
token. Failure issue creation remains best-effort with the built-in credential so a notification
failure cannot hide the original mirror error.

Never store the App private key in Git, documentation, chat, command output, or logs. Rotate the
private key through the organization-owned App settings, update the repository secret, validate
the official mirror, XCoin, and independent Portfolio Margin synchronization workflows, and only
then delete the previous App key.

## CI and release gates

Every pull request into `xcoin` and every push to `xcoin` runs focused XCoin validation, including
the product-boundary check. A push to `xcoin`, an automated upstream synchronization pull request,
or an explicit full dispatch also runs the complete upstream compatibility suite.

The release workflow accepts only a tag whose commit belongs to `origin/xcoin`. It then requires a
successful `XCoin CI` push run for that exact tag SHA and a successful `Full upstream compatibility`
job in that run. A successful run for another commit or a focused-only run does not satisfy the
release gate. Moving an existing release tag is prohibited and does not substitute for exact-SHA
CI evidence.

## Releases

Create an immutable annotated release tag matching `xcoin-YYYY.MM.DD.N` only after the `xcoin`
commit has passed focused and full CI plus dry-run review:

```bash
git switch xcoin
git pull --ff-only origin xcoin
git tag -a xcoin-YYYY.MM.DD.N -m "XCoin YYYY.MM.DD.N"
git push origin xcoin-YYYY.MM.DD.N
```

The release workflow publishes a multi-architecture image to
`ghcr.io/waterwoods-labs/freqtrade-xcoin`, with release-tag and commit-SHA tags, provenance, and an
image digest recorded in the release. Never move, delete, or reuse a release tag. Runtime
configuration must use the reported `ghcr.io/waterwoods-labs/freqtrade-xcoin@sha256:...` digest,
never `latest` or another mutable tag.

The immutable tags `xcoin-2026.07.30.1` and `xcoin-2026.07.31.1` are legacy combined builds from
before product separation. Retain them for provenance, but never use them for a new Binance
Portfolio Margin/PAPI deployment. New XCoin releases and images are XCoin-only; PAPI releases use
the independent product branch and image.
