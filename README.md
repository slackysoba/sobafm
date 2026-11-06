# SobaFM

A self-hosted Discord music bot for Linux and Docker.

- **/play** takes any request, however unusual, and uses Gemini to find the best fit in a curated catalog of existing recordings. The program continues, one recording after another without repeats, until stopped or replaced; **/skip** moves on.
- **/freestyle** produces continuous generated music with Lyria RealTime.
- Both modes share one station per server, access controls and accurate now-playing information.

## Development status

`/play` ranks complete recordings from prepared catalog metadata with one validated Gemini call per request. The real catalog holds 167 recordings from the Free Music Archive's research dataset, chosen by a [written rule](docs/decisions/0007-fma-dataset-batch-catalog.md). Jamendo stays parked, and [ccMixter is not an enabled audio source](docs/requirements.md#first-source-evaluation). Matching hasn't yet been evaluated against the live API. A first, not yet qualified amd64 playback slice can be run with Docker Compose: a `/play` program continues through the ranked recordings and `/skip` moves on ([operations](docs/operations.md)). Release qualification is not yet complete.

The Python development toolchain and Linux CI are available. Current work establishes source admission and a feasible Linux resource configuration. A narrow end-to-end playback implementation follows. Listening comparisons are deferred optional evidence.

## Development setup

In a Linux checkout with Git and [uv 0.13.6](https://docs.astral.sh/uv/getting-started/installation/) available:

```sh
uv python install
uv sync --locked
uv run pre-commit run --all-files
uv run pytest
```

This installs Python 3.14 and the locked development environment, then runs all-file checks and the offline tests. No credentials or audio are needed. See [Contributing](CONTRIBUTING.md) for tool choices, updates and required checks.

See what Gemini ranks for real requests, using your own API key and 20 invented songs. These are billable calls, and it [isn't part of the checks](evaluations/gemini_matching/README.md):

```sh
uv run python evaluations/gemini_matching/try_requests.py --send-to-gemini "a haunted carnival on the moon"
```

The [catalog contract](docs/catalog-evaluation.md#current-selection-contract) explains the input bounds and the separate review required for real metadata.

Normalize a saved source response into that catalog format:

```sh
uv run python -m sobafm.catalog_import ccmixter \
  --input evaluations/catalog/import-ccmixter-observed.json \
  --context evaluations/catalog/import-ccmixter-context.json \
  > ccmixter-catalog.json 2> ccmixter-import-report.json
```

This included ccMixter observation retains a missing requested ID and incomplete upstream notices, and it only demonstrates the import. The [import guide](docs/catalog-evaluation.md#saved-source-metadata-imports) also covers saved Jamendo JSON and FMA operator manifests, with synthetic fixtures for those two sources. Normalization reports evidence gaps and creates no playback admission or network activity.

## Run

The first amd64 playback slice runs with Docker Compose. It is not yet qualified: it has run in two private Discord sessions on Docker Desktop under WSL2 ([#97](https://github.com/slackysoba/sobafm/issues/97) and [#124](https://github.com/slackysoba/sobafm/issues/124), the second with the real catalog and Gemini), but neither the image, its limits nor the release is qualified.

```sh
cp .env.example .env   # then replace the placeholder token and Gemini key
mkdir data
docker compose up --build -d
```

See [Operations](docs/operations.md) for Discord permissions, the catalog and media files, stopping, updates, troubleshooting and the memory scope.

## Documentation

- [Operations](docs/operations.md)

- [Product requirements](docs/requirements.md)
- [Architecture](docs/architecture.md)
- [Engineering notes](docs/engineering-notes.md)
- [Roadmap](docs/roadmap.md)
- [Decision records](docs/decisions/README.md)
- [Contributing](CONTRIBUTING.md)
- [Agent instructions](AGENTS.md)
- [Issues](https://github.com/slackysoba/sobafm/issues)
- [Project board](https://github.com/users/slackysoba/projects/3)

## Platform

Linux amd64 is the first deployment target. Native arm64 is on hold until after the first amd64 release ([#60](https://github.com/slackysoba/sobafm/issues/60)). Docker is the supported operator path. Windows runtime support is out of scope.

## License

[MIT](LICENSE). Music and third-party components retain their own licenses and usage conditions.
