# Contributing to swapsack

Contributions are mostly welcome (but do say if you've used AI or other tools).
If the length of this text scares you, skip it and just open a pull request on
GitHub. If you find it too difficult to write test code, you may skip it and
hope the maintainer fixes it.

This is a **hot wallet that broadcasts irreversible transactions**, so changes to
the signing, verify-gate, or keystore code get extra scrutiny — please include
tests for anything in those paths.

## What to include

- **Test code** covering the new behaviour or bug fix.
- **Documentation** updates where relevant.
- **A changelog entry** in `CHANGELOG.md` under `[Unreleased]`.

## Running checks

```sh
make test          # unit tests
make test-network  # live THORChain integration tests (read-only)
make lint
```

## Commit messages

Please follow [Conventional Commits](https://www.conventionalcommits.org/en/v1.0.0/)
in the imperative mood — `fix: …`, `feat: …`, `docs: …`. Older commits predate
this convention.
