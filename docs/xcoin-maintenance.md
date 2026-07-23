# WaterWoods XCoin maintenance

The WaterWoods fork keeps upstream Freqtrade history separate from the XCoin implementation.

## Branches

- `develop` and `stable` are read-only, fast-forward mirrors of the corresponding branches in
  `freqtrade/freqtrade`.
- `xcoin` is the default branch and the only long-lived XCoin integration branch.
- `feature/*` and `fix/*` branches start from `xcoin` and return through pull requests.
- `sync/upstream-develop` is managed by automation and must never be merged without XCoin CI.

Do not commit XCoin changes to `develop` or `stable`. Do not force-push any long-lived branch.

## Upstream synchronization

`Sync official mirror branches` fast-forwards the mirror branches every day. `Prepare XCoin upstream
sync` creates or updates a pull request that merges `develop` into `xcoin` every week. A divergence
in either mirror is reported without rewriting history.

Both workflows authenticate through a dedicated GitHub App installed only on
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
both synchronization workflows, and only then delete the previous App key.

## Releases

Create an immutable release tag only after the `xcoin` commit has passed CI and dry-run review:

```bash
git switch xcoin
git pull --ff-only origin xcoin
git tag -a xcoin-YYYY.MM.DD.N -m "XCoin YYYY.MM.DD.N"
git push origin xcoin-YYYY.MM.DD.N
```

The release workflow publishes a multi-architecture image to
`ghcr.io/waterwoods-labs/freqtrade-xcoin`. Runtime configuration must use the reported digest, never
`latest` or another mutable tag.
