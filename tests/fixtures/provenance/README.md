# Platform provenance regression fixtures

These are minimal Buildx `.Provenance` excerpts, not full SLSA statements. Buildx exposes each platform's predicate under `SLSA`, without the enclosing statement's `predicateType`. The validator checks platform coverage and the schema-specific build-type field; it does not replace signed index identity verification or validate the entire SLSA schema.

- `buildkit-v1.json`: retained fields from anonymous `docker buildx imagetools inspect --format '{{json .Provenance}}'` of the [actual rc.1 candidate](https://github.com/slackysoba/sobafm/issues/104), index `sha256:e5d9f929ff7e5c800f3096a374068125dccd190982fff3f3f587d636a1d5e039`, run 37582773159 attempt 1. Both platforms were inspected during #145 implementation; unrelated build inputs and metadata are omitted.
- `buildkit-v0.2.json`: adapted from the genuine predicate fields in [Docker's documented provenance example](https://docs.docker.com/build/metadata/attestations/slsa-provenance/#provenance-attestation-example), wrapped in the multi-platform Buildx inspection shape. This is documented compatibility evidence, not a measured v0.2 SobaFM candidate run.

[BuildKit's SLSA definitions](https://github.com/moby/buildkit/blob/master/docs/attestations/slsa-definitions.md) specify `buildType` for v0.2 and `buildDefinition.buildType` for v1. Regression tests execute the validator command taken from the release workflow, using these excerpts and malformed variants.
