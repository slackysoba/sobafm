# 0008: The Lyria RealTime path

- Status: **Proposed, not accepted.** The maintainer's answer to the [questions on issue #128](https://github.com/slackysoba/sobafm/issues/128) is still needed, and the code has [open review findings](#open-review-findings).
- Date: 2026-11-05
- Scope: [issue #128](https://github.com/slackysoba/sobafm/issues/128)
- Builds on: the capacity contract in [ADR 0004](0004-amd64-whole-service-adoption.md) and its [copy partitions](../linux-feasibility.md#capacity-ownership-and-admission), which this record leaves unchanged
- Sources read on 2026-11-05 and rechecked on 2026-11-06, when `ai.google.dev` pages redirect to `aistudio.google.com`: the [realtime guide](https://aistudio.google.com/docs/realtime-music-generation), [API reference](https://aistudio.google.com/api-reference/live-music), [model page](https://aistudio.google.com/docs/models/lyria-realtime-exp), [pricing](https://aistudio.google.com/docs/pricing), [billing](https://aistudio.google.com/docs/billing), [rate limits](https://aistudio.google.com/docs/rate-limits), [deprecations](https://aistudio.google.com/docs/deprecations), [changelog](https://aistudio.google.com/docs/changelog), [Gemini API terms](https://aistudio.google.com/docs/terms), [Google APIs Terms](https://developers.google.com/terms), the [Lyria 3.5 guide](https://aistudio.google.com/docs/music-generation) and [prompt guide](https://aistudio.google.com/docs/lyria-prompt-guide), the [Live API session guide](https://aistudio.google.com/docs/live-session), the [cookbook notebook](https://github.com/google-gemini/cookbook/blob/main/quickstarts/Get_started_LyriaRealTime.ipynb) and the [SDK source](https://github.com/googleapis/python-genai/blob/main/google/genai/live_music.py)

No call was made to Lyria, Gemini or Discord for this record, and `/freestyle` is not wired to anything. Everything here about the service comes from documentation. [What's established](#what-a-fake-cant-establish) lists what that leaves open.

## Problem

Outcome 3 needs generated audio to reach the player: continuously, for up to 240 minutes, with fades, and inside the 98,979,840-byte capacity contract. Lyria RealTime sends base64 audio in JSON over a WebSocket, so one message of audio exists several times in memory before it reaches a bank. The contract's copy pool has explicit owners, and any path that can't bound a message before allocating it [fails qualification](../linux-feasibility.md#capacity-ownership-and-admission).

## What the documentation establishes

- **Model and API.** `models/lyria-realtime-exp` over a WebSocket at `/ws/google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateMusic`. The guide, model page and changelog call the model experimental, the reference calls the API preview, and the deprecations page files the model under preview models with no shutdown date announced. The model page's latest update is May 2025. Google says experimental models aren't usually suited to production and their availability can change. The cookbook still uses `v1alpha`; the reference and guide use `v1beta`, which this pins.
- **Audio.** Raw 16-bit PCM at 48 kHz stereo, which is SobaFM's format, so no resampling is needed. One JavaScript sample shows 44,100 Hz, against the specification. The cookbook's recorded WAVs are 48 kHz.
- **Message size.** Not documented. The cookbook's recorded output settles it for planning: ten messages produced exactly 3,840,000 bytes, so one message is two seconds, 384,000 PCM bytes and 512,000 bytes of base64.
- **Session limit.** Not documented for music, and the music API has no resumption or GoAway message. The Live API documents about ten minutes per connection.
- **Protocol.** `setup` first, then wait for `setupComplete`; then `clientContent.weightedPrompts`, `musicGenerationConfig` and `playbackControl`. The server sends `serverContent.audioChunks[]`, `filteredPrompt` and `warning`. The SDK sends the key as an `x-goog-api-key` header. Output is generated as fast as possible rather than in real time, and clients are told to buffer.
- **Cost and paid status.** The pricing page has no entry for this model, so neither its fee nor whether it is billed at all is published. For comparison, the non-streaming Lyria 3.5 costs $0.08 per song with no free tier. The terms treat a service as paid when it's offered for a fee, take fees from the pricing page, and treat API access as paid only through a project with active billing. So the paid tier's data terms and its access to EEA, Swiss and UK users aren't established for this model until it's seen billed or Google confirms. That the free tier serves it isn't documented either. Rate limits for experimental models are tighter, and the specific ones are only visible in AI Studio. Concurrent session limits aren't documented, and renewal needs two at once.
- **Limiting spend.** Billing data lags: usage takes about ten minutes to process and cost graphs up to 24 hours, so watching billing can't stop a short check. A Prepay balance with auto-reload off can: the minimum is $5, and at zero requests fail with HTTP 402. Usage in that ten-minute lag can overshoot, and the billing page warns about long-running tasks without saying how it treats an open stream. Zero stops every key on that billing account, including `/play`'s if it shares one. Accounts on Postpay don't have this balance. The other stops are an optional project spend cap, itself marked experimental and with the same overshoot, and the tier's monthly cap, $250 at Tier 1. A spend-rate limit of $10 per ten minutes may also apply at Tier 1, returning 429. Prices can change on 30 days' notice.
- **Terms.** These are the Gemini API Additional Terms, effective March 23, 2026, and this summary isn't legal advice. They apply to `/play`'s Gemini matching today as well as to generated music. [ADR 0006](0006-gemini-play-matching.md) records free-tier data use but none of the other clauses here.
  - **Purpose.** The API is for developers building for "professional or business purposes, not for consumer use". The page doesn't define those words or mention hobby or non-commercial projects. Terms it doesn't define take their meaning from the Google APIs Terms (last modified November 9, 2021), which don't help: they never use "professional" or "consumer", and "business" appears only in a HIPAA clause and an indemnity. These pages don't say what a "professional" self-hosted bot is, and SobaFM's catalog permission bars commercialization and monetization. This record doesn't decide it. It is the maintainer's judgment.
  - **Age.** Whoever uses the API must be 18 or over. Listeners fall under a separate clause: the API may not be part of a service directed at or likely to be accessed by under-18s, and a Discord voice channel doesn't check ages.
  - **Regions and data.** Making an API client available to users in the EEA, Switzerland or the UK requires paid services. Unpaid use lets Google use prompts and output to improve its products, with human review. Paid use doesn't, but logs them for a limited time for abuse detection. Developers located in those three regions get the paid data terms even on unpaid quota.
  - **Other.** The Google APIs Terms also bar offering third parties an API client that functions substantially like the APIs. This record doesn't judge that either.
- **Data handling.** Only prompts and controls are sent. There is no `store` field, so the requirement to disable provider storage has nothing to set here. Output is always watermarked. The model's music is instrumental, and its `VOCALIZATION` mode adds voices as another instrument, which should fit the wordless vocals the requirements allow. A live check should confirm it produces no lyrics.

## Options

**Generation path, which is the maintainer's choice.** All of these depend first on the purpose clause above. No option answers it, and confirming that listeners are adults answers only the age clause.

1. **Lyria RealTime on a billed project.** One continuous stream steered by prompts and documented as instrumental. No decoder involved, and the audio is already in the player's format. It is experimental, its price and limits are unknown, and its paid status isn't established. A small Prepay balance with auto-reload off can limit a first check, as above. That limits only the check. Afterwards the price stays unpublished and can change, `lyria.py` has no spend limit, and the only stops are that balance, an optional project cap and the tier's monthly cap. The [open findings](#open-review-findings) need fixing first.
2. **Lyria RealTime on the free tier.** The same stream with no spend, but free-tier access isn't documented. Google may use prompts and output, with human review, and it can't be offered to users in the EEA, Switzerland or the UK. The open findings apply here too.
3. **Lyria 3.5, one song at a time.** Generally available since 2026-09-03, at $0.08 per song with no free tier. Price is per song and the prompt sets the length, so at an assumed two to three minutes a song, a 60-minute program costs about $1.60 to $2.40, and 240 minutes four times that. It makes vocals and generated lyrics by default, which the requirements bar. Instrumental output is only a prompt instruction with no parameter, so choosing it means trusting that instruction or changing the requirement. Its MP3s would go through the existing decoder, and each song takes time to generate, which changes the continuity design. Little here would apply.
4. **Wait** for a stable real-time model. Generated mode, and with it Outcome 3, waits too.

**Transport, within options 1 and 2:**

| Option | Runtime closure | Service RSS at import | Can bound a message before allocating it |
| --- | --- | --- | --- |
| A. `google-genai` 3.0.0 (Apache-2.0) | 16 to 38 distributions, adding native `cryptography` and `pydantic-core` | About 84,800 KiB (+30,600) | No: it passes no limits to `websockets`, whose defaults allow 1 MiB per message and a queue of 16 |
| B. `websockets` 17.4 directly (BSD-3-Clause) | 17 | About 55,600 KiB (+1,400) | Yes, with the same TLS staging as C, and it would follow redirects with the key |
| C. `aiohttp` 3.14.5, already locked | 16, no change | About 54,200 KiB (no change) | Yes |

Import memory was measured on Python 3.14 under WSL2 x86_64: `disnake` first, then the candidate, with compiled bytecode as the image has, taking resident memory from `/proc/self/statm` and three runs. The service's group limit is 88,064 KiB, so the SDK leaves about 3,200 KiB for everything the service does before it has played anything. That alone rules it out, and its queue exceeds the whole 4,194,304-byte copy pool.

## Decision

Recommended, if the maintainer judges the use to be within the purpose clause: **option 1 over transport C, starting with a small Prepay check.** Options 1 and 2 are the only ones documented as instrumental that also stream continuously. Option 1 is preferred over 2 because Google doesn't document that the free tier serves this model, and the free tier lets Google use prompts and output and can't reach EEA, Swiss or UK users. Its risks are an unknown price, which the check can show, and an experimental model with no fallback here, which no check removes. This is what is built so far, with [open findings](#open-review-findings).

The order is the maintainer's answers, then the four code fixes, then any Prepay check. Before the fixes, a renewal refused with 429 would end the check early, and a closing session would keep reading a stream that may be billed.

`src/sobafm/lyria.py` adds no distribution. It makes the existing `audioop-lts` a direct pin for mixing.

### How audio maps onto capacity

A session holds one bank lease, so at most two sessions exist, and they can't exceed the existing two producer slots. A session pays for its copies from the slots belonging to its lease. A generated producer has no decoder child, so nothing else uses that lease's read/write slot. All totals and partitions stay as they are.

| Existing reservation | What it pays for | Worst case per session |
| --- | --- | --- |
| Network message slot, 1,048,576 | A frame's reads plus their join in aiohttp, then the one complete message. Messages are limited to under 524,288 bytes, so two copies fit for a message in one frame. A fragmented message takes a third, which is an open finding. After the join, one copy plus the JSON around the audio, at most 16,384 bytes before decoding. | 1,048,574 |
| Read/write slot, 524,288 | asyncio's TLS read buffer (262,144), then up to 98,304 of staged ciphertext, its decrypted chunks and their join (196,608 together), a partial TLS record (16,709), and control messages | 479,557 |
| PCM conversion scratch, 524,288, shared | One decoded slice per session (49,152) and, once across sessions, a crossfade's five frames plus a fade-out's one (23,040) | 121,344 for both sessions together |
| Cancellation drain, 524,288, shared | **Not met yet.** A closing session still reads until a close frame, so it holds three message copies and exceeds its network slot, and this reservation can't pay for two sessions closing together. The planned fix closes without reading, which frees this row to pay for a fragmented message's third copy. | See the open findings |
| PCM banks, 2,304,000 each | Prepared audio only. Nothing else holds PCM beyond a slice in transit. | One bank |

Four choices bound the receive path for a message in one frame, and each is pinned by a test:

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
- **Retries.** Three attempts per renewal, with waits of one and two seconds. A planned renewal that fails leaves the healthy session playing and gets one more set of attempts when that session ends, then the stream stops trying. Rejected requests and filtered prompts are never retried, because the same request would be refused again. That reason doesn't hold for a rate limit, and today's code also counts 408 and 429 as rejected, which is an open finding. Three sessions in a row that die before delivering 60 seconds of audio end the stream, since each of them would otherwise restart the attempts. When attempts run out, the buffered audio plays out and the stream ends with a reason.
- **Capacity.** A renewal needs the second bank. If another server holds it, the session keeps playing until it ends, its bank drains and is returned, and the replacement follows after a gap while it pre-rolls. This claims no two-server throughput, as ADR 0004 already says.

### Fades

`GeneratedStream.read_frame` mixes in the voice callback from the two banks, as the feasibility plan expects. Gains are linear, so a mix can't exceed its inputs, and `audioop` saturates rather than wrapping. This has a small dip in loudness halfway through a fade, which is a musical choice a listening check could revisit. The mix takes about 17 µs per frame, and the callback's lock is never waited on. A fade-out takes two seconds and only ends when frames have actually been read, so its owner needs a time limit if voice has gone.

### Cancellation and failure

- **Owners.** A session owns its socket, its session object and its two tasks. A stream owns every session and lease, and each retiring session has its own retirement task, which callers never cancel.
- **One clock.** The first of cancellation, a failure or the end of the stream fixes the deadline, three seconds later. A bank stopped by its owner earlier keeps that earlier time. Repeating cancellation changes neither.
- **Awaits.** Connecting, sending, receiving, the bank-full wait and closing all settle under cancellation. Cancellation during `receive` drops the connection at once. Otherwise the session closes politely, and `finish` drops the connection two seconds after the first stop. The polite close has no firm limit: aiohttp's one-second timeout applies per read, so a server that keeps sending extends it, and nothing forces a session that stopped itself until its owner calls `finish`. That is an open finding.
- **Failed verdicts and debt.** Settling after the deadline is an `expired` verdict that a later settlement never changes. The lease stays charged until the session's tasks have actually finished, and a bank that still can't release stays listed as debt. Nothing frees capacity to admit the next request.

## Consequences

- Google can change or withdraw an experimental model without much warning, and this path has no fallback. A generated request would then fail and say so, leaving current audio alone.
- The first live use has an unknown price. A small Prepay balance with auto-reload off limits that check, with the overshoot above; watching billing can't. It limits only the check. Afterwards the price stays unpublished and can change, `lyria.py` has no spend limit, and the only stops are that balance, an optional project cap and the tier's monthly cap ($250 at Tier 1).
- If real messages exceed 524,287 bytes, every session fails `oversized` at once. That would be visible on the first live message, and it would need a new mapping with a measured alternative rather than a larger limit here.
- Renewal uses two concurrent sessions for a few seconds every nine minutes. If the service refuses the second, the result depends on how. Playback continues either way, but a refusal counted as `rejected` also ends the program when the first session ends, with no later attempt. Today that means any handshake status from 300 to 499, including 408 and 429, or a close with code 1008. So a rate limit on renewal cuts a program short, against the requirement that generated programs last their configured duration. Only a refusal counted as `connect` gets later attempts and a gap. Moving 408 and 429 to `connect` is an open finding; another 4xx or a close with 1008 would still end the program.
- A second server's generated program can't renew smoothly while both banks are held, and the gap at its renewal is about six seconds or more.
- The stream exposes `read_frame` and `ready_now` but isn't a `PcmBank`. Connecting it means `StationSource` and `Track` must accept that shape, with the stream's `finish` in place of the producer-and-bank retirement. That belongs to the station work and needs no change to `pcm.py`. `freestyle_plan.GeneratedPlan` already maps field for field onto `MusicRequest`, which stays free of plan types just as the plan stays free of transport types.

## Open review findings

The [review at `d08e8b6`](https://github.com/slackysoba/sobafm/pull/136#pullrequestreview-5797091896) found these in the code. They aren't fixed. They wait for the maintainer's answers, which decide whether this module stays, and they come before any Prepay check. Until the first two are fixed, the module doesn't stay inside its partitions on those paths, though the contract itself is unchanged.

1. **A closing session exceeds its network slot.** Reading until a close frame holds three copies: review measured 1,536,141 bytes for two-second messages and 1,572,861 at the limit, against 1,048,576. Two sessions closing together, as in a stop during a crossfade, would need 975,130 from the drain reservation's 524,288. The correction is to close without reading, by dropping the connection.
2. **A fragmented message takes three copies while being received.** It peaked at 1,543,411 bytes for a two-second message and 1,592,371 at the limit. The correction is to charge the third copy to the drain reservation, which only one session can use at a time because the copy exists inside one synchronous reader call.
3. **408 and 429 end a program.** Both count as `rejected`, as the Consequences describe. The correction is to count them as `connect`.
4. **A false failed verdict.** A session cancelled in the same tick it started never records settling, so it reports `expired` with nothing held. The correction is to record settling in a done callback on its task.

## What a fake can't establish

The tests run the real aiohttp client over real sockets, and one runs over TLS with a throwaway certificate. They establish validation, framing limits, pacing, every failure schedule, cancellation, deadlines and crossfade arithmetic. They can't establish any of these:

- That the live service accepts this handshake, version and header, or its real message size, frame type, MIME value, JSON layout and close codes.
- Session lifetime, whether two concurrent sessions are allowed, whether the service tolerates a client that stops reading, and how fast it generates, which decides pre-roll latency against the 15-second target.
- Price, whether the model is billed at all, whether the free tier serves it, rate limits and filtering behavior.
- Whether two sessions from one prompt crossfade musically, or whether the audio is any good.
- Real fit under the 90,177,536-byte group: TLS context, allocator and voice overhead, in the actual image and kernel.
- Continuity and timing over Discord.

## Verification needed

Each of these needs its own explicit authorization, and none is approved by issue #128.

1. **L2 from the [feasibility plan](../linux-feasibility.md#later-application-qualification).** Ten bounded loopback sessions with renewal overlap in the actual image, under the group limit. The transport is real and a safe local endpoint now exists, so this needs no provider, only an execution scope.
2. **A bounded opt-in provider check,** after the maintainer's answers and the four code fixes. Its limits should be chosen with the maintainer: for example one program of a few minutes with one forced renewal and a hard stop on time, funded by a small Prepay balance with auto-reload off on a billing account `/play` doesn't share. It should record the facts above, including whether the bounded receive window holds real-time throughput on the deployment's route.
3. **Station integration.** L1, L4 and L5 at the real station, and L6-style continuity for generated programs, including the 60-minute underrun target.
4. **A listening check,** optional as elsewhere, for the crossfades.
