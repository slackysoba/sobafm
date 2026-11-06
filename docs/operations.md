# Operations

**Status: amd64 first slice, not yet qualified.** This guide describes the first playback slice for Linux amd64 with Docker Compose. The image at commit `ab10a71` ran in one private Discord session on Docker Desktop under WSL2 with the real catalog and Gemini ([#124](https://github.com/slackysoba/sobafm/issues/124)), after an earlier session at `d6923c8` ([#97](https://github.com/slackysoba/sobafm/issues/97)); that is not qualification of the image, its limits or the release. Settings, volume, the volume layout and the song-list refill below came after those sessions and have only been tested offline. Treat it as a preview of the operator path, not a release. Native arm64 is on hold until after the first amd64 release ([#60](https://github.com/slackysoba/sobafm/issues/60)).

The slice joins a voice channel and plays a catalog program per `/play` request: the admitted local recordings Gemini ranks for the request, 8 at a time, one after another, until it is stopped, replaced, runs out of recordings or plays for a minute with no listener. `/skip` moves on to the next one, `/now` shows the heard recording's full credits, `/settings` keeps each server's volume and change cooldown, and it stops and shuts down cleanly. It has no `/freestyle` and doesn't remember its voice channel across a restart.

## Requirements

- Linux amd64 with Docker Engine and the Compose plugin, using cgroup v2 (the default on current distributions). No compiler is needed.
- A Discord application with a bot user and its token.
- Optionally, a prepared catalog snapshot, its admission reviews, a media manifest, the admitted MP3 files and a Gemini API key. Without them the bot runs with `/join`, `/now`, `/stop`, `/leave` and `/settings` only; `/play` and `/skip` are not registered.

## Discord setup

1. In the Discord Developer Portal, create an application and add a bot. Copy its token; it goes only into `.env`.
2. No privileged gateway intents are needed. SobaFM requests only the Guilds and Voice States intents.
3. Invite the bot with the `bot` and `applications.commands` scopes and these permissions: **View Channels**, **Connect** and **Speak**. Replies are private interaction responses, so the bot needs no Send Messages permission.

Command access:

| Command | Who can use it |
| --- | --- |
| `/join`, `/leave`, `/settings` | Members with Manage Server by default. Server admins can change this under Server Settings, Integrations. |
| `/play request:<text>`, `/skip` | Members in the bot's voice channel. |
| `/stop` | Members in the bot's voice channel, or anyone with Manage Server. |
| `/now` | Anyone in the server. It privately shows what is actually heard, with full credits. |

## Install and configure

```sh
git clone https://github.com/slackysoba/sobafm.git
cd sobafm
cp .env.example .env
mkdir catalog
```

Edit `.env` and replace the placeholders. Never commit `.env`. The variables are:

| Variable | Required | Meaning |
| --- | --- | --- |
| `SOBAFM_DISCORD_TOKEN` | Yes | The bot token. SobaFM removes it from its own environment at startup, so the decoder never inherits it, and never logs it. |
| `SOBAFM_DATABASE` | Yes | The settings database. Keep the example's `/data/sobafm.db`, on the writable volume. |
| `SOBAFM_CATALOG` | With the next three | Prepared catalog snapshot (`sobafm-catalog-1`). |
| `SOBAFM_REVIEWS` | With the catalog | Trusted admission reviews (`sobafm-admission-1`). |
| `SOBAFM_MEDIA` | With the catalog | Media manifest locating each admitted recording's file. |
| `SOBAFM_GEMINI_API_KEY` | With the catalog | The key for matching requests. It is removed from the environment at startup, sent only as a request header and never logged. |

The catalog, reviews and media manifest must be set together, or none of them, and the Gemini key is required with them. Without them `/play` and `/skip` are not registered, and a key on its own is ignored.

**Where things live** ([ADR 0009](decisions/0009-writable-data-volume.md)):

| Container path | On the host | Mode | Holds |
| --- | --- | --- | --- |
| `/catalog` | `./catalog` | Read-only | The catalog, reviews, media manifest and audio. The container runs as UID 1000, so that user must be able to read them. |
| `/data` | The named volume `sobafm-data` | Writable | `sobafm.db`, the settings database, and SQLite's brief journal beside it while it saves. Compose creates the volume owned by UID 1000, so it needs no setup. |

The rest of the container's filesystem is read-only.

**What Gemini receives and what it costs.** Each `/play` makes one call containing the request text and each admitted recording's title, creators, length, labels and details. It contains no Discord identities or credits, and SobaFM asks Google not to store the interaction. **The bot makes more calls while a program plays, with nobody typing a command:** one for each 8 recordings, each leaving out what the program has played. `/skip` makes no call. Cost grows with the catalog. On the current 167 recordings, the estimate on Google's paid tier is about 1 US cent for the largest call, 2 to 4 cents for an hour of music and 8 to 21 cents for a program through the whole catalog; see [ADR 0011](decisions/0011-program-refill.md). Google's [pricing page](https://ai.google.dev/gemini-api/docs/pricing) says free-tier content may be used to improve its products and paid-tier content isn't, and that covers these calls too. To limit calls, the [change cooldown](#server-settings) spaces out requests, each server runs one match at a time, and only members in the bot's voice channel can use `/play`. A request Gemini refuses or fails on doesn't hold the cooldown, so repeated requests of that kind are still sent.

The media manifest binds each playable recording to one local file:

```json
{
  "version": 1,
  "recordings": [
    {
      "key": {"source": "jamendo", "recording_id": "example-id"},
      "file": "jamendo/example-id.mp3",
      "size": 4812345,
      "sha256": "<64 lowercase hex digits of the file>",
      "max_pcm_bytes": 46080000
    }
  ]
}
```

`file` is relative to the manifest's directory and may not leave it. `size` and `sha256` are the exact bytes reviewed at ingestion; the decoder never gets end of input for a file that differs. `max_pcm_bytes` is a hard output bound, not a duration: decoded output beyond it fails the track. Set it from the recording's validated decoded length plus a small margin, as a whole number of 3,840-byte frames (192,000 bytes per second; at most four hours). A recording with no manifest entry fails at `/play` and the current audio continues.

## Run

```sh
docker compose up --build -d
docker compose logs -f
```

The service runs with Docker's default seccomp profile, a read-only root, all capabilities dropped, no new privileges, an init process, two CPUs and the [ADR 0004](decisions/0004-amd64-whole-service-adoption.md) limits: memory and memory-plus-swap 90,177,536 bytes and 64 tasks. Logs go to stdout and are kept in at most two 1 MB files. The restart policy is `on-failure:5`: Docker restarts a container that exits nonzero at most five times, so a refused start (exit 2, 3, 4 or 5) or a failed Discord login does not retry indefinitely. A clean shutdown (exit 0) is not restarted. After fixing the cause, start it again with `docker compose up -d`.

Before reading any configuration or connecting to Discord, SobaFM reads its own cgroup's `memory.max`, `memory.swap.max` and `pids.max` and refuses to start unless they are exactly 90177536, 0 and 64. Do not change those limits in `compose.yaml`: the app will refuse to run, and no other values are accepted.

Then, in Discord: a manager uses `/join` from a voice channel, a listener uses `/play` with any request, such as `a haunted carnival on the moon`, and `/now` shows the heard recording and its credits. The reply says how close the match is: the recording asked for, the closest match or just the nearest thing in the catalog. Audio becomes audible after six seconds of buffered PCM, or at the verified end of a shorter recording.

While a recording plays, the next one on the program's list is prepared in the second audio bank and follows the last frame directly, with no fade or overlap. `/skip` switches to the next recording as soon as it is buffered; the current recording keeps playing until then, and keeps playing if the next one cannot be prepared. `/play`, `/stop`, `/leave` and shutdown discard a prepared next recording.

**Song lists refill as the program plays** ([ADR 0011](decisions/0011-program-refill.md)). Gemini is asked for 8 recordings at a time, best first, so later ones may fit the request less well; the reply's description of the fit is about the first recording only. When the last recording on the list starts, the bot asks for 8 more in the background, leaving out what the program has played. Music keeps playing while it waits. A `/skip` then doesn't wait for the answer: if nothing is ready, it says the next recording could not be prepared, the current one continues, and the skip holds no cooldown. `/play` for a new program, `/stop`, `/leave`, the end of the listener grace period, losing voice and shutdown cancel the call, and an answer that still arrives changes nothing.

If the call fails, is refused or brings nothing new, the bot tries twice more while the current recording plays: after 15 seconds and then after up to 60, so a per-minute rate limit can pass. On a short recording the second wait shortens so the next one can still be ready in time. If those fail too, the program ends after the current recording, or at once if nothing is playing by then, and `/now` says Gemini did not supply more recordings. `/play` starts a new program. A program waiting in silence for more recordings keeps first claim on the audio banks, so another server's next recording or `/skip` can wait for it, by about a minute at most. A program never repeats a recording: once every admitted recording has been played or tried, it ends after the last one and `/now` says so. With a one-recording catalog that is the normal result once the recording ends.

A program ends 60 seconds after the last listener leaves the bot's voice channel. A listener is a member in that channel who is neither a bot nor deafened, whether self-deafened or deafened by the server; muting does not matter. If a listener joins or undeafens before then, the program continues untouched. When the period ends, the program stops as with `/stop`: the bot stays connected and silent, and `/now` says the program ended because no listeners remained and that `/play` starts a new one. Moving the bot with `/join` to a channel with no listener starts the same period. Because the bot does not request the members intent, it cannot tell whether a member who was already in the channel when it connected, and has not changed voice state since, is a bot; such a member counts as a listener, so the program keeps playing for them until they leave or deafen.

## Server settings

`/settings` privately shows the server's values. Give it any of its options to change them:

| Option | Setting | Default | Range |
| --- | --- | --- | --- |
| `volume` | Playback volume, as a linear gain | 50% | 1 to 100 |
| `cooldown` | Change cooldown, in seconds | 30 | 0 to 600 |
| `duration` | Generated duration, in minutes | 60 | 5 to 240 |

Settings are saved before they take effect, and each server keeps its own. If saving fails, the reply says so and nothing changes.

**Volume** applies to current audio from its next 20 ms frame, with no restart. It can only lower the level, so it can't clip, and 100% leaves the recording's samples unchanged. It defaults to 50%, so a recording plays at half the level it would without this setting.

**Change cooldown** is one wait per server across `/play` and `/skip`, whoever asked. A request during it is refused with the time left, before Gemini is called or a recording is chosen, and current audio continues. It counts from when the last accepted request arrived, and refused requests don't extend it. A request that fails or changes nothing doesn't hold it: an invalid request, one Gemini refuses or fails on, a `/skip` with nothing to skip, or a recording that can't start. A `/stop` doesn't clear it, unless the request it interrupts hasn't been answered yet: a `/play` still being matched, or a `/skip` still waiting up to five seconds for its next track. A changed `cooldown` applies at once, including to a wait already running, and 0 turns it off. The wait itself isn't saved, so it starts afresh after a restart or when the bot leaves voice.

**Generated duration** is stored and validated, but nothing uses it until `/freestyle` exists.

The database keeps a row only for servers that changed a setting, up to 1,000. Beyond that, a further server keeps the defaults and its change is refused. To back it up, stop the service first, so no write is in progress:

```sh
docker compose stop
docker compose cp sobafm:/data/sobafm.db ./sobafm-backup.db
docker compose start
```

To restore a copy, give it to UID 1000 and keep that owner with `-a`. Otherwise Docker creates it owned by root, and the next start exits 4:

```sh
docker compose stop
sudo chown 1000:1000 sobafm-backup.db
docker compose cp -a ./sobafm-backup.db sobafm:/data/sobafm.db
docker compose start
```

Don't edit the database while the service runs: SobaFM reads it only at startup, so it wouldn't see the change, and a later save would overwrite that server's row.

## Stop

```sh
docker compose stop
```

SIGTERM stops playback on every server, disconnects, waits for every decoder to be killed and reaped, releases the audio banks and closes the settings database. The cleanup deadline is three seconds per track, and closing the database waits at most five seconds; Compose's default ten-second stop grace covers both. `docker compose down` also removes the container and keeps the settings. `docker compose down -v` also deletes the `sobafm-data` volume, and with it every server's settings.

## Update

```sh
git pull
docker compose up --build -d
```

Read the release notes and `docs/requirements.md` first. Updating the files in `./catalog` needs a restart: `docker compose restart`.

**Moving from the read-only `./data` layout.** Earlier versions kept the catalog in `./data`, mounted at `/data`. Move it once, before starting the new version:

```sh
mv data catalog
```

Then, in `.env`, add `SOBAFM_DATABASE=/data/sobafm.db` and change `SOBAFM_CATALOG`, `SOBAFM_REVIEWS` and `SOBAFM_MEDIA` to start with `/catalog/` instead of `/data/`. Until then the service exits 2: an old catalog path logs `catalog snapshot or reviews could not be loaded`, and a missing database location logs `SOBAFM_DATABASE is required`. Files in the manifest are relative to its own directory, so it needs no change.

## Build the FMA catalog

The catalog comes from the [FMA research dataset](https://github.com/mdeff/fma) (Defferrard, Benzi, Vandergheynst and Bresson, ISMIR 2017; its metadata is CC BY 4.0). [Decision 0007](decisions/0007-fma-dataset-batch-catalog.md) records the rule, the limits and what a review can't establish: the dataset says it's meant for research, it's a third-party copy, and nothing after its 2017 snapshot is known.

The catalog, reviews and manifest for the 167 accepted recordings are in [`evaluations/catalog/fma-2026-11/`](../evaluations/catalog/fma-2026-11/). The audio is never in Git or the image, so another machine has to fetch the files again, under its operator's own decision. Copy the three files into `./catalog` and the audio into `./catalog/fma/`.

**Authorization comes first.** These steps send requests to the dataset's host and store audio. Run them only under your own decision, within the limits in that record. Any response other than partial content stops the run, and it stays stopped until you remove the `fma-STOPPED` marker from the state folder. Don't work around a refusal by changing headers, identity or client.

Run each step in a container of your own image, with this repository mounted and `PYTHONPATH=/work/src`. Keep the state folder private. It needs `tracks.csv` and `raw_tracks.csv` from the dataset's metadata archive.

| Step | Network | What it does |
| --- | --- | --- |
| `fma_build.py candidates --target N --spares N --strike ID,ID` | No | Applies the rule and writes the list for review. Later steps read only the chosen rows, and the catalog names the rule version that chose them |
| `fma_fetch.py directory` | Yes | Reads the archive's directory once and screens out files that look cut short |
| `fma_fetch.py audio --limit N` | Yes | Reads the next files, one at a time, checking each against the archive's CRC-32. A finished file is never requested again |
| `fma_build.py verify` | No | Reads each file's tags and decodes it with the real producer. Run it at the service limits: `--network none --memory 90177536 --memory-swap 90177536 --pids-limit 64` |
| `fma_build.py finalize --target N` | No | Writes `catalog.json` and `media.json` for what passed, the final list, every rejection and ten recordings to spot-check. Run it again with `--strike ID,ID` to apply what the maintainer strikes. That keeps the same ten |
| `fma_build.py reviews` | No | Writes `reviews.json`. Run it only after the maintainer accepts the list. It records that acceptance and doesn't replace it |

Then start the service as usual and check the `catalog source:` lines in the log. A rebuild changes a recording's digest wherever its reviewed metadata changed, so those recordings need a fresh acceptance.

**To remove a recording**, for example after a takedown or a rights conflict, remove its entry from the `reviews` array in `reviews.json` and delete its file from `./catalog/fma/`, then run `docker compose restart`. The file is one line of JSON, so edit the array rather than deleting a line: invalid JSON stops the whole service at startup with exit 2. Without a review entry the recording can't be chosen. No automatic recheck of rights or availability happens.

## Exit status and troubleshooting

| Exit | Log line | Meaning and fix |
| --- | --- | --- |
| 5 | `crash dump controls refused: <what>` | SobaFM couldn't keep its memory out of crash dumps, so it stopped before loading its dependencies or reading any configuration. This shouldn't happen under the unchanged `compose.yaml`. Look for a custom seccomp or AppArmor profile, or a changed `/proc` mount. See [Crash dumps](#crash-dumps). |
| 4 | `settings database refused: <kind>` | The database couldn't be opened, read and written, so SobaFM stopped rather than lose settings silently, before connecting to Discord. It leaves a file it refuses unchanged. See the kinds below. |
| 3 | `container limits refused: <file> must be <value>` | The cgroup limits differ from ADR 0004, or cgroup v2 is unavailable. Use the unchanged `compose.yaml` on a cgroup v2 host. |
| 2 | `configuration error: ...` | A missing token or database location, an incomplete catalog/reviews/media/key set, or a file that is unreadable, too large or invalid, including a catalog too large to send for matching. Messages name variables, never values. |
| 1 | `... lease(s) remain at shutdown`, `cleanup deadline(s) were missed`, `settings database did not close in time` or `Discord client stopped: <type>` | Shutdown could not confirm release, or the Discord client failed (for example an invalid token). |
| 0 | | Clean shutdown. |

For exit 4, the kind says what to check:

- `SQLITE_CANTOPEN` or `SQLITE_READONLY`: the location is missing or not writable by UID 1000. Check that `SOBAFM_DATABASE` is on `/data` and that `compose.yaml` mounts `/data` without `:ro`. SQLite also writes a journal beside the file, so the directory must be writable too.
- `SQLITE_NOTADB` or `SQLITE_CORRUPT`: the file is damaged. Restore a copy, or remove it to return every server to the defaults.
- `SQLITE_FULL`: the volume's disk is full.
- `SQLITE_BUSY`: another program held the database for over five seconds.
- `unknown_schema_version`: the file is from a newer SobaFM. Use that version or restore an older copy.
- `invalid_row` or `too_many_servers`: the file holds values SobaFM would never write, so it was edited by something else. Restore a copy.

INFO lines record what was played, without request text or tokens: `voice connected` and `voice disconnected`; `heard source changed: recording=<source:id> credit_snapshot=<id>` and `heard source cleared`; `program refilled: recordings=<n> attempt=<n>` when more recordings arrive; `program has no recording left in the catalog`; `listener grace started: seconds=60` and `listener grace cancelled`; `program ended: reason=<exhausted|refill_failed|unavailable|no_listeners>`; one `track retired: ...` line per track with `frames_handed_to_voice` (frames given to the voice player, not packets confirmed delivered), `underruns` (silence frames returned while the track was playing or ready, including a momentarily busy bank), the completion facts, `failure`, `bank_released` and `cancel_to_release_seconds`; `settings saved: generated_minutes=<n> volume_percent=<n> cooldown_seconds=<n>`; and `shutdown: released=... leases_held=... cleanup_failures=...`. At startup, `settings database opened: servers=<n>` reports how many servers have saved settings, and `catalog source: source=<name> state=<state> provided=<n> admitted=<n>` reports each source; `admitted=0` everywhere means `/play` has nothing to choose from. Each call, whether from `/play` or a refill, logs `request matched: outcome=<match|refuse> ranked=<n> seconds=<s>`.

During playback:

- `request matching failed: kind=<kind> seconds=<s>`: no list was produced and the current audio continues. `http_401` or `http_403` usually means a wrong key, `http_429` a rate or quota limit, and `timeout` no answer within 10 seconds. `interaction_not_completed`, `response_shape` and `output_*` mean Gemini's answer wasn't usable, which may mean the API has changed.
- `program refill failed: attempt=<n>` (WARNING): a call for more recordings failed, was refused or brought nothing new. The line before it, if any, gives the kind. After `attempt=3` the program ends with `reason=refill_failed`. If this recurs, check the key and Gemini's rate limits; the free tier's are lower.
- `decode failed: <kind>` names the decode failure, for example `input_hash` (the file differs from the manifest), `input_open`, `output_overrun` or `decoder_exit`. The previous audio, if any, keeps playing.
- `audio producer failed: MediaMissingError`: the selected recording has no media manifest entry.
- `Playback could not start (capacity is busy ...)`: both audio banks are still held, possibly by cleanup that has not finished. Retry shortly.
- `next track could not be prepared; the program ends` (WARNING): the next recording could not be prepared. Either three attempts failed (decodes or voice failed), or no audio bank freed up in time. The two banks are shared by every server, and a track still being cleaned up keeps its bank. When both are held, the next recording waits for one: while the current recording plays, then for up to ten seconds after it ends, so there can be a gap of silence. The current recording's own end frees its bank, so two servers each playing a recording both continue, one gap at a time. A program whose recording has ended takes a freed bank before one that is still playing. The program ends after the current recording; `/now` explains it and `/play` starts a new one.
- `audio producer missed its cleanup deadline`: a decoder took longer than three seconds to settle. Its bank stays held until it actually settles, and shutdown then exits 1.
- `settings write failed: kind=<kind>` (WARNING): a change couldn't be saved, so nothing changed and the reply says so; playback continues. The kinds are those for exit 4, plus `busy`, when too many changes are waiting, and `too_many_servers`, at the limit of 1,000. If it continues, the volume may be full or no longer writable, and the next start may exit 4.
- If the bot loses its voice connection, playback commands report that it has not joined; use `/join` again.
- Discord shows "The application did not respond": Discord rejected the bot's acknowledgement, usually because it arrived after the three-second window. `/stop` and `/leave` still take effect (check with `/now`) and log `interaction acknowledgement failed: command=<name> age_seconds=<seconds> status=<http> code=<discord>`, where `code=10062` is Unknown interaction. If the acknowledgement request itself fails, for example on a network error or timeout, the line ends with `error=<exception type>` instead of the status and code, and the commands still take effect. The age counts from the interaction's creation, so it includes network and Discord delay as well as any delay in the bot. A rejected `/join` or `/play` starts nothing; repeat it.

### Crash dumps

SobaFM keeps its memory out of crash dumps, because the main process holds your Discord token, your Gemini key and audio. As its first step, before loading its dependencies, it makes itself non-dumpable. A crash of the main process then writes nothing to `/data` and sends nothing to a crash handler on the host, whatever the host's `kernel.core_pattern` says. No host setting is needed or changed.

The cost is that a crash of the main process **leaves no dump to debug**, only the container's exit status and the logs. There is no setting to turn this off. Tools that attach to the process, such as a debugger, are refused too.

The decoder is still stock mpg123 with its limits unchanged, and it isn't made non-dumpable. It inherits only a filter that keeps memory out of any dump it produces. Its core limit of zero means a host that writes dumps to files gets none. On a host that pipes dumps to a handler, such as apport or systemd-coredump, a decoder crash still starts the handler and can leave a small record there, holding registers and small notes such as mapped file names, but no memory contents. Whether SobaFM should require anything of the host's setting is an open question on [#133](https://github.com/slackysoba/sobafm/issues/133).

The init process and the moment before Python reaches SobaFM's first line aren't covered.

## Memory scope and exclusions

The limits implement the requirements' [approved memory definition](requirements.md#approved-memory-definition): the whole-service group (90,177,536 bytes) plus two globally owned 2,304,000-byte PCM banks plus one 4,194,304-byte audio/IPC-copy reservation, 98,979,840 controlled bytes. This is a controlled-capacity guarantee, **not** a total RSS or total physical-audio-memory ceiling. These are explicitly excluded, and total physical audio memory may exceed 100 MB:

- parent allocator and object overhead;
- native voice encoding, encryption and packet backing;
- kernel pipe and socket storage;
- the narrow decoder kernel PTY backing defined in [ADR 0003](decisions/0003-decoder-pty-exclusion.md) (this slice uses no PTY);
- uncharged shared or mapped backing.

Repeated sessions may leave an unquantified physical or kernel backlog. No kernel-release time bound or eventual reclamation is established. All servers share one container, so exhausting its memory limit can stop every station.

The settings database and its worker thread run inside the whole-service group: they count against the same 90,177,536 bytes and 64 tasks, and add no capacity or partition. Applying volume makes one more short-lived 3,840-byte copy of each frame, under the audio/IPC-copy reservation.

## Image contents

The image is built from the reviewed Python 3.14 base pinned by digest, the locked Python dependencies installed by uv 0.13.6, and the reviewed Debian mpg123 1.33.7 packages (from forky) with their stable dependencies, including libopus. It contains no FFmpeg. mpg123 is LGPL-2.1; see [the reviewed supply route](../evaluations/linux_resource/mpg123-proposal.md) and [Contributing](../CONTRIBUTING.md) for dependency licenses. The image is for local use; it is not published.

The build fetches packages from Debian over HTTPS with normal signature checks. Debian forky is a testing suite, so the pinned mpg123 1.33.7-1 packages may later leave it, and the build then fails rather than installing a different version. That needs a reviewed update, not a local workaround.
