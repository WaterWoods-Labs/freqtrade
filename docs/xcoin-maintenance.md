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

The built-in `GITHUB_TOKEN` can fast-forward ordinary upstream commits. GitHub requires a separate
credential when the update changes `.github/workflows/*`. Configure the `MIRROR_SYNC_TOKEN`
repository secret with a dedicated credential limited to this repository and granted both Contents
and Workflows write permissions. Use a fine-grained personal access token with an expiration date;
do not reuse a broad personal token or store the credential in the repository. A future GitHub App
migration must mint its short-lived installation token during each workflow run rather than saving
that token as a long-lived secret.

When the secret is missing, workflow-file updates stop before the push and explain the required
configuration in the job summary. Failure issue creation is best-effort so a notification-policy
restriction cannot hide the original synchronization error.

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
