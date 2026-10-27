# SobaFM

A self-hosted Discord music bot for Linux and Docker.

- **/play** selects existing recordings from a curated catalog.
- **/freestyle** produces continuous generated music with Lyria RealTime.
- Both modes share one station per server, access controls and accurate now-playing information.

## Development status

Specification and planning are available. Application implementation, installation packages and release qualification are not yet complete.

Initial work evaluates catalog quality and establishes a feasible Linux resource configuration. The Python development toolchain and Linux CI are available. A narrow end-to-end playback implementation follows those decisions.

## Development setup

In a Linux checkout with Git and [uv 0.13.6](https://docs.astral.sh/uv/getting-started/installation/) available:

```sh
uv python install
uv sync --locked
uv run pre-commit run --all-files
uv run pytest
```

This installs Python 3.14 and the locked development environment, then runs all-file checks and the offline package smoke test. No credentials or audio are needed. See [Contributing](CONTRIBUTING.md) for tool choices, updates and required checks. There is no runnable bot or operator image yet.

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
