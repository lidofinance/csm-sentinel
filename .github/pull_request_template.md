## Release label

Apply one release label before merging:

- `release:major` for breaking changes
- `release:minor` for backward-compatible features
- `release:patch` for fixes and internal changes that should roll into the next release

If no `release:*` label is applied, release preparation treats the PR as `release:patch`.
