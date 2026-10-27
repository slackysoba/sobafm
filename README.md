# SobaFM

A self-hosted Discord music bot for Linux and Docker.

- **/play** selects existing recordings from a curated catalog.
- **/freestyle** produces continuous generated music with Lyria RealTime.
- Both modes share one station per server, access controls and accurate now-playing information.

## Development status

The offline catalog selector compares prepared Jamendo, ccMixter and Free Music Archive metadata, prioritizing strong prompt matches and complete recordings. Application playback, installation packages and release qualification are not yet complete.

The Python development toolchain and Linux CI are available. Current work establishes source admission and a feasible Linux resource configuration. A narrow end-to-end playback implementation follows. Listening comparisons are deferred optional evidence.

## Development setup

In a Linux checkout with Git and [uv 0.13.6](https://docs.astral.sh/uv/getting-started/installation/) available:

```sh
uv python install
uv sync --locked
uv run pre-commit run --all-files
uv run pytest
```

This installs Python 3.14 and the locked development environment, then runs all-file checks and the offline tests. No credentials or audio are needed. See [Contributing](CONTRIBUTING.md) for tool choices, updates and required checks. There is no runnable bot or operator image yet.

Try the selector with explicitly synthetic metadata:

```sh
uv run python -m sobafm.catalog_demo "Quiet solo piano, no singing" \
  --catalog evaluations/catalog/selector-demo.json \
  --reviews evaluations/catalog/selector-demo-reviews.json --limit 3
```

The output includes ranked matches, complete supplied credits, rejections and coverage gaps for all three sources. It performs no network or audio activity. The [selection contract](docs/catalog-evaluation.md#current-selection-contract) explains the input bounds, limited prompt grammar and separate review required for real metadata.

## Documentation

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

Linux amd64 and native arm64 are the deployment targets. Docker is the supported operator path. Windows runtime support is out of scope.

## License

[MIT](LICENSE). Music and third-party components retain their own licenses and usage conditions.
