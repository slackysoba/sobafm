# SobaFM

A self-hosted Discord bot that streams AI-generated music into voice channels.

A server manager places SobaFM in a voice channel, where it waits for requests. A member describes the music they want, such as "rainy lo-fi with soft piano" or "upbeat synthwave for a night drive." SobaFM turns the request into musical direction with [Gemini](https://ai.google.dev/gemini-api/docs) and plays it continuously with [Lyria RealTime](https://ai.google.dev/gemini-api/docs/realtime-music-generation) until the program runs its course or someone asks for something else.

## Status

[SobaFM 1.0.0](https://github.com/slackysoba/sobafm/releases/tag/v1.0.0) is available as a published container image for Linux AMD64 and ARM64, or as a source install. Release evidence and the maintainer's acceptance dispositions are recorded on [#108](https://github.com/slackysoba/sobafm/issues/108); ongoing work is tracked in the [SobaFM project](https://github.com/users/slackysoba/projects/2).

## Before you deploy

Read Google's [Gemini API Additional Terms of Service](https://ai.google.dev/gemini-api/terms) before deploying. The current terms require:

- **Age:** you must be 18 or older to use the API. You may not offer it through a service directed at, or likely to be accessed by, people under 18.
- **Region:** use is limited to [available regions](https://ai.google.dev/gemini-api/docs/available-regions). Serving members in the EEA, Switzerland, or the United Kingdom requires a Google Cloud project with active billing.
- **Data:** requests go to Google. On the free tier, Google may use prompts and responses to improve its products, and human reviewers may read them. Tell your members and keep personal or confidential information out of requests.

The project does not operate a public instance. Each operator runs their own copy with their own Discord application and Gemini API key, and is responsible for their deployment and members. The [self-hosting guide](docs/self-hosting.md#before-you-deploy) explains these notices and Google's intended-use restrictions in more detail.

## Get started

Follow the [self-hosting guide](docs/self-hosting.md) to create a Discord application, get a Gemini API key, configure SobaFM, and run version 1.0.0 with Docker Compose or from source with uv. The guide includes versioned configuration downloads, image pinning, and source and attestation verification.

The published 1.0.0 commands are described in the [versioned guide](https://github.com/slackysoba/sobafm/blob/v1.0.0/docs/self-hosting.md). The command list below describes the current source, where generated music uses `/freestyle`. After `/join`, use `/freestyle request:rainy lo-fi with soft piano`.

| Command | What it does |
| --- | --- |
| `/join` | Connects SobaFM to your voice channel, or moves it there |
| `/freestyle request:<text>` | Starts generated music or replaces it with a crossfade. Requests and the current generated plan go to Google; the resulting prompts and settings go to Lyria RealTime. No Discord identifiers are sent |
| `/play request:<text>` | When catalog configuration is present, requests catalog music: interpretation goes to Google and searches to Jamendo. Requires qualified catalog readiness; it never silently falls back to generated music |
| `/skip` | When catalog configuration is present, switches to the existing eligible ready replacement, without a fade, overlap, new model call or queue. If none is ready, the current track continues; private feedback says to try again later |
| `/stop` | Ends catalog audio immediately, or fades out generated music; SobaFM stays in the channel |
| `/now` | Privately shows the program, generated time left where applicable, and complete source-bound credits for the catalog track actually heard |
| `/settings [duration] [volume] [cooldown]` | Shows or changes the server's settings |
| `/leave` | Ends the program and disconnects |

`/join`, `/leave`, and `/settings` require Manage Server by default. `/play`, `/freestyle` and `/skip` require you to be in SobaFM's voice channel; `/stop` does too, unless you have Manage Server. Music requests and skips share the change cooldown; an unsuccessful skip releases only its own cooldown. A skip targets the track heard when the command arrives: if the track, program or voice identity changes while Discord acknowledges the command, no later track is skipped. A new request replaces the music without a queue. By default, generated programs last 60 minutes, volume is 50%, and changes have a 30-second cooldown. Music ends when the channel has had no listeners for 60 seconds.

Catalog implementation is in progress. Production trust remains empty, so configuring a Jamendo client id alone does not enable catalog playback. Actual live, resource, notice and listener-client qualification remains open in [#131](https://github.com/slackysoba/sobafm/issues/131) and [#175](https://github.com/slackysoba/sobafm/issues/175).

## Documentation

- [Self-hosting guide](docs/self-hosting.md): set up and run your own SobaFM
- [Requirements](docs/requirements.md): what v1 does
- [Architecture](docs/architecture.md): how SobaFM works
- [Roadmap](docs/roadmap.md): milestones and sequencing
- [Decision records](docs/decisions/README.md): material technical choices and their rationale
- [Security policy](SECURITY.md): report vulnerabilities privately

## Contributing

Contributions are welcome; the [contributing guide](CONTRIBUTING.md) explains how work is planned and merged, and everyone taking part follows the [code of conduct](CODE_OF_CONDUCT.md).

## License

[MIT](LICENSE)
