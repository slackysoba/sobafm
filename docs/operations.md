# Operations

**Status: amd64 first slice, not yet qualified.** This guide describes the first playback slice for Linux amd64 with Docker Compose. The image at commit `ab10a71` ran in one private Discord session on Docker Desktop under WSL2 with the real catalog and Gemini ([#124](https://github.com/slackysoba/sobafm/issues/124)), after an earlier session at `d6923c8` ([#97](https://github.com/slackysoba/sobafm/issues/97)); that is not qualification of the image, its limits or the release. Treat it as a preview of the operator path, not a release. Native arm64 is on hold until after the first amd64 release ([#60](https://github.com/slackysoba/sobafm/issues/60)).

The slice joins a voice channel and plays a catalog program per `/play` request: the admitted local recordings Gemini ranks for the request, up to 20, one after another, until it is stopped, replaced, runs out of recordings or plays for a minute with no listener. `/skip` moves on to the next one, `/now` shows the heard recording's full credits, and it stops and shuts down cleanly. It has no settings, persistence or `/freestyle`.

## Requirements

- Linux amd64 with Docker Engine and the Compose plugin, using cgroup v2 (the default on current distributions). No compiler is needed.
- A Discord application with a bot user and its token.
- Optionally, a prepared catalog snapshot, its admission reviews, a media manifest, the admitted MP3 files and a Gemini API key. Without them the bot runs with `/join`, `/now`, `/stop` and `/leave` only; `/play` and `/skip` are not registered.

## Discord setup

1. In the Discord Developer Portal, create an application and add a bot. Copy its token; it goes only into `.env`.
2. No privileged gateway intents are needed. SobaFM requests only the Guilds and Voice States intents.
3. Invite the bot with the `bot` and `applications.commands` scopes and these permissions: **View Channels**, **Connect** and **Speak**. Replies are private interaction responses, so the bot needs no Send Messages permission.

Command access:

| Command | Who can use it |
| --- | --- |
| `/join`, `/leave` | Members with Manage Server by default. Server admins can change this under Server Settings, Integrations. |
| `/play request:<text>`, `/skip` | Members in the bot's voice channel. |
| `/stop` | Members in the bot's voice channel, or anyone with Manage Server. |
| `/now` | Anyone in the server. It privately shows what is actually heard, with full credits. |

## Install and configure

```sh
git clone https://github.com/slackysoba/sobafm.git
cd sobafm
cp .env.example .env
mkdir data
```

Edit `.env` and replace the placeholders. Never commit `.env`. The variables are:

| Variable | Required | Meaning |
| --- | --- | --- |
| `SOBAFM_DISCORD_TOKEN` | Yes | The bot token. SobaFM removes it from its own environment at startup, so the decoder never inherits it, and never logs it. |
| `SOBAFM_CATALOG` | With the next three | Prepared catalog snapshot (`sobafm-catalog-1`). |
| `SOBAFM_REVIEWS` | With the catalog | Trusted admission reviews (`sobafm-admission-1`). |
| `SOBAFM_MEDIA` | With the catalog | Media manifest locating each admitted recording's file. |
| `SOBAFM_GEMINI_API_KEY` | With the catalog | The key for matching requests. It is removed from the environment at startup, sent only as a request header and never logged. |

The catalog, reviews and media manifest must be set together, or none of them, and the Gemini key is required with them. Without them `/play` and `/skip` are not registered, and a key on its own is ignored. The files live in `./data`, which Compose mounts read-only at `/data`. The container runs as UID 1000, so that user must be able to read the files.

**What Gemini receives and what it costs.** Each `/play` makes one call containing the request text and each admitted recording's title, creators, length, labels and details. It contains no Discord identities or credits, and SobaFM asks Google not to store the interaction. `/skip` and continuation make no call. Cost grows with the catalog, because every call sends it; see [ADR 0006](decisions/0006-gemini-play-matching.md). Google's [pricing page](https://ai.google.dev/gemini-api/docs/pricing) says free-tier content may be used to improve its products and paid-tier content isn't. There is no cooldown setting yet: each server runs one match at a time, and only members in the bot's voice channel can use `/play`.

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

The service runs with Docker's default seccomp profile, a read-only root, all capabilities dropped, no new privileges, an init process, two CPUs and the [ADR 0004](decisions/0004-amd64-whole-service-adoption.md) limits: memory and memory-plus-swap 90,177,536 bytes and 64 tasks. Logs go to stdout and are kept in at most two 1 MB files. The restart policy is `on-failure:5`: Docker restarts a container that exits nonzero at most five times, so a refused start (exit 2 or 3) or a failed Discord login does not retry indefinitely. A clean shutdown (exit 0) is not restarted. After fixing the cause, start it again with `docker compose up -d`.

Before reading any configuration or connecting to Discord, SobaFM reads its own cgroup's `memory.max`, `memory.swap.max` and `pids.max` and refuses to start unless they are exactly 90177536, 0 and 64. Do not change those limits in `compose.yaml`: the app will refuse to run, and no other values are accepted.

Then, in Discord: a manager uses `/join` from a voice channel, a listener uses `/play` with any request, such as `a haunted carnival on the moon`, and `/now` shows the heard recording and its credits. The reply says how close the match is: the recording asked for, the closest match or just the nearest thing in the catalog. Audio becomes audible after six seconds of buffered PCM, or at the verified end of a shorter recording.

The request is interpreted once. While a recording plays, the next ranked recording the program has not yet played is prepared in the second audio bank and follows the last frame directly, with no fade or overlap. A program never repeats a recording: when none is left, it ends after the current one and `/now` says so; `/play` starts a new program. With a one-recording catalog that is the normal result once the recording ends. Gemini chooses how many recordings to rank, so a narrow request can also end the program after one recording. `/skip` switches to the next recording as soon as it is buffered; the current recording keeps playing until then, and keeps playing if the next one cannot be prepared. `/play`, `/stop`, `/leave` and shutdown discard a prepared next recording.

A program ends 60 seconds after the last listener leaves the bot's voice channel. A listener is a member in that channel who is neither a bot nor deafened, whether self-deafened or deafened by the server; muting does not matter. If a listener joins or undeafens before then, the program continues untouched. When the period ends, the program stops as with `/stop`: the bot stays connected and silent, and `/now` says the program ended because no listeners remained and that `/play` starts a new one. Moving the bot with `/join` to a channel with no listener starts the same period. Because the bot does not request the members intent, it cannot tell whether a member who was already in the channel when it connected, and has not changed voice state since, is a bot; such a member counts as a listener, so the program keeps playing for them until they leave or deafen.

## Stop

```sh
docker compose stop
```

SIGTERM stops playback on every server, disconnects, waits for every decoder to be killed and reaped, and releases the audio banks. The cleanup deadline is three seconds per track; Compose's default ten-second stop grace covers it. `docker compose down` also removes the container.

## Update

```sh
git pull
docker compose up --build -d
```

Read the release notes and `docs/requirements.md` first. Updating the files in `./data` needs a restart: `docker compose restart`.

## Build the FMA catalog

The catalog comes from the [FMA research dataset](https://github.com/mdeff/fma) (Defferrard, Benzi, Vandergheynst and Bresson, ISMIR 2017; its metadata is CC BY 4.0). [Decision 0007](decisions/0007-fma-dataset-batch-catalog.md) records the rule, the limits and what a review can't establish: the dataset says it's meant for research, it's a third-party copy, and nothing after its 2017 snapshot is known.

The catalog, reviews and manifest for the 167 accepted recordings are in [`evaluations/catalog/fma-2026-11/`](../evaluations/catalog/fma-2026-11/). The audio is never in Git or the image, so another machine has to fetch the files again, under its operator's own decision. Copy the three files into `./data` and the audio into `./data/fma/`.

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

**To remove a recording**, for example after a takedown or a rights conflict, remove its entry from the `reviews` array in `reviews.json` and delete its file from `./data/fma/`, then run `docker compose restart`. The file is one line of JSON, so edit the array rather than deleting a line: invalid JSON stops the whole service at startup with exit 2. Without a review entry the recording can't be chosen. No automatic recheck of rights or availability happens.

## Exit status and troubleshooting

| Exit | Log line | Meaning and fix |
| --- | --- | --- |
| 3 | `container limits refused: <file> must be <value>` | The cgroup limits differ from ADR 0004, or cgroup v2 is unavailable. Use the unchanged `compose.yaml` on a cgroup v2 host. |
| 2 | `configuration error: ...` | A missing token, an incomplete catalog/reviews/media/key set, or a file that is unreadable, too large or invalid, including a catalog too large to send for matching. Messages name variables, never values. |
| 1 | `... lease(s) remain at shutdown`, `cleanup deadline(s) were missed` or `Discord client stopped: <type>` | Shutdown could not confirm release, or the Discord client failed (for example an invalid token). |
| 0 | | Clean shutdown. |

INFO lines record what was played, without request text or tokens: `voice connected` and `voice disconnected`; `heard source changed: recording=<source:id> credit_snapshot=<id>` and `heard source cleared`; `program has no further recording in its plan`; `listener grace started: seconds=60` and `listener grace cancelled`; `program ended: reason=<exhausted|unavailable|no_listeners>`; one `track retired: ...` line per track with `frames_handed_to_voice` (frames given to the voice player, not packets confirmed delivered), `underruns` (silence frames returned while the track was playing or ready, including a momentarily busy bank), the completion facts, `failure`, `bank_released` and `cancel_to_release_seconds`; and `shutdown: released=... leases_held=... cleanup_failures=...`. At startup, `catalog source: source=<name> state=<state> provided=<n> admitted=<n>` reports each source; `admitted=0` everywhere means `/play` has nothing to choose from. Each request logs `request matched: outcome=<match|refuse> ranked=<n> seconds=<s>`.

During playback:

- `request matching failed: kind=<kind> seconds=<s>`: no plan was produced and the current audio continues. `http_401` or `http_403` usually means a wrong key, `http_429` a rate or quota limit, and `timeout` no answer within 10 seconds. `interaction_not_completed`, `response_shape` and `output_*` mean Gemini's answer wasn't usable, which may mean the API has changed.
- `decode failed: <kind>` names the decode failure, for example `input_hash` (the file differs from the manifest), `input_open`, `output_overrun` or `decoder_exit`. The previous audio, if any, keeps playing.
- `audio producer failed: MediaMissingError`: the selected recording has no media manifest entry.
- `Playback could not start (capacity is busy ...)`: both audio banks are still held, possibly by cleanup that has not finished. Retry shortly.
- `next track could not be prepared; the program ends` (WARNING): the next recording could not be prepared. Either three attempts failed (decodes or voice failed), or no audio bank freed up in time. The two banks are shared by every server, and a track still being cleaned up keeps its bank. When both are held, the next recording waits for one: while the current recording plays, then for up to ten seconds after it ends, so there can be a gap of silence. The current recording's own end frees its bank, so two servers each playing a recording both continue, one gap at a time. A program whose recording has ended takes a freed bank before one that is still playing. The program ends after the current recording; `/now` explains it and `/play` starts a new one.
- `audio producer missed its cleanup deadline`: a decoder took longer than three seconds to settle. Its bank stays held until it actually settles, and shutdown then exits 1.
- If the bot loses its voice connection, playback commands report that it has not joined; use `/join` again.
- Discord shows "The application did not respond": Discord rejected the bot's acknowledgement, usually because it arrived after the three-second window. `/stop` and `/leave` still take effect (check with `/now`) and log `interaction acknowledgement failed: command=<name> age_seconds=<seconds> status=<http> code=<discord>`, where `code=10062` is Unknown interaction. If the acknowledgement request itself fails, for example on a network error or timeout, the line ends with `error=<exception type>` instead of the status and code, and the commands still take effect. The age counts from the interaction's creation, so it includes network and Discord delay as well as any delay in the bot. A rejected `/join` or `/play` starts nothing; repeat it.

## Memory scope and exclusions

The limits implement the requirements' [approved memory definition](requirements.md#approved-memory-definition): the whole-service group (90,177,536 bytes) plus two globally owned 2,304,000-byte PCM banks plus one 4,194,304-byte audio/IPC-copy reservation, 98,979,840 controlled bytes. This is a controlled-capacity guarantee, **not** a total RSS or total physical-audio-memory ceiling. These are explicitly excluded, and total physical audio memory may exceed 100 MB:

- parent allocator and object overhead;
- native voice encoding, encryption and packet backing;
- kernel pipe and socket storage;
- the narrow decoder kernel PTY backing defined in [ADR 0003](decisions/0003-decoder-pty-exclusion.md) (this slice uses no PTY);
- uncharged shared or mapped backing.

Repeated sessions may leave an unquantified physical or kernel backlog. No kernel-release time bound or eventual reclamation is established. All servers share one container, so exhausting its memory limit can stop every station.

## Image contents

The image is built from the reviewed Python 3.14 base pinned by digest, the locked Python dependencies installed by uv 0.13.6, and the reviewed Debian mpg123 1.33.7 packages (from forky) with their stable dependencies, including libopus. It contains no FFmpeg. mpg123 is LGPL-2.1; see [the reviewed supply route](../evaluations/linux_resource/mpg123-proposal.md) and [Contributing](../CONTRIBUTING.md) for dependency licenses. The image is for local use; it is not published.

The build fetches packages from Debian over HTTPS with normal signature checks. Debian forky is a testing suite, so the pinned mpg123 1.33.7-1 packages may later leave it, and the build then fails rather than installing a different version. That needs a reviewed update, not a local workaround.
