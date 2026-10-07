# ADR-0005: Release images with GitHub Actions and artifact attestations

- **Status:** Accepted
- **Date:** 2026-10-07
- **Issue:** #102

## Context

SobaFM 1.0 ships as a container image that operators pull instead of building ([OPS-4](../requirements.md#operation), [USE-3](../requirements.md#responsible-use)). M4's exit criteria ask that tagging a release publishes `linux/amd64` and `linux/arm64` images with provenance attestations and an SBOM, and that release notes are generated from labels ([#4](https://github.com/slackysoba/sobafm/issues/4)). The image itself is built and checked on every pull request ([#103](https://github.com/slackysoba/sobafm/issues/103)); this record decides how a tag becomes a published, verifiable release.

An operator who pulls an image should be able to check that it was built from this repository's source by this repository's workflow, and the maintainer should run no release step by hand.

## Requirements

1. Images are built only from a tag, by a workflow with least-privilege permissions and actions pinned by commit SHA.
2. Both architectures are published, with provenance and an SBOM, and an operator can verify them with documented commands.
3. No new vendor, paid service, or maintained tool beyond GitHub, and no credential beyond the workflow's own token.
4. Release notes are generated from the labels that pull requests already carry, without a commit-message convention the repository does not use.
5. Only the maintainer can create a release tag.

## Options considered

1. **GitHub's first-party actions.** A workflow triggered by a `v*` tag builds and pushes with `docker/build-push-action`, which attaches BuildKit's provenance and SPDX SBOM to the image index. `actions/attest` signs a build provenance attestation for the image digest and stores it in the registry. `gh release create --generate-notes`, configured by `.github/release.yml`, groups merged pull requests by label.
   - Fit: covers every requirement with platform capabilities and Docker's own actions, which CI already uses to build the image.
   - Security: signing uses the workflow's OIDC identity, so there is no signing key to hold. The permissions are `contents: write`, `packages: write`, `id-token: write`, and `attestations: write`, on that one job.
   - Cost and operations: no new service. GHCR is free for a public repository.
   - Lock-in: attestations are verifiable with the `gh` CLI and use the Sigstore and in-toto formats, so they are not specific to GitHub's UI.
2. **A release tool such as release-please or semantic-release.** It would automate version bumps and changelogs.
   - Fit: it expects conventional-commit messages, but SobaFM squash-merges pull requests with free-form titles and uses labels. Adopting it would change the contribution rules for a benefit that one tag a release does not need.
   - Operations: a new maintained dependency, and a release pull request to manage.
3. **Build and push by hand from the maintainer's machine.** Simple, but nothing proves where an image came from, so it fails requirement 2.

## Decision

Option 1.

- **Trigger.** A `release.yml` workflow runs on a pushed tag matching `v*`. It first checks that the tag points at a commit on `main`, and that the tag equals the version in `pyproject.toml` with a leading `v`. A tag that fails either check publishes nothing. A tag ruleset on `v*` lets only the maintainer create and blocks updates and deletion, so a published tag cannot move.
- **Build.** One job sets up QEMU and Buildx, logs in to GHCR with the workflow token, and builds `linux/amd64,linux/arm64` with `provenance: mode=max` and `sbom: true`. CI has already shown that the same build works under QEMU, so no native arm64 runner is added.
- **Tags.** A release tag `v1.2.3` publishes `1.2.3`, `1.2`, and `latest`. A pre-release tag such as `v1.0.0-rc.1` publishes only its own version tag, so `latest` always means the newest release.
- **Attestation.** `actions/attest` attests the pushed image digest and pushes the attestation to the registry. The self-hosting guide documents `gh attestation verify oci://ghcr.io/slackysoba/sobafm:<version> --repo slackysoba/sobafm`.
- **Release notes.** The workflow creates the GitHub release with generated notes. `.github/release.yml` groups pull requests by label: security, the `area:` labels, and dependencies, with the remainder under other changes. A pre-release is marked as one.
- **Visibility.** The package is public.

The SobaFM-specific surface is the `release.yml` workflow, the label configuration, and the tag ruleset, which `docs/repository-settings.md` documents with the command that applies it.

## Consequences

- Releasing is `git tag` and `git push` of one tag; everything after is automatic and checked.
- The release image is built from the tagged commit, but its first run in the soak test (#107) happens after publication. A pre-release tag is the way to run that test before `v1.0.0`.
- QEMU makes the arm64 build slower than a native runner. The release is rare, so the time is acceptable.
- Release notes are only as good as pull-request labels. A pull request without an `area:` label lands under other changes.
- Base images and actions are pinned and updated by Dependabot, so a release picks up the pins from `main` at the tag.
- The tag must equal the project version, so the version bump is a pull request before each release.

## Revisit triggers

- Releases become frequent enough that a tool to automate versions and changelogs pays for itself.
- QEMU build times exceed the workflow's timeout, which would call for native arm64 runners.
- GitHub changes how attestations are stored or verified, or `actions/attest` is replaced.
- The project publishes to a second registry.
