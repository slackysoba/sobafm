# SobaFM

A self-hosted Discord music bot with generated music and a readiness-gated catalog mode.

A server manager places SobaFM in a voice channel, where it waits for requests. A member describes the music they want, such as "rainy lo-fi with soft piano" or "upbeat synthwave for a night drive." Generated music uses [Gemini](https://ai.google.dev/gemini-api/docs) to interpret the request and [Lyria RealTime](https://ai.google.dev/gemini-api/docs/realtime-music-generation) to make instrumental music. The current source also implements a catalog mode that interprets a request with Gemini and searches [Jamendo](https://developer.jamendo.com/v3.0/docs) for eligible recordings, including vocals when requested.

## Status

[SobaFM 1.0.0](https://github.com/slackysoba/sobafm/releases/tag/v1.0.0) is available as a published container image for Linux AMD64 and ARM64, or as a source install. Release evidence and the maintainer's acceptance dispositions are recorded on [#108](https://github.com/slackysoba/sobafm/issues/108); ongoing work is tracked in the [SobaFM project](https://github.com/users/slackysoba/projects/2).

**Published 1.0.0 is generated-only:** its `/play` command generates music. The **unreleased current source** uses `/freestyle` for generated music and conditionally registers catalog `/play` and `/skip`. Catalog playback is currently unavailable: production trust is empty and platform/resource, actual-source notice and listener qualification remains open. A Jamendo client id alone does not enable it. The [two-mode guide draft](docs/self-hosting.md#current-unreleased-source-two-modes) is not a claim that its required clean-machine walkthrough has passed.

## Before you deploy

Read Google's [Gemini API Additional Terms of Service](https://ai.google.dev/gemini-api/terms) before deploying. The current terms require:

- **Age:** you must be 18 or older to use the API. You may not offer it through a service directed at, or likely to be accessed by, people under 18.
- **Region:** use is limited to [available regions](https://ai.google.dev/gemini-api/docs/available-regions). Serving members in the EEA, Switzerland, or the United Kingdom requires a Google Cloud project with active billing.
- **Data:** requests go to Google. On the free tier, Google may use prompts and responses to improve its products, and human reviewers may read them. Tell your members and keep personal or confidential information out of requests.

For catalog use, also read Jamendo's [API terms](https://devportal.jamendo.com/api_terms_of_use) and [application authentication guide](https://developer.jamendo.com/v3.0/authentication), checked **21 October 2026**. Each operator registers their own application and protects its client id. Free API use is non-commercial, even if a particular track permits commercial use; advertising, affiliate compensation and paid access are not authorized by configuring this bot. Operators need a transparent privacy policy and must preserve creator/provider credit and direct track links. These service terms do not establish an individual recording's rights or complete notices; see the [catalog obligations and credits](docs/self-hosting.md#catalog-obligations-and-credits).

The project does not operate a public instance. Each operator runs their own copy with their own Discord application and Gemini API key, and is responsible for their deployment and members. The [self-hosting guide](docs/self-hosting.md#before-you-deploy) explains these notices and Google's intended-use restrictions in more detail.

## Get started

For the published release, follow the [self-hosting guide](docs/self-hosting.md) to create a Discord application, get a Gemini API key, and run **generated-only 1.0.0** with Docker Compose or uv. The guide includes versioned downloads, image pinning and source/attestation verification. In that release, use `/join`, then `/play request:rainy lo-fi with soft piano`.

The following demo and command list describe the **current unreleased source**, not the published image:

```text
/join
/freestyle request:rainy lo-fi with soft piano
/play request:gentle acoustic songs with vocals
/now
/skip
/stop
```

`/freestyle` is the generated path. `/play` and `/skip` appear only with catalog configuration: catalog `/play` currently reports unavailable prerequisites, and `/skip` cannot act without a playing catalog track. This is not a working catalog setup recipe. Once qualification permits catalog playback, `/now` provides complete private credits for the song actually heard. Catalog recordings play successively without fades or overlap; generated-to-generated changes retain crossfades.

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

The current-source guide's verbatim clean-machine both-mode walkthrough is still mandatory in [#130](https://github.com/slackysoba/sobafm/issues/130). Actual live, resource, notice and listener-client qualification remains open in [#131](https://github.com/slackysoba/sobafm/issues/131) and [#175](https://github.com/slackysoba/sobafm/issues/175).

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
