# Requirements

- **Status:** Approved (#5)
- **Related:** [Architecture](architecture.md) · [Roadmap](roadmap.md) · [Decisions](decisions/README.md)

SobaFM's v1 behavior. Each requirement has a stable ID and the milestone that delivers it.

## Product summary

SobaFM is a self-hosted Discord bot that plays continuous AI-generated music in a voice channel. A server manager places it in a channel, where it waits for requests. A member describes the music they want with a slash command; a Gemini model turns the request into musical direction, and Lyria RealTime generates matching instrumental music until the program's duration elapses or someone requests something else. Each operator runs their own instance with their own Discord application and Gemini API key.

## Commands

- **CMD-1** `/play request:<text>` starts a program from a request of up to 200 characters, or replaces the current program. _(M2)_
- **CMD-2** `/stop` ends the current program with a fade-out; SobaFM stays in the channel. _(M2)_
- **CMD-3** `/now` privately shows the current program: title, interpreted style, requester, and time left. _(M3)_
- **CMD-4** `/join` connects SobaFM to the caller's voice channel, or moves it there; `/leave` ends any program and disconnects. _(M2)_
- **CMD-5** `/settings [duration] [volume] [cooldown]` shows the server's settings, or changes the given ones. _(M3)_
- **CMD-6** Commands are available in servers only, not in direct messages. _(M2)_

## Access

- **ACC-1** `/join`, `/leave`, and `/settings` require the Manage Server permission by default. _(M2)_
- **ACC-2** `/play` and `/stop` require the caller to be in SobaFM's voice channel; members with Manage Server may use `/stop` from anywhere. _(M2)_
- **ACC-3** Server administrators adjust who may use each command through Discord's command permissions (Server Settings → Integrations); SobaFM has no separate role setting. _(M2)_
- **ACC-4** When SobaFM is not in a voice channel, `/play` tells the caller to ask a server manager to use `/join`. _(M2)_

## Playback

- **PLAY-1** A server has at most one program. A new request replaces the current program with a crossfade; there is no queue. _(M2)_
- **PLAY-2** A program plays for the server's play duration, restarted by each new request, and then fades out. _(M3)_
- **PLAY-3** Playback is continuous: Lyria RealTime session limits and reconnections cause no audible gaps under normal service conditions. _(M2)_
- **PLAY-4** A program ends 60 seconds after the last listener leaves the channel. A listener is a member who is not a bot and not deafened. _(M2)_
- **PLAY-5** After a program ends, SobaFM stays in the channel, silent, until the next request. _(M2)_
- **PLAY-6** SobaFM remembers its voice channel. After a restart it rejoins that channel if the channel still exists and SobaFM can connect and speak there; otherwise it forgets the channel. Programs do not resume after a restart. _(M2)_
- **PLAY-7** If an administrator moves SobaFM, it adopts the new channel. If SobaFM is disconnected or its channel is deleted, the program ends and the channel is forgotten. _(M2)_
- **PLAY-8** Music is instrumental; wordless vocals are the only vocal option. _(M3)_

## Interpretation

- **AI-1** A Gemini model turns each request, together with the current program for relative requests such as "faster", into a structured plan: a title, one to four weighted style prompts, and optional tempo, key, density, brightness, drum muting, and wordless vocals. _(M3)_
- **AI-2** Model output is validated before it reaches Lyria. _(M3)_
- **AI-3** Requests that are not about music, and requests that Gemini's safety filters block, are refused and never sent to Lyria. _(M3)_
- **AI-4** If the model call fails, times out, or returns invalid output, the request text is used as typed and the reply says so: added as a lighter prompt to the current program's plan when music is playing, and as the only prompt otherwise. _(M3)_
- **AI-5** Names of artists, songs, and other works are translated into descriptive style terms. _(M3)_
- **AI-6** Only the request text and the current plan are sent to Gemini; no Discord identifiers are sent. _(M3)_
- **AI-7** An evaluation set of representative requests measures interpretation quality. It runs on demand with an API key, not in CI. _(M3)_

## Settings

Settings are stored per server and persist across restarts. _(M3)_

| ID | Setting | Default | Range |
| --- | --- | --- | --- |
| SET-1 | Play duration | 60 minutes | 5 to 240 minutes |
| SET-2 | Volume | 50% | 1% to 100%, as linear gain; 100% leaves the level unchanged |
| SET-3 | Change cooldown | 30 seconds | 0 to 600 seconds between program changes, server-wide, checked before the model call |

## Feedback

- **FB-1** Starting or replacing a program posts a now-playing message with the title, the interpreted style, the requester, and the end time. _(M3)_
- **FB-2** When it has permission, SobaFM sets the voice channel status to the current title. _(M3)_
- **FB-3** Filtered prompts, exhausted quotas, and service outages each produce a specific message, and the current music keeps playing where possible. _(M3)_
- **FB-4** Model-written text is escaped, length-capped, and sent with mentions disabled. _(M3)_

## Operation

- **OPS-1** Operators configure SobaFM through environment variables or a `.env` file; `DISCORD_TOKEN` and `GEMINI_API_KEY` are required. _(M1)_
- **OPS-2** SobaFM uses only the Guilds and Guild Voice States gateway intents. _(M2)_
- **OPS-3** SobaFM runs from source with uv. _(M2)_
- **OPS-4** SobaFM runs as a multi-architecture container image with a persistent data volume. _(M4)_
- **OPS-5** Logs go to standard output. Secrets are never logged, and request text is logged only at debug level. _(M2)_

## Non-functional requirements

- **NFR-1 Latency.** The median time from `/play` to audible music is 15 seconds or less. _(M3)_
- **NFR-2 Endurance.** A program plays for 60 minutes with at most 2 seconds of total underrun under normal service conditions. _(M2)_
- **NFR-3 Footprint.** Audio buffers for a playing server stay below 40 MB. _(M2)_
- **NFR-4 Platforms.** SobaFM runs on Linux (x86-64 and ARM64) and Windows. _(M4)_

## Responsible use

- **USE-1** Before its setup instructions, the README states that the Gemini API terms require users to be 18 or older and restrict use in services likely to be accessed by minors, that users in the EEA, Switzerland, and the UK require a billing-enabled project, and that Google may review prompts sent on the free tier. _(M4)_
- **USE-2** The description of `/play` says that requests are sent to Google. _(M3)_
- **USE-3** The project does not operate a public instance; each operator is responsible for their own deployment. _(M4)_

## Out of scope for v1

Voice input; queues and playlists; vocals with lyrics; Stage channels; more than one voice channel per server; a hosted public instance; a web dashboard.

## Open questions

| Question | Needed by | Tracking |
| --- | --- | --- |
| Lyria RealTime's session limit, generation rate, concurrency, and API version | ADR-0004 | Answered in #11 |
| Whether continuous playback is achievable at the measured generation rate, and the policy if it is not | M2 | Answered in ADR-0004 (#12): achievable with two sessions per playing server |
