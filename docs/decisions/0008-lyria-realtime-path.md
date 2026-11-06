# 0008: The Lyria RealTime path

- Status: **Proposed, not accepted.** The maintainer's answer to the [question on issue #128](https://github.com/slackysoba/sobafm/issues/128) is still needed.
- Date: 2026-11-05
- Scope: [issue #128](https://github.com/slackysoba/sobafm/issues/128)
- Builds on: the capacity contract in [ADR 0004](0004-amd64-whole-service-adoption.md) and its [copy partitions](../linux-feasibility.md#capacity-ownership-and-admission), which this record leaves unchanged
- Sources read on 2026-11-05: the [realtime guide](https://ai.google.dev/gemini-api/docs/realtime-music-generation), [API reference](https://ai.google.dev/api/live_music), [model page](https://ai.google.dev/gemini-api/docs/models/lyria-realtime-exp), [pricing](https://ai.google.dev/gemini-api/docs/pricing), [rate limits](https://ai.google.dev/gemini-api/docs/rate-limits), [terms](https://ai.google.dev/gemini-api/terms), the [Live API session guide](https://ai.google.dev/gemini-api/docs/live-session), the [cookbook notebook](https://github.com/google-gemini/cookbook/blob/main/quickstarts/Get_started_LyriaRealTime.ipynb) and the [SDK source](https://github.com/googleapis/python-genai/blob/main/google/genai/live_music.py)

No call was made to Lyria, Gemini or Discord for this record, and `/freestyle` is not wired to anything. Everything here about the service comes from documentation. [What's established](#what-a-fake-cant-establish) lists what that leaves open.

## Problem

Outcome 3 needs generated audio to reach the player: continuously, for up to 240 minutes, with fades, and inside the 98,979,840-byte capacity contract. Lyria RealTime sends base64 audio in JSON over a WebSocket, so one message of audio exists several times in memory before it reaches a bank. The contract's copy pool has explicit owners, and any path that can't bound a message before allocating it [fails qualification](../linux-feasibility.md#capacity-ownership-and-admission).

## What the documentation establishes

- **Model and API.** `models/lyria-realtime-exp` over a WebSocket at `/ws/google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateMusic`. The guide calls the model experimental and the reference calls the API preview. The model page's latest update is May 2025. Google says experimental models aren't usually suited to production and their availability can change. The cookbook still uses `v1alpha`; the reference and guide use `v1beta`, which this pins.
- **Audio.** Raw 16-bit PCM at 48 kHz stereo, which is SobaFM's format, so no resampling is needed. One JavaScript sample shows 44,100 Hz, against the specification. The cookbook's recorded WAVs are 48 kHz.
- **Message size.** Not documented. The cookbook's recorded output settles it for planning: ten messages produced exactly 3,840,000 bytes, so one message is two seconds, 384,000 PCM bytes and 512,000 bytes of base64.
- **Session limit.** Not documented for music, and the music API has no resumption or GoAway message. The Live API documents about ten minutes per connection.
- **Protocol.** `setup` first, then wait for `setupComplete`; then `clientContent.weightedPrompts`, `musicGenerationConfig` and `playbackControl`. The server sends `serverContent.audioChunks[]`, `filteredPrompt` and `warning`. The SDK sends the key as an `x-goog-api-key` header. Output is generated as fast as possible rather than in real time, and clients are told to buffer.
- **Cost.** The pricing page has no entry for this model, so its cost isn't published. For comparison, the non-streaming Lyria 3.5 costs $0.08 per song with no free tier. Rate limits for experimental models are tighter, and the specific ones are only visible in AI Studio. Concurrent session limits aren't documented, and renewal needs two at once.
- **Terms.** The API is for developers building for professional or business purposes, not consumer use. Users must be 18 or over, and it may not be part of a service directed at or likely to be used by under-18s. Reaching users in the EEA, Switzerland or the UK requires paid services. Unpaid use lets Google use prompts and output to improve its products, with human review; paid use doesn't, but logs them for a limited time for abuse detection. These terms apply to the Gemini matching in [ADR 0006](0006-gemini-play-matching.md) too, which doesn't record them.
- **Data handling.** Only prompts and controls are sent. There is no `store` field, so the requirement to disable provider storage has nothing to set here. Output is always watermarked. The model's music is instrumental, and its `VOCALIZATION` mode adds voices as another instrument, which should fit the wordless vocals the requirements allow. A live check should confirm it produces no lyrics.

## Options

**Generation path, which is the maintainer's choice:**

1. **Lyria RealTime, streaming.** One continuous stream steered by prompts. No decoder involved, and the audio is already in the player's format. Its price and limits are unknown and it is experimental.
2. **Lyria 3.5, one song at a time.** A published price and a model not marked experimental. At two to three minutes a song, a 60-minute program costs about $1.60 to $2.40, and 240 minutes four times that, with no free tier. Its MP3s would go through the existing decoder, and each song takes time to generate, which changes the continuity design. Little here would apply.
3. **Wait** for a stable real-time model. Generated mode, and with it Outcome 3, waits too.

**Transport, within option 1:**

| Option | Runtime closure | Service RSS at import | Can bound a message before allocating it |
| --- | --- | --- | --- |
| A. `google-genai` 3.0.0 (Apache-2.0) | 16 to 38 distributions, adding native `cryptography` and `pydantic-core` | About 84,800 KiB (+30,600) | No: it passes no limits to `websockets`, whose defaults allow 1 MiB per message and a queue of 16 |
| B. `websockets` 17.4 directly (BSD-3-Clause) | 17 | About 55,600 KiB (+1,400) | Yes, with the same TLS staging as C, and it would follow redirects with the key |
| C. `aiohttp` 3.14.5, already locked | 16, no change | About 54,200 KiB (no change) | Yes |

Import memory was measured on Python 3.14 under WSL2 x86_64: `disnake` first, then the candidate, with compiled bytecode as the image has, taking resident memory from `/proc/self/statm` and three runs. The service's group limit is 88,064 KiB, so the SDK leaves about 3,200 KiB for everything the service does before it has played anything. That alone rules it out, and its queue exceeds the whole 4,194,304-byte copy pool.

## Decision

Recommended, and built so far: **option 1 over option C.** `src/sobafm/lyria.py` adds no distribution. It makes the existing `audioop-lts` a direct pin for mixing.

### How audio maps onto capacity

A session holds one bank lease, so at most two sessions exist, and they can't exceed the existing two producer slots. A session pays for its copies from the slots belonging to its lease. A generated producer has no decoder child, so nothing else uses that lease's read/write slot. All totals and partitions stay as they are.

| Existing reservation | What it pays for | Worst case per session |
| --- | --- | --- |
| Network message slot, 1,048,576 | A frame's reads plus their join in aiohttp, then the one complete message. Messages are limited to under 524,288 bytes, so two copies fit. After the join, one copy plus the JSON around the audio, at most 16,384 bytes before decoding. | 1,048,574 |
| Read/write slot, 524,288 | asyncio's TLS read buffer (262,144), then up to 98,304 of staged ciphertext, its decrypted chunks and their join (196,608 together), a partial TLS record (16,709), and control messages | 479,557 |
| PCM conversion scratch, 524,288, shared | One decoded slice per session (49,152) and, once across sessions, a crossfade's five frames plus a fade-out's one (23,040) | 121,344 for both sessions together |
| Cancellation drain, 524,288, shared | Unused. A closing session keeps its own slots, so what it discards while closing is already paid for. | 0 |
| PCM banks, 2,304,000 each | Prepared audio only. Nothing else holds PCM beyond a slice in transit. | One bank |

Four choices make those numbers true, and each is pinned by a test:

- **Reading is paused between messages.** aiohttp's own limit is 512 KiB of queued messages, so two 2-second messages would queue, and a third could assemble while one was being processed. The session takes a message only when the bank has room for all of it, and pauses the transport right after.
- **Transport reads are small.** The socket's receive buffer is 32,768 bytes, which Linux doubles, so one read is at most 65,536. The TLS read high-water mark is 32,768, down from 256 KiB. This limits throughput to roughly one window per round trip, which is enough for the 256 KB/s of real time on routes under about 100 ms. A live check should confirm it.
- **Base64 is never a Python string.** One non-ASCII character in a message would otherwise make a 512,000-character string take four bytes a character. The audio is found and validated in place with a regular expression over the bytes, and only the small remainder is parsed as JSON. It is decoded in 65,536-byte slices straight into the bank.
- **The message limit is checked before payload.** aiohttp rejects an oversized frame from its declared length.

Measured once on TLS loopback, for development and not as L2: the real client's Python allocations peaked at 1.42 to 1.55 MB for one session and 2.12 to 2.15 MB for two overlapping sessions, against 1,572,864 and 3,145,728 for their slots. Those figures include object overhead and exclude OpenSSL's native buffer, which the table counts.

**The event loop.** Converting a message runs on the event loop, which the engineering notes generally advise against for preparing PCM. It takes 2.2 ms for a two-second message. A worker thread would resume reading while the message is still held, adding up to two more message copies, and both `json` and `binascii` hold the interpreter lock while they run anyway. The bound comes from the message limit. The voice callback runs on its own thread and is not delayed by the loop except through that lock.

### Sessions and renewal

- **Pre-roll.** The bank's existing six seconds. Frames can't be read before that. A session has one firm 15-second limit to reach it, however slowly audio arrives.
- **Completion policy.** Generated audio has no expected length. PCM is valid once every message producing it passed validation; a message is committed whole or not at all. A session that ends after readiness declares the end of PCM so that audio drains while a replacement prepares. One that ends before readiness stops its bank, and nothing is ever audible. Transfer completion is recorded only for a close frame with code 1000 or 1001. Decoder completion doesn't apply and stays false.
- **Stalls.** No audio for five seconds after readiness fails the session, unless reading stopped because the bank is full.
- **Renewal.** A stream renews 540 seconds after a session starts playing or as soon as it ends, whichever is first. The replacement pre-rolls on the second bank and a four-second crossfade follows. There is no resumption, so each session starts its music afresh. Renewing before the Live API's ten minutes turns a likely server close into a planned fade, and the same path handles a close at any moment.
- **Retries.** Three attempts per renewal, with waits of one and two seconds. A planned renewal that fails leaves the healthy session playing and gets one more set of attempts when that session ends, then the stream stops trying. Rejected requests and filtered prompts are never retried, because the same request would be refused again. Three sessions in a row that die before delivering 60 seconds of audio end the stream, since each of them would otherwise restart the attempts. When attempts run out, the buffered audio plays out and the stream ends with a reason.
- **Capacity.** A renewal needs the second bank. If another server holds it, the session keeps playing until it ends, its bank drains and is returned, and the replacement follows after a gap while it pre-rolls. This claims no two-server throughput, as ADR 0004 already says.

### Fades

`GeneratedStream.read_frame` mixes in the voice callback from the two banks, as the feasibility plan expects. Gains are linear, so a mix can't exceed its inputs, and `audioop` saturates rather than wrapping. This has a small dip in loudness halfway through a fade, which is a musical choice a listening check could revisit. The mix takes about 17 µs per frame, and the callback's lock is never waited on. A fade-out takes two seconds and only ends when frames have actually been read, so its owner needs a time limit if voice has gone.

### Cancellation and failure

- **Owners.** A session owns its socket, its session object and its two tasks. A stream owns every session and lease, and each retiring session has its own retirement task, which callers never cancel.
- **One clock.** The first of cancellation, a failure or the end of the stream fixes the deadline, three seconds later. A bank stopped by its owner earlier keeps that earlier time. Repeating cancellation changes neither.
- **Awaits.** Connecting, sending, receiving, the bank-full wait and closing all settle under cancellation. Cancellation during `receive` drops the connection at once. Otherwise the session closes politely, for at most a second, and two seconds after the first cancellation it stops being polite.
- **Failed verdicts and debt.** Settling after the deadline is an `expired` verdict that a later settlement never changes. The lease stays charged until the session's tasks have actually finished, and a bank that still can't release stays listed as debt. Nothing frees capacity to admit the next request.

## Consequences

- Google can change or withdraw an experimental model without much warning, and this path has no fallback. A generated request would then fail and say so, leaving current audio alone.
- The first live use has an unknown price. The first check should watch actual billing and stop on a bound.
- If real messages exceed 524,287 bytes, every session fails `oversized` at once. That would be visible on the first live message, and it would need a new mapping with a measured alternative rather than a larger limit here.
- Renewal uses two concurrent sessions for a few seconds every nine minutes. If the service allows only one, renewal fails, playback continues, and the eventual server close leaves a gap.
- A second server's generated program can't renew smoothly while both banks are held, and the gap at its renewal is about six seconds or more.
- The stream exposes `read_frame` and `ready_now` but isn't a `PcmBank`. Connecting it means `StationSource` and `Track` must accept that shape, with the stream's `finish` in place of the producer-and-bank retirement. That belongs to the station work and needs no change to `pcm.py`.

## What a fake can't establish

The tests run the real aiohttp client over real sockets, and one runs over TLS with a throwaway certificate. They establish validation, framing limits, pacing, every failure schedule, cancellation, deadlines and crossfade arithmetic. They can't establish any of these:

- That the live service accepts this handshake, version and header, or its real message size, frame type, MIME value, JSON layout and close codes.
- Session lifetime, whether two concurrent sessions are allowed, whether the service tolerates a client that stops reading, and how fast it generates, which decides pre-roll latency against the 15-second target.
- Price, rate limits and filtering behavior.
- Whether two sessions from one prompt crossfade musically, or whether the audio is any good.
- Real fit under the 90,177,536-byte group: TLS context, allocator and voice overhead, in the actual image and kernel.
- Continuity and timing over Discord.

## Verification needed

Each of these needs its own explicit authorization, and none is approved by issue #128.

1. **L2 from the [feasibility plan](../linux-feasibility.md#later-application-qualification).** Ten bounded loopback sessions with renewal overlap in the actual image, under the group limit. The transport is real and a safe local endpoint now exists, so this needs no provider, only an execution scope.
2. **A bounded opt-in provider check.** Its limits should be chosen with the maintainer: for example one program of a few minutes with one forced renewal and a hard stop on time and cost. It should record the facts above, including whether the bounded receive window holds real-time throughput on the deployment's route.
3. **Station integration.** L1, L4 and L5 at the real station, and L6-style continuity for generated programs, including the 60-minute underrun target.
4. **A listening check,** optional as elsewhere, for the crossfades.
