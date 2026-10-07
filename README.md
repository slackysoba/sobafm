# SobaFM

A self-hosted Discord bot that streams AI-generated music into voice channels.

A server manager places SobaFM in a voice channel, where it waits for requests. A member describes the music they want, such as "rainy lo-fi with soft piano" or "upbeat synthwave for a night drive." SobaFM turns the request into musical direction with [Gemini](https://ai.google.dev/gemini-api/docs) and plays it continuously with [Lyria RealTime](https://ai.google.dev/gemini-api/docs/realtime-music-generation) until the program runs its course or someone asks for something else.

## Status and demo

SobaFM's core playback and request features are implemented. Version 1.0 has not been released; release validation and remaining work are tracked in the [SobaFM project](https://github.com/users/slackysoba/projects/2).

**Demo pending ([#106](https://github.com/slackysoba/sobafm/issues/106)):** record a short clip or GIF in the maintainer's Discord server showing `/play` starting music and a second request changing it with a crossfade, then embed it here. A recording is needed before this task is complete.

## Before you deploy

Read Google's [Gemini API Additional Terms of Service](https://ai.google.dev/gemini-api/terms) before deploying. The current terms require:

- **Age:** you must be 18 or older to use the API. You may not offer it through a service directed at, or likely to be accessed by, people under 18.
- **Region:** use is limited to [available regions](https://ai.google.dev/gemini-api/docs/available-regions). Serving members in the EEA, Switzerland, or the United Kingdom requires a Google Cloud project with active billing.
- **Data:** requests go to Google. On the free tier, Google may use prompts and responses to improve its products, and human reviewers may read them. Tell your members and keep personal or confidential information out of requests.

The project does not operate a public instance. Each operator runs their own copy with their own Discord application and Gemini API key, and is responsible for their deployment and members. The [self-hosting guide](docs/self-hosting.md#before-you-deploy) explains these notices and Google's intended-use restrictions in more detail.

## Get started

Follow the [self-hosting guide](docs/self-hosting.md) to create a Discord application, get a Gemini API key, configure SobaFM, and run it with Docker Compose or from source with uv. Until the first image release, the guide explains how to build the container locally.

Once it is running, join a voice channel and use `/join`, then `/play request:rainy lo-fi with soft piano`.

| Command | What it does |
| --- | --- |
| `/join` | Connects SobaFM to your voice channel, or moves it there |
| `/play request:<text>` | Starts music or replaces it with a crossfade. Requests and the current music plan are sent to Google for interpretation; the resulting prompts and generation settings go to Lyria RealTime. No Discord identifiers are sent |
| `/stop` | Fades out the music; SobaFM stays in the channel |
| `/now` | Privately shows the current program and time left |
| `/settings [duration] [volume] [cooldown]` | Shows or changes the server's settings |
| `/leave` | Ends the program and disconnects |

`/join`, `/leave`, and `/settings` require Manage Server by default. `/play` requires you to be in SobaFM's voice channel; `/stop` does too, unless you have Manage Server. A new request replaces the music without a queue. By default, a program lasts 60 minutes, volume is 50%, and changes have a 30-second cooldown. Music ends when the channel has had no listeners for 60 seconds.

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
