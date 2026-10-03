# Architecture

- **Status:** Approved (#5); the playback engine follows ADR-0004 (#12).
- **Requirements:** [requirements.md](requirements.md)
- **Decisions:** [ADR-0001](decisions/0001-build-on-python-discord-py-and-the-google-gen-ai-sdk.md) · [ADR-0002](decisions/0002-interpret-requests-with-gemini-structured-output.md) · [ADR-0003](decisions/0003-keep-server-state-in-sqlite-and-use-discord-command-permissions.md) · [ADR-0004](decisions/0004-stream-lyria-realtime-through-buffered-decks.md)

## Summary

SobaFM is a single Python process built on discord.py. It asks Gemini to interpret each request into a `MusicPlan`, and keeps one **station** per server. A station holds the current **program** and keeps audio flowing by filling **decks** (frame buffers fed by Lyria RealTime sessions) and crossfading between them in a **mixer** that discord.py reads every 20 ms.

## Scope

In scope: the v1 [requirements](requirements.md). Out of scope: hosting several voice channels per server, horizontal scaling, and a hosted public instance. One process serves the servers that invite its Discord application.

## Platform constraints

| Constraint | Source | Design response |
| --- | --- | --- |
| Lyria RealTime emits raw 16-bit PCM at 48 kHz in stereo, in chunks of about 2 seconds | [Lyria RealTime guide](https://ai.google.dev/gemini-api/docs/realtime-music-generation) | The format Discord encodes to Opus: chunks are split into 3,840-byte, 20 ms frames with no resampling |
| Audio is generated ahead of playback, and prompt changes affect only audio generated afterwards | Lyria RealTime guide | Every plan change fills a new deck and crossfades to it, instead of steering a session whose audio is already buffered |
| Every config update must include all fields, and `bpm` or `scale` changes need a hard context reset | Lyria RealTime guide | The full config is sent with every new session; tempo and key change only between decks |
| Sessions close after 600 seconds with WebSocket code 1011 and no warning; audio starts a median 2.9 seconds after `play()` | Measured in #11; not in the official documentation | Sessions are retired after 540 seconds; a deck goes live after a 6-second pre-roll |
| Generation averages 0.97× to 0.99× real time per session, with per-minute dips to 0.77× and stalls of up to 17 seconds | Measured in #11 | The next deck fills up to 60 seconds ahead, so the deck taking over can absorb stalls |
| Four concurrent sessions work when started apart; a session started seconds after others can be refused with a `filtered_prompt` | Measured in #11 | Two sessions per playing server; a refused start is retried once after 30 seconds |
| discord.py calls `AudioSource.read()` on its player thread every 20 ms, and an empty result ends playback | [discord.py API reference](https://discordpy.readthedocs.io/en/stable/api.html#discord.AudioSource) | `read()` never blocks and returns silence on underrun |
| Discord accepts only DAVE end-to-end-encrypted voice connections since March 1, 2026 | [Discord change log](https://docs.discord.com/developers/change-log) | discord.py 2.7 with its `voice` extra (ADR-0001) |

## Existing-solution review

| Need | Solution | Notes |
| --- | --- | --- |
| Gateway, slash commands, voice, Opus, DAVE | discord.py 2.7 | ADR-0001 |
| Gemini and Lyria RealTime | Google Gen AI SDK | Official SDK; asynchronous `live.music` client |
| Validation and configuration | Pydantic, pydantic-settings | Schema for model output; typed environment settings |
| Gain and mixing | `audioop` (`audioop-lts`) | C implementation, already a discord.py dependency |
| Persistence | Standard library `sqlite3` | ADR-0003 |
| Access control | Discord command permissions | ADR-0003 |
| Continuous playback from Lyria sessions | Custom: decks, mixer, station | No library mixes live PCM streams into a discord.py audio source. Audio servers such as Lavalink play tracks from sources and cannot take a live PCM stream |

## Design

```mermaid
flowchart LR
    member([Member]) -- "slash command" --> commands[Commands]
    subgraph process [SobaFM process]
        commands -- "request and current plan" --> interpreter[Interpreter]
        commands -- "MusicPlan" --> station["Station (one per server)"]
        station -- "MusicPlan" --> decks["Decks (live and next)"]
        decks -- "20 ms frames" --> mixer[Mixer]
    end
    interpreter <--> gemini[(Gemini API)]
    decks <--> lyria[(Lyria RealTime)]
    mixer --> voice["discord.py voice: Opus and DAVE"]
    voice --> channel([Voice channel])
```

| Module | Responsibility |
| --- | --- |
| `__main__` | Entry point: configuration, logging, signal handling, and a startup watchdog |
| `config` | Typed settings from the environment and `.env`; secrets held as `SecretStr` |
| `bot` | The discord.py client: intents, command sync, the station registry, interpreting and playing requests, voice-state events, and recovering voice clients discord.py strands |
| `commands` | Slash command handlers: checks, deferral, and replies with mentions disabled |
| `station` | One per server: the desired program, the reconcile loop, listener tracking, and the voice channel status |
| `deck` | One Lyria RealTime session filling a buffer of 20 ms frames, with flow control |
| `mixer` | The `discord.AudioSource`: the live deck, crossfades, fades, and volume |
| `pcm` | PCM format constants, the silence frame, and the chunk-to-frame splitter |
| `plan` | `MusicPlan` and its mapping to Lyria prompts and the full generation config |
| `interpreter` | The Gemini call, its instruction, and the refusal and fallback policies |
| `failures` | The failures of Gemini and Lyria RealTime a requester can act on, a rejected API key, an exhausted quota, or an outage, and which parts of Google's errors are safe to log |
| `store` | Per-server state in SQLite |

`scripts/lyria_probe.py` measures Lyria RealTime's behavior and is kept so the measurements can be repeated after SDK or model changes.

### Playback engine

A **deck** is a buffer of 20 ms frames filled by at most one Lyria RealTime session. A playing server keeps two decks generating: the **live** deck, which the mixer plays, and the **next** deck, which fills ahead. A session is retired when it reaches the session limit or when its plan is replaced, and its buffered audio stays playable. Session rotation, recovery from a dropped session, stalls, and plan changes therefore follow one path: hand over to the next deck with a crossfade, then open a new next deck ([ADR-0004](decisions/0004-stream-lyria-realtime-through-buffered-decks.md)).

Flow control uses only `pause()` and `play()`: a deck pauses generation when it holds 60 seconds of audio and resumes below 45 seconds. The receive loop is never throttled, because unread messages would delay WebSocket keepalive replies.

The **mixer** is the audio source discord.py reads. Each `read()` returns exactly one 3,840-byte frame from the live deck, mixed during a handover with the incoming deck using equal-power gains. Gain is the smoothed volume multiplied by the current fade, applied with `audioop`. On underrun, and on any exception, `read()` returns silence, so the player never stops on its own. Crossfades are counted in frames, so they pause intact while discord.py reconnects to voice.

### Threads and buffers

- The **event loop** runs the gateway, commands, Gemini calls, Lyria sessions, and the reconcile loop. Store calls run in a worker thread through `asyncio.to_thread`.
- discord.py's **player thread** calls `Mixer.read()` every 20 ms.
- Frames cross between the two through each deck's `collections.deque`, whose appends and pops are thread-safe.
- The mixer's control state (live deck, pending switch, volume) sits behind one `threading.Lock`, held for one read's pops and mixing, tens of microseconds, or for a constant-time update.
- The mixer never calls into the event loop; the station observes it on each tick. The only cross-thread signal is discord.py's `after` callback, delivered with `loop.call_soon_threadsafe`.

### Station reconcile loop

A station stores only what should be happening: the program (plan, requester, and end time) or none. Its `reconcile()` method runs every 250 ms and whenever a command or event wakes it, and it is the only code that creates, switches, or closes decks while the station runs; `close()` ends them. It applies these rules in order:

1. **End.** If SobaFM has lost its voice connection, the program's end time has passed, or the channel has had no listeners for 60 seconds, clear the program.
2. **Discard.** Drop ended decks the mixer no longer references, except the deck ready to go live for the current plan with the most buffered audio: an ended deck can't fill any further, and a handover needs only one. A session that ended early, because it failed or Lyria ended it before SobaFM retired it, or short of the pre-roll, counts once as a failed or refused session, so the station backs off before opening another, whether or not its program has started.
3. **Retire.** Stop the session of any deck that has reached the session limit or belongs to a replaced plan. Its buffered audio stays playable.
4. **Generate.** Keep a next deck filling for the current plan beside the live one, within the global session cap. Retry failed sessions, including ones that ended early, with backoff, and retry a refused start once after 30 seconds. A start fails at once when Google rejects the API key or the quota is exhausted, since retrying within seconds can't help, unless another deck of its plan is still open or ready to go live. When a replacement fails to start, the program still heard becomes the program again (FB-3).
5. **Start.** If nothing is playing and a deck for the current plan has its pre-roll, start the player, and fade in once it runs with voice connected. Restart the player if discord.py stopped it.
6. **Hand over, or stop.** With a program, if the player runs with voice connected, no crossfade is in progress, and the live deck holds less than 4 seconds or belongs to a replaced plan, crossfade to the ready deck of the current plan with the most buffered audio. Without one, retire idle decks; once any fade or crossfade completes, fade out the live deck and then stop the player. With no player running, nothing reads the mixer, so the station stops at once.
7. **Regulate.** Pause or resume each deck's generation at the flow-control thresholds.
8. **Status.** Show the title being heard as the voice channel status (FB-2): a program's title once it plays, kept while a replacement starts, and cleared when the program ends or the station closes. Discord lets SobaFM change a channel's status only while connected there, so the station syncs only then. Each tick compares what SobaFM has set, and where, with what it should show, so a drag or a new gateway session catches up. One request runs at a time and is never cancelled. Closing, and `/join` before it moves SobaFM, clear the status, waiting at most 10 seconds for the request in flight and the clear after it. Without the Set Voice Channel Status permission, SobaFM sets nothing and checks again every 10 seconds. A refused request is logged once and retried after 10 seconds, doubling up to 5 minutes, and never affects playback.

   A channel SobaFM was dragged out of keeps the title until it empties or SobaFM shows another title there, since only someone connected there, or with Manage Channels, can change it. So does a channel whose connection SobaFM lost, since that closes the station. A new gateway session keeps the station, which clears the title once SobaFM is back.

The station never closes a deck the mixer still references. Commands only replace the desired program and wake the loop, so they need no lock: the latest request wins. The change cooldown (M3) is checked and started when a request arrives, before the model call. It is freed again if that request ends without playing, even after the reply, unless a later request has started it since. It is kept in memory, so a restart, which ends every program, clears it.

### Constants

Set by ADR-0004 from the measurements in #11.

| Constant | Value | Purpose |
| --- | --- | --- |
| Frame | 3,840 bytes | 20 ms of 16-bit, 48 kHz stereo PCM |
| Reconcile tick | 250 ms | Rule evaluation and flow-control checks |
| Pre-roll | 6 s | Audio a deck needs before it can go live, about three chunks |
| Pause and resume thresholds | 60 s and 45 s buffered | Flow control; the next deck stays paused at 60 s until it goes live |
| Session limit | 540 s | Retires sessions before the 600-second limit |
| Hand-over point and crossfade | 4 s remaining and 4 s | Start of a handover, and its length |
| Fade-in and fade-out | 2 s and 3 s | Program start and end |
| Refused-start retry | Once, after 30 s | Separates rate-limited session starts from filtered prompts |
| Session caps | 2 per server; 4 per process | Bounds Lyria usage; two playing servers per API key |
| Connect timeout and backoff | 15 s; 2, 5, then 15 s | Session recovery |
| Empty-channel grace period | 60 s | PLAY-4 |

The next deck reaches its 60-second buffer about a minute after it opens, well before the live session's 540-second limit, so each handover starts with a full minute of audio in reserve. A full deck holds about 11.5 MB of audio, and a playing server holds less than 40 MB in the worst case (NFR-3).

### Interpreter

The interpreter is SobaFM's only Gemini stage. It makes one call for each accepted `/play`:

- **Input:** the request text and the current plan, sent as data in the user turn. The system instruction distills the [Lyria prompt guide](https://ai.google.dev/gemini-api/docs/lyria-prompt-guide), asks for instrumental descriptors in English, and has names of artists and works translated into descriptive terms.
- **Output:** JSON constrained by the schema below through [structured output](https://ai.google.dev/gemini-api/docs/structured-output), then validated with Pydantic.
- **Policy:** `not_music`, safety-blocked, and blank requests are refused. Any other failure, such as a timeout (10 seconds), a rejected API key, a rate limit, a server error, a connection failure, or invalid output, falls back to the request text, truncated to 120 characters, as a single prompt, and the reply names an exhausted quota or an outage as the cause. Lyria RealTime uses the same key, so it reports a rejected key itself. For `refine`, the values an answer leaves out or unset are copied from the current plan: tempo, key, density, brightness, drum muting, and wordless vocals. API errors are logged by code, status, and error reason, never by message, since Gemini's messages can quote the API key. Other failures are logged by exception type, with a traceback when unexpected.
- **Model:** `gemini-3.5-flash-lite` by default, set with `SOBAFM_GEMINI_MODEL`. The call uses the minimal thinking level and no tools; sampling parameters are not sent.

```python
class Prompt(BaseModel):
    text: str  # 1 to 120 characters, with whitespace collapsed
    weight: float  # 0.1 to 1.0


class MusicPlan(BaseModel):
    title: str  # 1 to 60 characters, with whitespace collapsed
    prompts: list[Prompt]  # 1 to 4 prompts
    bpm: int | None  # 60 to 200
    scale: Scale | None  # the SDK's scale enum
    density: float | None  # 0.0 to 1.0
    brightness: float | None  # 0.0 to 1.0
    mute_drums: bool
    vocalization: bool  # wordless vocals


class Interpretation(BaseModel):
    kind: Literal["new", "refine", "not_music"]
    plan: MusicPlan | None  # None only when kind is "not_music"
```

`MusicPlan.to_config()` always builds the complete Lyria config. Guidance (4.0), temperature (1.1), and top-k (40) stay at Lyria's defaults, the mode is `QUALITY` (or `VOCALIZATION` for wordless vocals), and no seed is set.

## Data model

```sql
CREATE TABLE guild (
    guild_id   INTEGER PRIMARY KEY,
    channel_id INTEGER,           -- remembered voice channel, or NULL
    settings   TEXT    NOT NULL,  -- GuildSettings as JSON
    updated_at TEXT    NOT NULL   -- ISO 8601, UTC
);
```

`GuildSettings` is a Pydantic model with the defaults and ranges in [SET-1 to SET-3](requirements.md#settings). Missing fields take their defaults and unknown fields are ignored, so adding a setting needs no migration. A stored setting that no longer validates falls back to its default, with a warning that names it.

## Interfaces

**Slash commands** are specified in the [requirements](requirements.md#commands).

**Configuration** comes from environment variables or a `.env` file:

| Variable | Required | Default | Purpose |
| --- | --- | --- | --- |
| `DISCORD_TOKEN` | Yes | None | Discord bot token |
| `GEMINI_API_KEY` | Yes | None | Gemini API key, used for interpretation and Lyria RealTime |
| `SOBAFM_GEMINI_MODEL` | No | `gemini-3.5-flash-lite` | Interpreter model |
| `SOBAFM_DATA_DIR` | No | `./data` | Directory for the SQLite database |
| `SOBAFM_LOG_LEVEL` | No | `INFO` | Log level |
| `SOBAFM_DEV_GUILD_ID` | No | None | Registers commands to one server for development |
| `SOBAFM_MAX_SESSIONS` | No | `4` | Global cap on concurrent Lyria RealTime sessions, an even number: each playing server reserves two |

**Discord invitation:** scopes `bot` and `applications.commands`; permissions View Channel, Connect, Speak, and Set Voice Channel Status.

## State and lifecycle

- **Request:** admitted by `/play`, then interpreted by Gemini. While Gemini interprets it, the first newer request to reach the station ends it as replaced. `/stop` and `/leave` end it as stopped, and losing the channel or shutting down ends it too. A refused request ends nothing, so the newest request Gemini accepts wins.
- **Program:** created by an accepted `/play` and replaced by the next one. Its end time is the play duration after the station accepts the request, so start-up time counts toward it. The program is cleared when its end time passes, by `/stop` or `/leave`, after the empty-channel grace period, or when SobaFM loses its channel. A replacement that fails to start makes the program it replaced current again. `/now` shows a program as starting until it plays, and as playing after. When a program hasn't played 75 seconds after the station takes its request, `/play` answers that it is taking longer, then edits that answer once the program plays or doesn't, if that is within the interaction's 15 minutes (FB-1).
- **Deck:** `connecting`, then `generating` (paused or not), then `ended` with a reason: retired, closed, failed, or filtered. A deck records the close code when Lyria closes its session with a close frame, including a refusal during setup, which ends the deck as failed. A deck is ready once it holds the pre-roll. Its generation rate is audio seconds received per unpaused wall-clock second, measured after 10 seconds.
- **Startup:** load and validate configuration, check that the Opus library loads, open the store, connect to the gateway, and sync commands. Each server's remembered channel is rejoined when that server becomes available: at startup, after a new gateway session, or when an outage ends. The watchdog exits with an error if the gateway is not ready within 120 seconds, so the process supervisor restarts SobaFM.
- **Shutdown:** on `SIGTERM`, stop the voice check and any recoveries, drop pending edits of slow-start answers, end the requests being interpreted, close each station, which ends its Lyria sessions and clears its voice channel status, waiting up to 10 seconds for Discord, then disconnect from voice and close the client.

## Failure behavior

| Failure | Behavior |
| --- | --- |
| Gemini timeout, rejected API key, rate limit, server error, connection failure, or invalid output | Fall back to the request text, and say so in the reply, naming an exhausted quota or an outage as the cause. Lyria RealTime then reports a rejected key |
| Not-music request or Gemini safety block | Refuse with a short explanation; nothing reaches Lyria |
| Lyria filters a prompt | Discard the new deck, keep the current music, and tell the requester |
| Lyria rejects the API key, or the quota is exhausted | Report it at once with a specific reply, since retrying within seconds can't help, unless another deck may still start the program. A failed replacement keeps the current music. Both causes are recognized from the close's free-text reason, which SobaFM matches but never logs: Google's live APIs are reported to signal an exhausted quota only that way, with the code `1011` that outages also use, and the key's rejection came with code `1007` |
| Lyria session closes during a program | Its buffer keeps playing while a replacement deck fills, opened after the backoff. Failed sessions are retried after 2, 5, then every 15 seconds; a program that has not started yet reports the failure after the third retry, as an outage when the connection failed, the upgrade was refused with a 5xx status, or Lyria closed with `1006`, `1011`, or `1013` |
| Generation stalls or slows | The live deck's buffer absorbs it; below 4 seconds, the station hands over to the next deck. Remaining underruns play silence and are logged: a warning when the live deck runs dry while a program is heard, the silence's length once audio has played again for a second, and each program's total underrun when it stops being heard |
| Lyria refuses a new session's start (`filtered_prompt` with no audio) | Retry once after 30 seconds, then report the prompt as filtered |
| No session capacity left in the process | Each program reserves two sessions; a request beyond the reservations is told that SobaFM is busy in other servers |
| Voice reconnection | discord.py reconnects; reads pause, flow control pauses generation, and crossfades resume intact. If the player thread gives up first, a playing station starts a new one once voice is back. A fade-in or handover, which reports a request as playing, waits until voice is connected. During a network outage, discord.py can stop reconnecting without cleaning up its voice client. SobaFM checks every 10 seconds for a client with no connection task. Once one stays that way for 60 seconds, twice discord.py's websocket close timeout, SobaFM ends any program as disconnected, closes the client, and rejoins the remembered channel. It retries the rejoin 30 seconds after each failed attempt until SobaFM is back in voice or the channel is forgotten. When Discord doesn't answer, an attempt holds the server's voice lock for about a minute: discord.py's 30-second handshake timeout, then its 30-second wait to leave |
| SobaFM moved, disconnected, or its channel deleted | Adopt the new channel, or end the program and forget the channel (PLAY-7). Discord reports every disconnect alike, so a voice connection that discord.py gives up reconnecting is also forgotten. A move or a voice server change after one of discord.py's own reconnects keeps the connection: SobaFM clears the flag discord.py leaves set, which would make it end the connection. A move or voice server change during the connection handshake restarts discord.py's connector, and SobaFM waits for it before judging the connection |
| New gateway session (a reconnect that cannot resume) | discord.py forgets its voice clients, so the program ends. SobaFM closes the old voice connection, which takes 30 seconds because discord.py no longer routes its events, then looks the channel up again and rejoins it |
| Player thread error | The `after` callback tells the station, which starts a new player while a program plays, at most once a second. If discord.py cannot start a player for a reason other than a missing voice connection, the program ends. Errors inside `read()` return silence and are logged once per run |
| Gateway not ready at startup | The watchdog exits with an error and the process supervisor restarts SobaFM |
| Opus library missing or unloadable | SobaFM exits at startup with a message that names the library |

## Security and privacy

- **Secrets** come only from the environment, are held as `SecretStr`, and are never logged. `.env` files are git-ignored.
- **Data sent to Google:** the request text and current plan go to Gemini; prompts and generation settings go to Lyria RealTime. Discord identifiers are never sent. Google may review prompts sent on the free tier, which the README states (USE-1, USE-2).
- **Model output is untrusted.** It is validated against the schema before use; text shown in Discord is escaped, length-capped, and sent with mentions disabled; nothing is executed.
- **Prompt injection:** the instruction treats the request as data, and the schema limits what any request can produce.
- **Least privilege:** two non-privileged gateway intents and four channel permissions.
- **Logs:** request text appears only at debug level. Gemini's error statuses and reasons, and Lyria's close reasons, are logged only when they are tokens such as `RESOURCE_EXHAUSTED`, since free text could quote the API key. A frame from Lyria that the SDK can't parse or validate, or an unexpected audio format, is logged by a fixed description, without its content. A failed WebSocket handshake, or a redirect that can't be followed, is logged by the error's type and any status code, without the server's text. Lyria's close reasons are matched for a rejected API key or an exhausted quota, but never logged, and a deck's summary names the cause of a session that ended short of the pre-roll. `SOBAFM_LOG_LEVEL` applies to SobaFM's own loggers: libraries stay at INFO, since at DEBUG the websockets library logs request headers, which carry the API key. The Google Gen AI SDK logs only warnings and errors, since at INFO it logs Lyria RealTime's setup reply verbatim. Python warnings are logged too, except two of the SDK's: its warning about an unknown enum value in any response, which quotes the value Lyria or Gemini sent, and its expected warning that Lyria RealTime is experimental.

## Testing

| Level | Scope | Tooling |
| --- | --- | --- |
| Unit | Framing, mixer gains, plan validation and config mapping, the store | pytest |
| Component | Deck and station rules against a fake Lyria session and a fake clock; command handlers with mocked interactions | pytest, pytest-asyncio |
| Evaluation | 38 interpreter cases with property checks: valid output for every case, every not-music request refused, including prompt injection, tempo and key as asked, refinements keeping what they do not change, no names of artists or works, and injected text in the current plan ignored; at least 90% of checks passing | `pytest -m eval` with `GEMINI_API_KEY`; not run in CI |
| Live | The Lyria probe, smoke tests, and the 60-minute soak test | `scripts/lyria_probe.py`, a private Discord server |

## Deployment

SobaFM runs from source with `uv run sobafm`. From M4 it also ships as a multi-architecture container image on GitHub Container Registry, based on `python:3.14-slim` with `libopus0`, running as a non-root user with a `/data` volume. The compose file sets an init process and `restart: unless-stopped`.

## Open questions

| Question | Tracking |
| --- | --- |
| Session limit, generation rate, concurrency, settle time, and API version | Answered in #11 |
| Playback engine constants, the number of sessions per server, and whether continuous playback is achievable | Answered in ADR-0004 (#12) |
