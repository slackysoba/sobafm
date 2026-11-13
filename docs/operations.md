# Operations

**Status: amd64 first slice, release qualification incomplete.** This guide describes the Linux amd64 Docker Compose path. Historical Discord sessions on Docker Desktop under WSL2 used `d6923c8` ([#97](https://github.com/slackysoba/sobafm/issues/97)) and then `ab10a71` with the real catalog and Gemini ([#124](https://github.com/slackysoba/sobafm/issues/124)). Later network-disabled image checks covered settings persistence, volume ownership and backup/restore at `b5ef66e` ([#126](https://github.com/slackysoba/sobafm/issues/126#issuecomment-6679442387)); they did not exercise restart during playback. Current Discord controls, automatic refill, rejoin and recovery still need real integration evidence.

The [independently reviewed corrected L2 check](https://github.com/slackysoba/sobafm/issues/3#issuecomment-6789773813) passed five synthetic pairs/ten sessions in 27 seconds: application `01e2226adeeb49f676b525b9e903385f4960a1fd`, image `sha256:bd7d1aee495e612a8f7a168b767ce43eb996466d75e743e0f7997807fbb7fe23`, harness `37e2b8fbe5bef54f2884240571516779faf7eee0`, Docker Desktop linux/x86_64, WSL2 kernel `6.18.33.2-microsoft-standard-WSL2`, 4,096-byte pages. This establishes only that finite synthetic transport/buffering workload under its recorded limits, not current-main, full-station, provider, Discord or release qualification. The [first attempt failed at setup without running the workload](https://github.com/slackysoba/sobafm/issues/3#issuecomment-6789121839) and remains separate failed evidence.

The [one-shot Lyria access check](https://github.com/slackysoba/sobafm/issues/3#issuecomment-6795163301) passed in 17.756 seconds, consuming 200 frames/four seconds, with clean original-clock cleanup. It used application `01e2226adeeb49f676b525b9e903385f4960a1fd` in image `sha256:bd7d1aee495e612a8f7a168b767ce43eb996466d75e743e0f7997807fbb7fe23`, on Linux amd64/WSL2, under that image's historical limits. This establishes short free-tier access at that time, not current-main, ongoing quota, renewal, three-station fit, musical quality, Discord continuity or release readiness. The earlier failed attempt remains failed. The piped-host experiment #151 and native arm64 #60 remain deferred.

The slice joins a voice channel and plays a catalog program per `/play` request: the admitted local recordings Gemini ranks for the request, 8 at a time, one after another, until it is stopped, replaced, runs out of recordings or plays for a minute with no listener. `/skip` moves on to the next one, `/now` shows the heard recording's full credits, `/settings` keeps each server's volume and change cooldown, it rejoins its voice channel after a restart, and it stops and shuts down cleanly. With generated playback configured, `/freestyle` starts a timed Lyria program on the same station.

**Current catalog and concurrency limits.** The prepared catalog contains 167 FMA recordings, played from validated local files. `/play` uses one initial ranking call and prepared-catalog refills when Jamendo is disabled. With Jamendo enabled, it first interprets intent, searches source windows and ranks admitted results with untried FMA, retaining FMA fallback when the source is unavailable. Jamendo remains disabled by default and requires explicit configuration below. ccMixter remains disabled. One bot identity serves up to three stations, each reserving its own two producer/bank/copy slots and voice credits before model or source work. Pending requests and retained cleanup debt count; a fourth makes no external/native call. Idle joined channels reserve no pair. No station borrows another pair, and stop/restart cannot escape debt. [ADR 0012](decisions/0012-three-station-capacity.md) fixes the unchanged limits; current-image three-station fit remains unqualified.

## Jamendo discovery and checked streaming

When Jamendo is enabled, the runtime path interprets a musical request, searches a bounded window across Jamendo's permitted singles and album tracks, then ranks admitted results alongside the existing FMA library. Refills reuse intent. It adds a model step and sends validated musical query fields to Jamendo, without Discord identity. Tell listeners about that additional disclosure and repeated source/model calls; the existing [Google privacy and audience terms](provider-access.md) still apply.

The operator-enabled `jamendo-stream-1` rule defaults off. It provisionally accepts a checked ordinary listing with identity, license, full supplied credits and available notices; it does not certify completeness or undisclosed rights. Immediately before each audio open, a by-ID refresh must establish a fresh immutable decision and credits. Six-second readiness remains, with completed verification for shorter tracks. Leading tags are checked before readiness and later notices/integrity as they arrive. Some audio may already have been heard when corruption, truncation, additional required notices or a conflict is discovered. That attempt stops and discards its buffered audio, retains failure/cleanup ownership and uses bounded continuation. Known non-recording forms, missing required credits and rights conflicts remain ineligible. `/now` follows the actual heard source. FMA's exact list and reviews are unchanged.

Discovery keeps eight choices and three refill attempts with existing backoff. Search is limited to two sequential pages of 100 per attempt, with 1 MiB per metadata response, the 256-record/source envelope and 512,000-character matching ceiling. A program ends before evicting from its 768-key history, at 768 selection cycles or after three no-progress attempts. An empty window is not “the provider catalog is exhausted.” Count calls and bytes and respect actual account and configured operator limits; no 2 GB/day or 30,000-call/month allowance is accepted. Jamendo audio uses transient buffers, never an offline library or durable stream URLs; retain only necessary decision/credit provenance in a bounded journal.

The implementation is wired through [catalog search](catalog-search.md), [checked streaming](jamendo-stream.md) and the existing [transport/journal](jamendo-catalog.md). Configure a dedicated Jamendo Client ID, never a Client Secret. Audio remains metadata-only until the current recording has at most 30 seconds estimated remaining; skip or early drain wakes preparation. A failed refresh/preparation preserves current audio and credits. Actual-image/source compatibility, attribution delivery, three-station resource fit, musical quality, median first audio within 15 seconds and 60-minute continuity remain separate qualification. Configuration does not authorize live tests or spending.

## Requirements

- Linux amd64 with Docker Engine and the Compose plugin, using cgroup v2 (the default on current distributions). No compiler is needed.
- A Discord application with a bot user and its token.
- Optionally, a prepared catalog snapshot, its admission reviews, a media manifest, the admitted MP3 files and a Gemini API key. These enable `/play` and `/skip`.
- For generated playback, Gemini interpretation credentials and free-tier Lyria credentials. Generated-only operation needs no prepared catalog.

### Provider access

Read [provider access, privacy and free-tier limits](provider-access.md) before configuring Gemini or offering the bot to listeners. It covers the data sent by `/play` and automatic refill, Google's unpaid data use and human review, age and regional audience restrictions, and the maintainer's no-spend choice for Lyria. The generated command is implemented and tested offline. The short historical Lyria access result above does not establish continued access or current-image behavior. The guide lists the facts still needing an explicitly authorized live check.

## Discord setup

1. In the Discord Developer Portal, create an application and add a bot. Copy its token; it goes only into `.env`.
2. No privileged gateway intents are needed. SobaFM requests only the Guilds and Voice States intents.
3. Invite the bot with the `bot` and `applications.commands` scopes and these permissions: **View Channels**, **Connect** and **Speak**. Replies are private interaction responses, so the bot needs no Send Messages permission.

Optionally grant **Set Voice Channel Status** in the voice channel. Once the player starts a recording, SobaFM shows its catalog title and source, or just the source if the title contains link or mention syntax. Generated playback shows “Generated music”. Use private `/now` for details and full catalog credits. Stage channels keep their own topic. Status can lag during a slow request or rate limit. Each channel's next update waits for the outstanding write to settle. When playback ends or moves, SobaFM tries to clear a status that Discord acknowledged setting. If a response is lost after dispatch, Discord may have applied the write. The response timeout ends that attempt and the displayed status is uncertain. Discord can also refuse a clear after the bot has left unless it already has Manage Channels there. A moderator may need to clear an old status. Playback continues through these failures. The status route has been exercised only against a local test server. [Discord's status endpoint and permissions](https://github.com/discord/discord-api-docs/blob/da52492ced82d375981c46618cc3e182daada9a2/developers/resources/channel.mdx#set-voice-channel-status)

Command access:

| Command | Who can use it |
| --- | --- |
| `/join`, `/leave`, `/settings` | Members with Manage Server by default. Server admins can change this under Server Settings, Integrations. |
| `/play request:<text>`, `/freestyle request:<text>`, `/skip` | Members in the bot's voice channel. |
| `/stop` | Members in the bot's voice channel, or anyone with Manage Server. |
| `/now` | Anyone in the server. It privately shows the heard recording’s credits or the heard generated title and time remaining. For longer credits, use **Choose a credit page** to read any page of that recording, even after the music changes. The menu expires after ten minutes and may close sooner when many listeners open credits. Use `/now` again for the current recording. If all 256 menu slots are sending, the bot asks you privately to retry shortly. |

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
| `SOBAFM_CATALOG` | With the catalog | Prepared catalog snapshot (`sobafm-catalog-1`). |
| `SOBAFM_REVIEWS` | With the catalog | Trusted admission reviews (`sobafm-admission-1`). |
| `SOBAFM_MEDIA` | With the catalog | Media manifest locating each admitted recording's file. |
| `SOBAFM_GEMINI_API_KEY` | With either music mode | The key for interpreting requests. It is removed from the environment at startup, sent only as a request header and never logged. |
| `SOBAFM_LYRIA_API_KEY` | With generated playback | Key for the operator’s free-tier Lyria project. Removed from the environment at startup. Omit it for catalog-only operation. |
| `SOBAFM_JAMENDO_ENABLED` | No; defaults `false` | Exactly `true` enables the locally accepted `jamendo-stream-1` policy; other values are rejected. |
| `SOBAFM_JAMENDO_CLIENT_ID` | When Jamendo is enabled | Dedicated application Client ID, removed from the environment and omitted from representations. Client Secret is unsupported. |
| `SOBAFM_JAMENDO_JOURNAL` | When Jamendo is enabled | Exclusive journal path on `/data`, for example `/data/jamendo.json`. Invalid or interrupted state refuses startup. |
| `SOBAFM_JAMENDO_MAX_REQUESTS` | Optional | Nonnegative aggregate request ceiling for this journal, including uncertain reservations. No automatic period reset. |
| `SOBAFM_JAMENDO_MAX_BODY_BYTES` | Optional | Nonnegative aggregate received application-body byte ceiling, including conservative outstanding reservations; not wire billing. |

The local catalog, reviews and media manifest remain a set. Jamendo-only operation needs no local catalog or media files: omit those three variables and explicitly enable Jamendo with its ID, journal and Gemini key. Either local FMA or enabled Jamendo registers `/play` and `/skip`. With neither, those commands are omitted; generated-only operation still works. Unset quota ceilings invent no allowance and impose no application ceiling on that metric; configure limits consistent with actual account restrictions before deployment. Zero refuses that usage. No daily/monthly allowance or automatic reset is inferred. Never delete the journal to regain quota: crash-uncertain reservations stay charged, and unresolved/quarantined attempts require evidence-specific operator recovery.

**Where things live** ([ADR 0009](decisions/0009-writable-data-volume.md)):

| Container path | On the host | Mode | Holds |
| --- | --- | --- | --- |
| `/catalog` | `./catalog` | Read-only | The catalog, reviews, media manifest and audio. The container runs as UID 1000, so that user must be able to read them. |
| `/data` | The named volume `sobafm-data` | Writable | `sobafm.db`, its brief SQLite journal, and the optional bounded Jamendo provenance/usage journal. Compose creates the volume owned by UID 1000, so it needs no setup. |

The rest of the container's filesystem is read-only.

**What providers receive.** With Jamendo disabled, a new local-catalog `/play` makes one ranking call; refills send the original request and remaining prepared metadata, with no separate intent call or source request. With Jamendo enabled, a new `/play` makes one validated musical-intent call, then at most one pooled ranking call per selection attempt. Discovery refills reuse intent and repeat bounded source search/ranking without a new command; skip never reinterprets or waits for model work. Gemini receives musical request/intent and descriptive candidate metadata, never audio, Discord identities, full credit snapshots or credentials. Enabled Jamendo receives validated musical query fields. The shared model client allows four calls, each bounded to ten seconds; two source pages add up to twenty seconds. `store=false` does not opt out of unpaid data use or human review. [Provider access](provider-access.md) covers audience restrictions. Historical fixed-catalog price/latency estimates do not price or qualify this pipeline.

The media manifest binds each playable recording to one local file:

```json
{
  "version": 1,
  "recordings": [
    {
      "key": {"source": "fma", "recording_id": "example-id"},
      "file": "fma/example-id.mp3",
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

The service runs with Docker's default seccomp profile, a read-only root, all capabilities dropped, no new privileges, an init process, two CPUs and the [ADR 0012](decisions/0012-three-station-capacity.md) accepted limits: memory and memory-plus-swap 268,435,456 bytes and 64 tasks. Logs go to stdout and are kept in at most two 1 MB files. The restart policy is `on-failure:5`: Docker restarts a container that exits nonzero at most five times, so a refused start (exit 2, 3, 4 or 5) or a failed Discord login does not retry indefinitely. A clean shutdown (exit 0) is not restarted. After fixing the cause, start it again with `docker compose up -d`.

Before reading any configuration or connecting to Discord, SobaFM reads its own cgroup's `memory.max`, `memory.swap.max` and `pids.max` and refuses to start unless they are exactly 268435456, 0 and 64. Do not change those limits in `compose.yaml`: the app will refuse to run, and no other values are accepted.

Then, in Discord: a manager uses `/join` from a voice channel, a listener uses `/play` with any request, such as `a haunted carnival on the moon`, and `/now` shows the heard recording and its credits. The reply says how close the match is: the recording asked for, the closest match or just the nearest thing in the catalog. Audio becomes audible after six seconds of buffered PCM, or at the verified end of a shorter recording.

A listener in the same voice channel can use `/freestyle` with up to 200 characters. A relative direction such as “faster” uses the current generated plan. Switching from catalog music starts a fresh interpretation. A refusal leaves current music playing. If a catalog request fails during interpretation, the pending generated request can still start. An interpretation failure uses the request as written, even for a relative direction, and the reply discloses that fallback. The reply and `/now` distinguish preparation from the first frame handed to voice. An already accepted replacement can wait up to ten seconds for an audio bank while the earlier tracks close. If voice is reconnecting, the accepted replacement keeps its place and cooldown until the connection recovers, ends or the request is superseded or stopped. Two generated programs crossfade. A switch involving a catalog recording has no overlap. While a catalog program is still current, `/skip` remains available during interpretation of a generated direction. If a generated feed drains before the next one starts, `/now` shows recovery and the remaining program time until the next frame reaches voice. A provider refusal before playback gives back that request’s cooldown even when voice is reconnecting. `/stop` fades generated music for up to two seconds.

While a recording plays, the next one on the program's list is prepared in the second audio bank and follows the last frame directly, with no fade or overlap. `/skip` switches to the next recording as soon as it is buffered; the current recording keeps playing until then, and keeps playing if the next one cannot be prepared. `/play`, `/stop`, `/leave` and shutdown discard a prepared next recording.

**Song lists refill as the program plays** ([ADR 0011](decisions/0011-program-refill.md)). Gemini is asked for 8 recordings at a time, best first, so later ones may fit the request less well; the reply's description of the fit is about the first recording only. When the last recording on the list starts, the bot asks for 8 more in the background, leaving out what the program has played. Music keeps playing while it waits. A `/skip` then doesn't wait for the answer: if nothing is ready, it says the next recording could not be prepared, the current one continues, and the skip holds no cooldown. `/play` for a new program, `/stop`, `/leave`, the end of the listener grace period, losing voice and shutdown cancel the call, and an answer that still arrives changes nothing.

If the call fails, is refused or brings nothing new, the bot tries twice more while the current recording plays: after 15 seconds and then after up to 60, so a per-minute rate limit can pass. On a short recording the second wait shortens so the next one can still be ready in time. If those fail too, the program ends after the current recording, or at once if nothing is playing by then, and `/now` says Gemini did not supply more recordings. `/play` starts a new program. A program waiting in silence for more recordings keeps its own station reservation; another server retains its independent pair. A program never repeats a recording: once every admitted recording has been played or tried, it ends after the last one and `/now` says so. With a one-recording catalog that is the normal result once the recording ends.

A program ends 60 seconds after the last listener leaves the bot's voice channel. A listener is a member in that channel who is neither a bot nor deafened, whether self-deafened or deafened by the server; muting does not matter. If a listener joins or undeafens before then, the program continues untouched. When the period ends, the program stops as with `/stop`: the bot stays connected and silent, and `/now` says the program ended because no listeners remained and that `/play` starts a new one. Moving the bot with `/join` to a channel with no listener starts the same period. Because the bot does not request the members intent, it cannot tell whether a member who was already in the channel when it connected, and has not changed voice state since, is a bot; such a member counts as a listener, so the program keeps playing for them until they leave or deafen.

## Voice channel memory

SobaFM remembers each server's voice channel in the settings database and rejoins it after a restart.

- **`/join`** checks that the bot has Connect and Speak in the channel, saves it, then connects or moves there. If the check or the save fails, nothing changes, and the reply says so. A connection attempt takes at most 45 seconds. If it fails, the channel remembered before stays remembered and the attempt leaves nothing behind, so you can try again at once.
- **Moving with `/join`** is a request to Discord, so the reply says "Moving to" rather than that it has happened. If Discord doesn't carry it out, for example because the channel is full, the bot stays where it is with the new channel remembered. A restart then tries that channel, and forgets it if it can't connect.
- **`/leave`** forgets the channel, then disconnects. If the save fails, the bot stays connected and current audio continues, because leaving anyway would bring it back at the next restart. The reply says so, and `/stop` still ends the program. If the bot isn't connected but a channel is remembered, `/leave` forgets it.
- **After a restart**, once Discord first reports ready, SobaFM rejoins each remembered channel once: up to four at a time, with at most two attempts of up to 45 seconds each. A gateway resume or a later ready event doesn't repeat this. No program resumes. The bot stays connected and idle at the saved volume until someone uses `/play`.
- **A channel it can't rejoin is forgotten**, with the reason in the log: the channel is gone, the bot has left the server, it lacks Connect or Speak there, or the attempts failed. A manager then uses `/join` again. If the server is in an outage at that moment, there's no answer yet, so the channel stays remembered for the next start.
- **A move by an administrator is adopted.** The new channel is remembered and current audio carries on from where it was after a short gap while the connection moves. Nothing is skipped, and `/now` shows the same recording. Listeners are counted again in the new channel, so with none there the 60-second period starts.
- **A disconnection or a deleted channel ends the program** as `/stop` does, and forgets the channel. That covers being kicked, the bot being removed from the server, a connection Discord's library gives up on, and the channel being deleted. The bot stays out of voice until someone uses `/join`. After a kick it can take up to 20 seconds to notice, because the library first waits to see whether it's a move.
- **A reconnect isn't a disconnection, and it may take time.** The library recovers on its own and the program carries on with nothing skipped, though a few frames can be lost when Discord moves the voice server. If the current recording has already ended, the next one waits for the connection, so the silence can last as long as the gap. Each connection attempt is bounded at 45 seconds, but the library waits before retrying: on a connection that keeps dropping, that backoff can reach 1,024 seconds (17 minutes 4 seconds). A voice-session resume also has a 45-second bound. If it times out, SobaFM closes the failed resume connection and waits for its heartbeat to end before the normal reconnect begins. Cleanup, backoff and the next connection attempt can all add to the silence. If that reconnect fails or takes too long, it counts as a disconnection and ends the program. During the gap, `/play`, `/skip`, `/stop` and `/now` report that the bot isn't in a voice channel. A manager can use `/join` to start the connection afresh and end the old program, or `/leave` to stay disconnected; both promptly wake any old next-track wait. A silent reconnecting server does not hold priority for a free audio bank over a healthy server, though any bank it already owns stays charged until released.
- **A new gateway session forgets channels.** After a long outage Discord may start a new gateway session rather than resume the old one. The library then drops its voice connections, and each one's next reconnect fails within 20 seconds. That ends the program and forgets the channel like any other disconnection, on every affected server, and rejoining doesn't run again because it runs once per process. A manager uses `/join` again.

While a `/join`, `/leave` or rejoin is under way in a server, another `/join` or `/leave` there is refused with a request to try again, so they can't interleave.

Each connected server uses at least one of the service's 64 tasks for its voice keep-alive. A bot remembered in dozens of servers could exhaust them at startup. A channel it can't connect to for that reason stays remembered, logged as `outcome=kept reason=local_failure`, and the next start tries again. A `/join` at that limit fails with its usual reply.

## Server settings

`/settings` privately shows the server's values. Give it any of its options to change them:

| Option | Setting | Default | Range |
| --- | --- | --- | --- |
| `volume` | Playback volume, as a linear gain | 50% | 1 to 100 |
| `cooldown` | Change cooldown, in seconds | 30 | 0 to 600 |
| `duration` | Generated duration, in minutes | 60 | 5 to 240 |

Settings are saved before they take effect, and each server keeps its own. If saving fails, the reply says so and nothing changes.

**Volume** applies to current audio from its next 20 ms frame, with no restart. It can only lower the level, so it can't clip, and 100% leaves the recording's samples unchanged. It defaults to 50%, so a recording plays at half the level it would without this setting.

**Change cooldown** is one wait per server across `/play`, `/freestyle` and `/skip`, whoever asked. A request during it is refused with the time left, before Gemini is called or a recording is chosen, and current audio continues. It counts from when the last accepted request arrived, and refused requests don't extend it. A request that fails or changes nothing doesn't hold it: an invalid request, one Gemini refuses or fails on, a `/skip` with nothing to skip, or a recording that can't start. A `/stop` doesn't clear it, unless the request it interrupts hasn't been answered yet: a `/play` still being matched, or a `/skip` still waiting up to five seconds for its next track. A changed `cooldown` applies at once, including to a wait already running, and 0 turns it off. The wait itself isn't saved, so it starts afresh after a restart or when the bot leaves voice.

**Generated duration** counts from request acceptance, including interpretation and pre-roll. Each new generated request uses the setting then in effect. A failed replacement leaves the previous program on its original clock.

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

SIGTERM stops playback on every server, disconnects, waits for every decoder to be killed and reaped, releases the audio banks and closes the settings database. The cleanup deadline is three seconds per track, and closing the database waits at most five seconds; Compose's default ten-second stop grace covers both. Stopping forgets nothing, so the next start rejoins the remembered channels. `docker compose down` also removes the container and keeps the settings. `docker compose down -v` also deletes the `sobafm-data` volume, and with it every server's settings and remembered channel.

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
| `fma_build.py verify` | No | Reads each file's tags and decodes it with the real producer. Run it at the service limits: `--network none --memory 268435456 --memory-swap 268435456 --pids-limit 64` |
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
- `unknown_schema_version`: the file is from a newer SobaFM. Use that version or restore an older copy. A file from an older SobaFM upgrades itself at the next start, and an older SobaFM then refuses it, so back it up first if you may need to go back.
- `invalid_row` or `too_many_servers`: the file holds values SobaFM would never write, so it was edited by something else. Restore a copy.

INFO lines record what was played, without request text or tokens: `voice connected` and `voice disconnected`; `heard source changed: recording=<source:id> credit_snapshot=<id>` and `heard source cleared`; `program refilled: recordings=<n> attempt=<n>` when more recordings arrive; `program has no recording left in the catalog`; `listener grace started: seconds=60` and `listener grace cancelled`; `program ended: reason=<exhausted|refill_failed|unavailable|capacity_wait|no_listeners>`; one `track retired: ...` line per track with `frames_handed_to_voice` (frames given to the voice player, not packets confirmed delivered), `underruns` (silence frames returned while the track was playing or ready, including a momentarily busy bank), the completion facts, `failure`, `bank_released` and `cancel_to_release_seconds`; `settings saved: generated_minutes=<n> volume_percent=<n> cooldown_seconds=<n>`; `voice rejoin: outcome=<joined|forgotten|kept>`, with `reason=<channel_gone|guild_gone|no_permission|connect_failed|guild_unavailable|local_failure>` unless it joined; `voice rejoin attempt failed: <type>`; `voice connection ended: reason=<disconnected|channel_deleted>`; `voice channel adopted`; and `shutdown: released=... leases_held=... cleanup_failures=...`. None of these name a server or channel. At startup, `settings database opened: servers=<n> voice_channels=<n>` reports how many servers have saved settings and remembered channels, and `catalog source: source=<name> state=<state> provided=<n> admitted=<n>` reports each source; `admitted=0` everywhere means `/play` has nothing to choose from. A completed match or refusal, whether from `/play` or a refill, logs `request matched: outcome=<match|refuse> ranked=<n> seconds=<s>`.

### Playback requests and recovery

| Signal | What to do |
| --- | --- |
| Missing `/play` or `/freestyle` | For catalog playback, check the catalog, reviews and media paths and the Gemini key configuration. For generation, check the Gemini and free-tier Lyria key configuration. Restart after correcting the configuration and allow Discord to synchronize commands. |
| The bot asks you to join its channel | Join the bot's voice channel. If the bot is disconnected, a manager can use `/join` in a channel where the bot has Connect and Speak. |
| The catalog has no recordings ready | Check the startup counts for sources with `admitted=0`, then check the prepared snapshot and its reviews. If matching found candidates whose credits cannot be displayed, check their credit records. A “nearest thing” reply means a loose fit was **accepted** and is preparing. |
| The interpreter declined | Try another musical description. A catalog `blocked=True` or freestyle `outcome=blocked` can concern the input or the model's answer. Existing audio keeps playing. |
| Interpreter `http_401` or `http_403` | Check that the intended Gemini key and project have access to the configured model. The status alone cannot identify a particular key, project or regional problem. Keep key values out of reports. |
| Interpreter `http_402` or `http_404` | For 402, the service requested payment. Check the intended free-tier project and leave billing unchanged. For 404, check the configured model and endpoint. |
| Interpreter `http_429` | A rate or quota limit was reported. Wait before another request. If it persists, check the project's free-tier usage and limits. |
| Interpreter `http_5xx`, `timeout` or `http_408` | A 5xx is a server error. A timeout can include a wait for a local call slot. Try again later. For repeated `transport_error`, check the service's network path. |
| Another interpreter failure, including `response_shape`, `output_*` or an unrecognized HTTP 400 | The call produced no usable interpretation. If it persists, check the reported kind and the configured API revision. An error body may contain private text. Share only the safe kind. |

These interpreter kinds appear in `request matching failed` for the catalog and `freestyle interpretation fell back` for ordinary generated-mode interpretation failures. A failed initial catalog match leaves current playback alone. On a generated fallback the bot uses the request as written, including a relative request, without the previous plan. Check `/now` for the first frame or a terminal reason.

A catalog `program refill failed: attempt=<n>` means the call failed, was refused or returned nothing new. After the third attempt, any current song finishes and `reason=refill_failed` ends the program. Check the matching kind if one was logged, address it and use `/play` to start again. With `reason=exhausted`, every eligible recording was played or tried.

For generation, `lyria session stopped: failure=<kind>` describes one session. `lyria stream cannot continue: failure=<kind>` gives the final observation. When attempts run out, the final kind describes the last attempt. A terminal refusal from the audible session can also end recovery while a replacement is waiting to retry. In that case the final kind describes the refusal. Buffered audio can still play out. Check `/now` to see whether an earlier recording is still playing after a failed replacement.

| Terminal Lyria kind | Recovery |
| --- | --- |
| `access_denied` | The handshake returned 401 or 403. Check the configured Lyria project's free-tier model access. If free-tier access cannot serve the model, take the decision back to the maintainer. Keep billing settings unchanged. |
| `rate_limited` or `service_error` | A 429 handshake reports a rate or quota limit. An HTTP 5xx or a WebSocket 1011 reports a server error. Recovery uses the existing bounded attempts. Wait before another `/freestyle`. For repeated 429s, check free-tier usage. |
| `filtered` | The service filtered the direction. Try a different musical description. |
| `rejected` | The service declined the session, for example an HTTP 404, a redirect or a WebSocket policy close. Check the configured model and endpoint. The signal does not establish a key, quota or content problem. |
| `capacity`, `connect`, `stalled`, or another stream failure | For capacity, wait for an audio bank to return. For connection or audio failures, try again later and check the safe session kind if it recurs. |

Offline checks exercise these replies with fake interactions and local provider responses. Actual free-tier access, live provider responses, Discord delivery and audible recovery require separately authorized verification. See [provider access](provider-access.md).

#### Other playback signals

- `decode failed: <kind>` names the decode failure, for example `input_hash` (the file differs from the manifest), `input_open`, `output_overrun` or `decoder_exit`. The previous audio, if any, keeps playing.
- `audio producer failed: MediaMissingError`: the selected recording has no media manifest entry.
- `Audio capacity is busy` means both audio banks are held. Retry after one is released. If the voice player could not start, check the connection and try again. If an accepted request fails before its first frame, check `/now` for its reason alongside any audio still heard. A missing manifest entry names a catalog problem. Check the prepared manifest and retry after correcting it.
- `next track could not be prepared; the program ends` (WARNING): the next recording could not be prepared. The preparation attempts ran out. Check the preceding decode or voice failures. A separate `reason=capacity_wait` means no audio bank freed up in time. Each station owns two banks, and a track still being cleaned up keeps its bank. When both are held, the next recording waits for one: while the current recording plays, then for up to ten seconds after it ends, so there can be a gap of silence. Actual settlement of the current recording frees its own bank. Another guild cannot take that slot or lend this station its own capacity. The program ends after the current recording; `/now` explains it and `/play` starts a new one.
- `audio producer missed its cleanup deadline`: a decoder took longer than three seconds to settle. Its bank stays held until it actually settles, and shutdown then exits 1.
- `settings write failed: kind=<kind>` (WARNING): a change couldn't be saved, so nothing changed and the reply says so; playback continues. The kinds are those for exit 4, plus `busy`, when too many changes are waiting, and `too_many_servers`, at the limit of 1,000. If it continues, the volume may be full or no longer writable, and the next start may exit 4.
- `voice connection ended: reason=disconnected`: the bot was kicked or removed, or its connection couldn't be recovered. The program ended and the channel is forgotten; use `/join` again.
- `voice connection failed: <type>`: a `/join` couldn't connect, or a move couldn't be sent, and the reply says so. Nothing is left behind, so it can be tried again at once.
- Discord shows "The application did not respond": Discord rejected the bot's acknowledgement, usually because it arrived after the three-second window. `/stop` and `/leave` still take effect (check with `/now`) and log `interaction acknowledgement failed: command=<name> age_seconds=<seconds> status=<http> code=<discord>`, where `code=10062` is Unknown interaction. If the acknowledgement request itself fails, for example on a network error or timeout, the line ends with `error=<exception type>` instead of the status and code, and the commands still take effect. The age counts from the interaction's creation, so it includes network and Discord delay as well as any delay in the bot. The SDK warning `Discord rate limited an interaction response.` records a 429 without the webhook ID or response fields. A rejected acknowledgement for `/join`, `/play` or `/freestyle` starts nothing. Repeat the command. If a playback request instead logs `interaction follow-up failed`, the reply could not be delivered after processing. Check `/now` before repeating it.

### Crash dumps

SobaFM keeps its memory out of crash dumps, because the main process holds your Discord token, your Gemini key and audio. As its first step, before loading its dependencies, it makes itself non-dumpable. A crash of the main process then writes nothing to `/data` and sends nothing to a crash handler on the host, whatever the host's `kernel.core_pattern` says. No host setting is needed or changed.

The cost is that a crash of the main process **leaves no dump to debug**, only the container's exit status and the logs. There is no setting to turn this off. Tools that attach to the process, such as a debugger, are refused too.

The decoder is still stock mpg123 with its limits unchanged, and it isn't made non-dumpable. It inherits only a filter that keeps memory out of any dump it produces. Its core limit of zero means a host that writes dumps to files gets none. On a host that pipes dumps to a handler, such as apport or systemd-coredump, a decoder crash still starts the handler and can leave a small record there, holding registers and small notes such as mapped file names, but no memory contents. Whether SobaFM should require anything of the host's setting is an open question on [#133](https://github.com/slackysoba/sobafm/issues/133).

The init process and the moment before Python reaches SobaFM's first line aren't covered.

## Memory scope and exclusions

The accepted contract in [ADR 0012](decisions/0012-three-station-capacity.md) sets replacement numeric guarantees: the whole-service group (268,435,456 bytes) plus six 2,304,000-byte PCM banks plus 12,582,912 audio/IPC-copy bytes, **294,842,368 controlled bytes**. Conditional aggregate PCM is **101,552,640 bytes**, including six native decoder spaces under the unchanged per-decoder limits. Each of three stations owns two producer/bank/copy slots and its own voice-copy allowance. Every playing server charges the full global totals, never one third. Actual-image fit remains unmeasured; numeric acceptance does not establish qualification. This is controlled capacity, **not** a total RSS, host-RAM recommendation or physical-audio-memory ceiling. These remain explicitly excluded:

- parent allocator and object overhead;
- native voice encoding, encryption and packet backing;
- kernel pipe and socket storage;
- the narrow decoder kernel PTY backing defined in [ADR 0003](decisions/0003-decoder-pty-exclusion.md) (this slice uses no PTY);
- uncharged shared or mapped backing.

Repeated sessions may leave an unquantified physical or kernel backlog. No kernel-release time bound or eventual reclamation is established. All servers share one container, so exhausting its memory limit can stop every station.

The settings database and its worker thread run inside the whole-service group: they count against the same 268,435,456 bytes and 64 tasks, and add no capacity or partition. Applying volume makes one more short-lived 3,840-byte copy of each frame, under the station's audio/IPC-copy reservation. Stopped or disconnected stations retain reservations while requests, bank/native/session cleanup or voice frames remain owned. Another guild cannot borrow that capacity, even after a missed deadline. The original three-second cleanup clock and failed verdict remain unchanged.

## Image contents

The image is built from the reviewed Python 3.14 base pinned by digest, the locked Python dependencies installed by uv 0.13.6, and the reviewed Debian mpg123 1.33.7 packages (from forky) with their stable dependencies, including libopus. It contains no FFmpeg. mpg123 is LGPL-2.1; see [the reviewed supply route](../evaluations/linux_resource/mpg123-proposal.md) and [Contributing](../CONTRIBUTING.md) for dependency licenses. The image is for local use; it is not published.

The build fetches packages from Debian over HTTPS with normal signature checks. Debian forky is a testing suite, so the pinned mpg123 1.33.7-1 packages may later leave it, and the build then fails rather than installing a different version. That needs a reviewed update, not a local workaround.

For the source-only release decision, inspected package versions, licenses, notices and update procedure, see [Supply chain](supply-chain.md).
