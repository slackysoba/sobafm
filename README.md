# SobaFM

A self-hosted Discord music bot for Linux and Docker.

- **/play** takes any request, however unusual, and uses Gemini to find the best fit in a curated catalog of existing recordings. The program continues, one recording after another without repeats, asking Gemini for more as its list runs out, until stopped, replaced or out of recordings; **/skip** moves on.
- **/freestyle** produces continuous generated music with Lyria RealTime.
- Both modes share one station per server, access controls and accurate now-playing information.
- **/settings** lets a server's managers set its volume and how often requests can change the music. The values persist across restarts.

## Development status

`/play` ranks admitted local FMA recordings in one initial Gemini call, eight choices at a time, with prepared-catalog refills. Enabling [Jamendo discovery and checked transient streaming](docs/operations.md#jamendo-discovery-and-checked-streaming) adds a validated musical-intent call and pools bounded source windows with the 167 prepared, locally hosted FMA recordings, retaining FMA fallback when Jamendo is unavailable. Jamendo is disabled by default. Discovery refills reuse intent and retain finite history/cycle/retry budgets; an empty source window is not whole-catalog exhaustion. Jamendo requires a dedicated Client ID and bounded provenance journal. [ccMixter remains disabled](docs/requirements.md#first-source-evaluation). Current-image/source/Discord qualification and musical-quality evidence remain incomplete.

Each server (guild) has one shared station: listeners in its voice channel hear the same audio. The maintainer selected one bot identity with three simultaneous stations in [#166](https://github.com/slackysoba/sobafm/issues/166). The implementation gives each station two exclusive producer/bank/copy slots, including retained cleanup debt, and refuses a fourth before interpretation. The explicit [numeric capacity contract](docs/decisions/0012-three-station-capacity.md) is accepted; actual-image qualification remains pending and offline tests do not prove three-station throughput. Broad catalog access before deployment is separately tracked in [#171](https://github.com/slackysoba/sobafm/issues/171).

The amd64 Compose slice implements continuous catalog playback, `/skip`, persisted `/settings` and idle voice rejoin after restart. Current Discord controls, automatic refill and recovery still need real integration evidence. A [short free-tier Lyria access check](https://github.com/slackysoba/sobafm/issues/3#issuecomment-6795163301) passed on the historical `01e2226` image, consuming four seconds of PCM in a 17.756-second invocation; it does not qualify current main or ongoing access. See [operations](docs/operations.md) for the historical image checks and remaining limits. Release qualification is incomplete.

The Python development toolchain and Linux CI are available. Listening comparisons are deferred optional evidence.

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

The first amd64 playback slice runs with Docker Compose. Historical images ran in two private Discord sessions on Docker Desktop under WSL2 ([#97](https://github.com/slackysoba/sobafm/issues/97) and [#124](https://github.com/slackysoba/sobafm/issues/124), the second with the real catalog and Gemini). The [reviewed corrected L2 check](https://github.com/slackysoba/sobafm/issues/3#issuecomment-6789773813) also passed its finite synthetic buffering workload on application `01e2226`, image `bd7d1aee`, with harness `37e2b8f` on Docker Desktop WSL2 amd64. These results do not qualify current main, full Discord behavior or the release; [operations](docs/operations.md) preserves the exact binding and first failed attempt. This remains a source-only build: fresh availability of the pinned Debian packages is unverified ([supply chain](docs/supply-chain.md)).

```sh
cp .env.example .env   # then replace the placeholder token and Gemini key
mkdir catalog
docker compose up --build -d
```

The catalog and audio go in `./catalog`, mounted read-only. Settings and remembered voice channels go in a named volume at `/data`, the only writable path. See [Operations](docs/operations.md) for Discord permissions, the catalog and media files, settings, voice channel memory, stopping, updates, troubleshooting and the memory scope.

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
