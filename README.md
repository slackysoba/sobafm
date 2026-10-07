# SobaFM

A self-hosted Discord bot that streams AI-generated music into voice channels.

A server manager places SobaFM in a voice channel, where it waits for requests. A member describes the music they want, such as "rainy lo-fi with soft piano" or "upbeat synthwave for a night drive." SobaFM turns the request into musical direction with [Gemini](https://ai.google.dev/gemini-api/docs) and plays it continuously with [Lyria RealTime](https://ai.google.dev/gemini-api/docs/realtime-music-generation) until the program runs its course or someone asks for something else. Each operator runs their own instance with their own Discord application and Gemini API key.

## Status

SobaFM is in design and not yet ready to run. Planning and progress are tracked in the [SobaFM project](https://github.com/users/slackysoba/projects/2).

## Documentation

- [Self-hosting guide](docs/self-hosting.md): set up and run your own SobaFM
- [Requirements](docs/requirements.md): what v1 does
- [Architecture](docs/architecture.md): how SobaFM works
- [Roadmap](docs/roadmap.md): milestones and sequencing
- [Decision records](docs/decisions/README.md): material technical choices and their rationale

## Contributing

Contributions are welcome; the [contributing guide](CONTRIBUTING.md) explains how work is planned and merged, and everyone taking part follows the [code of conduct](CODE_OF_CONDUCT.md). Report vulnerabilities privately, as described in the [security policy](SECURITY.md).

## License

[MIT](LICENSE)
