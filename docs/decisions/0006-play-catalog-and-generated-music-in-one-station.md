# ADR-0006: Play catalog and generated music in one station

- **Status:** Accepted on merge of PR #156; proposed until then
- **Date:** 2026-10-15
- **Issue:** [#117](https://github.com/slackysoba/sobafm/issues/117)
- **Parent:** [M5, #114](https://github.com/slackysoba/sobafm/issues/114)
- **Evidence:** [#116](https://github.com/slackysoba/sobafm/issues/116), completed under
  qualitative maintainer acceptance; numerical relevance unmeasured

## Context

M5 proposes `/play` for matching Jamendo tracks and `/freestyle` for today's
Lyria RealTime music in one bot. The maintainer chose the one-bot direction over
[#40](https://github.com/slackysoba/sobafm/issues/40); #117 still needs an accepted
service, product and engine decision. This proposal incorporates the completed
research disposition, approved quiet-credit presentation and the
[product disposition](https://github.com/slackysoba/sobafm/issues/117#issuecomment-6197413269)
made under the maintainer's [delegated authority](https://github.com/slackysoba/sobafm/issues/114#issuecomment-6197383448).
The [broad catalog playback disposition](https://github.com/slackysoba/sobafm/issues/117#issuecomment-6197676011)
selects the exact license families and successive catalog transitions below.
These choices are resolved for incorporation and independent review. Under the
[resume and qualification-sequencing disposition](https://github.com/slackysoba/sobafm/issues/114#issuecomment-6198075027),
merging this reviewed record accepts the architecture and guardrails, authorizing
requirements reconciliation and implementation. Actual track rights, notice
coverage, resource enforcement and supported-platform qualification remain
mandatory before catalog runtime readiness or shipping. They are not evidence
that must be manufactured before implementation can begin.

The [requirements](../requirements.md) and [architecture](../architecture.md)
still describe approved v1 behavior. This proposal neither changes them nor
supersedes accepted ADRs. Adoption would require #119 to reconcile CMD-1 to CMD-3,
PLAY-1 to PLAY-3 and PLAY-8, AI-1 to AI-7, FB-1 to FB-4, OPS-1, NFR-1 to NFR-3 and
USE-1 to USE-3. Indefinite catalog programs and requested lyrics are selected
changes to current requirements. Successive catalog playback explicitly replaces
the original catalog-involving crossfade criteria, as traced below; #119 must
record these changes before implementation. Accepted v1 records remain intact.

### Current source and accepted decisions

The original source assessment uses main commit
[`2dec12be698845a89bf88070eae07f2fdae3d69e`](https://github.com/slackysoba/sobafm/tree/2dec12be698845a89bf88070eae07f2fdae3d69e).
Relative source links below identify the modules to compare during review.

| Existing surface | Reuse and boundary |
| --- | --- |
| [ADR-0001](0001-build-on-python-discord-py-and-the-google-gen-ai-sdk.md), [mixer.py](../../src/sobafm/mixer.py), [pcm.py](../../src/sobafm/pcm.py) | Keep discord.py's player, Opus/DAVE, PCM framing, gain and fades. `FrameSource` already requires only a `deque[bytes]`; the mixer cannot fetch or decode audio. |
| [ADR-0002](0002-interpret-requests-with-gemini-structured-output.md), [interpreter.py](../../src/sobafm/interpreter.py), [plan.py](../../src/sobafm/plan.py) | Preserve validated structured interpretation, refusal controls and descriptive artist/work translation. `MusicPlan` and its raw-text fallback remain Lyria-specific. The probe's models are experimental evidence, not adopted runtime contracts. |
| [ADR-0003](0003-keep-server-state-in-sqlite-and-use-discord-command-permissions.md), [store.py](../../src/sobafm/store.py) | Reuse per-server settings and Discord command permissions. Programs, reservations, buffers and recent-track history stay in memory. |
| [ADR-0004](0004-stream-lyria-realtime-through-buffered-decks.md), [station.py](../../src/sobafm/station.py), [deck.py](../../src/sobafm/deck.py) | Preserve Lyria session rotation and failure semantics. `_retire`, `_generate`, `_discard`, `_count_failure`, `_may_start`, `_ready_deck` and `Program` retry fields contain the mode-specific rules. |
| [bot.py](../../src/sobafm/bot.py), [commands.py](../../src/sobafm/commands.py), [status.py](../../src/sobafm/status.py), [config.py](../../src/sobafm/config.py) | Reuse voice recovery, command admission/cooldown, request supersession, slow-start replies and ordered status updates. Playback/feedback/configuration wiring still needs adaptation; these entire modules are not source-independent. |

### Research evidence and acceptance limits

The standalone [probe and operator guide](../catalog-probe.md) merged in
[PR #144](https://github.com/slackysoba/sobafm/pull/144). M4's ready-to-ship
prerequisite was satisfied by the stable v1.0.0 publication and
[recorded maintainer dispositions](https://github.com/slackysoba/sobafm/issues/4#issuecomment-6097955604),
with their original incomplete checks and exceptions retained. The approved
research window started at 2026-10-10T23:40:29.544021Z and its original deadline,
**2026-10-12T23:40:29.544021Z**, was missed; it was not reset or extended.

The [frozen D1 protocol](https://github.com/slackysoba/sobafm/issues/116#issuecomment-6104782646)
used 48 cases, including the ten maintainer-selected requests, and explicit
`gemini-3.5-flash-lite`. Its manifest digest was
`70ddf145823fb96e651e785dbeb2f480b175fa03d79ceec75870cee19294bdeb`.
The [completed D1 result and independent review](https://github.com/slackysoba/sobafm/issues/116#issuecomment-6104976415)
at source `a1e463903fa289f215ad2fd8d0d106471f39db3e` recorded all 48
classifications passing, with all six controls making no Jamendo searches.
There were 189 track appearances: 38 of 42 music cases returned at least three
candidates, while four empty cases remain in the 42-case denominator. The earlier
fatal prefixes remain separate; D1 does not establish their original causes.

On 13 October the maintainer
[accepted the research direction qualitatively and waived the remaining structured listening/export and calculated go/no-go](https://github.com/slackysoba/sobafm/issues/116#issuecomment-6145275917).
Issue #116 is closed under that narrow disposition. No report-bound ratings export
was received: the rating pairs remain null, and the frozen summary remains
`complete=false` / `recommendation=pending`. The approved numerical mark, **28 of
all 42 music cases with three fitting tracks in the returned top five**, remains
unmeasured rather than passed or failed. The maintainer's general feedback is not
a per-track or per-case score, and no agent hearing is claimed.

The maintainer also reported that the first ten songs were the weakest fits and
did not fit, whereas the rest generally did. If this refers to the first ten
displayed tracks in the retained D1 worksheet, those entries belong to eval-01 and
eval-02; this ordering observation assigns no rating pairs. Carry the weak-fit
note into #126's interpretation/search acceptance work. It does not establish fit
for a future license-filtered catalog.

D1 recorded 95 successful discovery calls and 378 stream HEAD observations:
`mp31` had 189 known successes; `mp32` had 126 known successes and 63 opaque
errors. Missing statuses/lengths remain unavailable. HEAD observations do not
prove audio GET, decoded format, playback, or production resource bounds. No
provider calls or audio retrieval/decoding are part of this documentation refresh.

#### License subset caveat

The recorded appearance labels were 128 BY-NC-ND 3.0, 13 BY-ND 3.0, seven
BY 3.0, 16 BY-SA 3.0, 21 BY-NC-SA 3.0 and four BY-NC 3.0. These describe
this sample, not the whole catalog or permission to use it. A read-only post-filter
of the unchanged D1 report (file SHA-256
`0f29ddc0d1dcb143011917b661364cf400b8c54e66835590c5bde1d0b39a41c4`)
gives the following candidate counts, retaining all 42 music cases:

| Subset of already returned top-five candidates | Appearances | Cases with at least one candidate | Cases with at least three candidates |
| --- | ---: | ---: | ---: |
| All returned licenses | 189 | 38/42 | 38/42 |
| Exclude ND; retain SA for comparison only | 48 | 20/42 | 8/42 |
| BY 3.0 and BY-NC 3.0 only | 11 | 9/42 | 0/42 |

This post-filter did not refill the top five or run a license-filtered search.
The counts are metadata availability, not audible-fit scores or a failure of the
waived research gate. They neither predict further results from a filtered query
nor establish relevance for a narrower allowlist. A 4.0-only allowlist would admit
none of this sample. The selected policy admits all six reviewed 3.0 Unported
families, subject to actual rights/notices and the successive-playback rules below.
These unchanged counts do not qualify individual tracks or prove runtime coverage.
The research waiver grants no legal, service, decoder, resource, platform or
other M5 exit waiver and does not accept this record or #118.

## Requirements

An acceptable decision must:

1. Keep one station, one player and at most one desired program per server, with
   no member queue or playlist. Mode changes preserve audio already heard until
   a replacement can play, and FB-3 works in both directions.
2. Preserve ADR-0004's Lyria constants and the subsequently corrected lifecycle:
   250 ms ticks; two sessions per station; 540 s retirement; 6 s pre-roll;
   60/45 s pause/resume; 4 s handover/crossfade; 2/3 s fade-in/out;
   15 s connection timeout; 2, 5, then 15 s failure backoff; one refusal retry
   after 30 s; and 60 s empty-channel grace. Quota/key failures and give-up
   precedence remain as in the current architecture, including #89 and #93.
   These fades and overlapping handovers remain generated-to-generated behavior;
   no fade or overlap is applied to catalog audio.
3. Keep the process-wide Lyria cap, default four sessions, configured by
   `SOBAFM_MAX_SESSIONS`. Catalog programs consume no Lyria reservation.
4. Never block the event loop with decoding or the player with I/O, decoding or
   waiting. Every mixer read returns a 3,840-byte, 20 ms PCM frame or silence.
5. Keep all retained audio below NFR-3's 40 MB per playing server, including mode
   transitions, compressed input, decoder queues/copies and ended sources.
6. Meet the selected catalog license, attribution, service and privacy obligations
   before playing a track. Keep credentials and credential-bearing URLs out of
   logs/replies; validate external metadata/model output before it changes state.
7. Retain M4 and #116's recorded acceptance limits and #118's independent decoder
   decision.
   No account, commercial service, dependency or distribution exception is
   approved by preparing this record.

## Options considered

| Option | Fit and reuse | Costs and risks |
| --- | --- | --- |
| **1. One station with one engine per mode** | Reuses the existing mixer and voice recovery. Sources share PCM; engines isolate Lyria lifetime/retries from catalog selection/EOF. One station retains FB-3 and transitions in both directions. | Requires careful program identity, reservation ownership and a shared audio budget. Jamendo adds per-operator setup, terms, attribution and availability risk. No new audio server or versioned shared package is needed. |
| **2. A station subclass per mode** | Initially less extraction; reuses voice and mixer code within each station. | Replacing the station loses the audible program and its recovery/reservation state. To preserve cross-mode FB-3 and safe successive boundaries, a further coordinator would be needed, duplicating option 1's core ownership. |
| **3. Separate bots with an extracted shared package** | Strong deployment isolation; generated music can release separately. | Two Discord applications and operating paths; versioned voice fixes and package maintenance. No unified source handover or cross-mode FB-3; contradicts the one-bot direction recorded on issue #117. |

A new managed playback service/library is not a substitute for this seam: the
[existing-solution review](../architecture.md#existing-solution-review) found no
library that mixes Lyria's live PCM into discord.py's source. Reuse the existing
mixer rather than add another service. Retaining v1 remains the alternative if
remaining catalog-adoption conditions cannot be met; the qualitative research
acceptance does not adopt a second catalog or remove the other M5 gates.

## Decision

**Decision:** option 1, with Jamendo API v3 as the
first catalog, under the linked delegated product disposition and subject to the
implementation and qualification conditions below. Keep the surface
limited to two engines and their buffered sources. Do not build a provider registry,
plugin framework, generic retry framework or runtime probe integration.

### Ownership and contracts

- **Station:** generic end rules, desired/heard/previous program identity,
  start-result settlement, player start/recovery, transitions, wind-down,
  listener grace, volume, underrun accounting and feedback. It owns the total
  audio budget across both engines. `reconcile()` and `close()` remain the only
  coordinators that create, switch or close sources; producer tasks fill buffers.
- **Program:** mode and a validated mode-specific plan/query, requester, optional
  monotonic deadline, start future and underrun count. The future settles once.
  Plan/program identity must distinguish a new request even with identical text.
  Lyria retry/refusal/deferred-cause state belongs to the Lyria engine, keyed weakly
  by program, without a value that retains its key. Catalog history belongs to
  its station's catalog engine across programs, bounded and cleared on restart.
- **LyriaEngine:** today's deck rules and reservation management; it shares the
  one process-wide `SessionPool`. Two engines in different servers must not create
  separate global pools. Source count still includes connecting/closing Lyria
  sessions until they have actually ended, not just until retirement is requested.
- **CatalogEngine:** validated candidate selection, bounded recent-track history,
  fetch/decode scheduling, ordered ready-track choice, normal EOF and track
  failure handling. The catalog client seam covers search and validated track
  metadata/stream access; no extra provider is implemented in this slice.
- **Source:** buffered PCM `frames`, diagnostic `number`, program/plan identity,
  producer `state`, `ready`, `buffered_seconds`, display `title`, and lifecycle
  `start()`, `retire()`, `wait_ended()`, `regulate()`. Retirement stops future
  production and preserves playable frames; producer-ended does not mean
  playback-drained. Catalog EOF is normal, not Lyria's early-session failure.
  Catalog attribution accompanies its source; Lyria titles derive from the plan.
  `FrameSource` stays the smaller mixer contract.

The proposed engine operations are `reserve(program)`, `count_ended(program, player)`, `drop_ended(program, mixer)`, `retire(program, mixer)`,
`generate(program)`, `ready_deck(program, exclude)`, `handover_due(source)`,
`going_live(source)` and
`close()`. Engines report start failure to the station, which owns any give-up and
rollback. Exact typing/state names are reviewed in #121; these are responsibilities,
not code scaffolding or a promise that a shallow protocol extraction suffices.
The added `handover_due` responsibility refines #117's initial seam: a low catalog
buffer can mean a stalled fetch/decoder, not the end of a track. Reusing the Lyria
trigger unconditionally could silently cut tracks short. Retain the source-specific
responsibility in the selected design and verify it through independent review
and the later EOF/starvation regressions.
Catalog `reserve` needs no Lyria slots; new catalog work still needs the proposed
combined byte-budget admission. Lyria generation/retention keeps its existing rules.
`going_live` updates track history/feedback only after an audible switch
is accepted, not when a candidate is fetched.

### Reconcile order and Lyria regressions

Preserve this order across all retained sources, including the old mode:

1. **End:** connection loss, generated-program deadline and listener grace take
   precedence. Catalog has no deadline in the selected product policy.
2. **Count, give up, then discard:** engines count newly ended producers once;
   the station applies any failure/FB-3 return; only then recompute the desired
   program and drop ended sources that the mixer no longer uses. Failure counting
   uses current program identity again after any rollback. Lyria resets backoff
   for a normally retired producer with delivered pre-roll before sorting failures:
   key/quota, refusal, recognized causes, other failures. Preserve the existing
   tie behavior rather than introducing cross-engine failure ordering.
3. **Retire:** Lyria rotates at 540 s and retires unused replaced sources.
   Both engines stop unused producers for obsolete programs. Never clear a
   mixer's live/incoming frames; retire an outgoing producer at its handover.
4. **Generate:** preserve Lyria's generation of up to two non-ended producers,
   reservation and backoff rules, including replacement producers beside retained
   ended audio. An outgoing producer still occupies a session slot until it ends.
   New catalog work needs the proposed combined byte-budget admission below;
   retained-buffer count is not an additional Lyria generation limit.
5. **Start:** choose a ready source only with a running player and connected voice.
   Generated starts retain their fade-in; catalog starts apply no automated fade.
   Report playing only then; disconnected readiness is not success.
6. **Hand over or stop:** no new switch while one is in progress. Lyria selects
   the most buffered ready source of its plan; catalog selects the earliest
   eligible ready track. Generated-to-generated handover retains the below-4 s
   trigger and existing clamped 4 s equal-power crossfade. A normal catalog track
   finishes only after normal decoded EOF **and** consumption of its remaining
   playable frames, then the next ready source starts without overlap or a catalog
   fade. EOF alone and a transient low buffer are not permission to cut the track.
   A low buffer waits/retries under the bounded failure policy; it does not become
   a successful track ending. Prepare a requested skip/replacement before switching;
   an explicit skip, stop or replacement ceases the outgoing performance. Every
   boundary involving catalog ends outgoing audio before incoming audio begins.
   Generated audio may use its own fade only while no catalog audio is audible.
   A normal boundary adds no intentional silence; later endurance tests still have
   to meet the unchanged underrun bound. With no program, retire idle sources,
   cease catalog audio without a fade, or finish generated wind-down, then stop and
   release resources. With no player, stop immediately rather than await unread fades.
7. **Regulate:** apply source-specific bounded production/backpressure.
8. **Feedback:** publish the accepted audible target, retaining old feedback while
   a replacement starts; status failure/backoff never stalls playback.

[#89](https://github.com/slackysoba/sobafm/issues/89) requires immediate release of
dropped decks without cycle collection. Keep the weak ended-source set, no strong
engine map keyed by deck, traceback detachment in `Deck._end()` and cancellation
exception consumption in `Deck._cancelled()`. Apply the same lifetime guarantee to
catalog tasks/errors; do not retain encoded bytes through error/report objects.
Lyria's discard retains at most the most buffered ended ready source for the current
plan while no open unused source is ready. Do not apply that policy to catalog EOF:
a normal completed next track must not be discarded merely because another is ready.

[#93](https://github.com/slackysoba/sobafm/issues/93) requires all three Lyria
start give-ups (key/quota, final retry, second refusal) to wait while another source
of the same plan is open or ended-ready with connected voice. Preserve once-per-round
counting and deferred decisive cause: a last-round outage followed by a quota close
reports exhausted. A started Lyria program continues backed-off retries. A catalog
track ending normally must never enter these refusal/retry counters.

### Cross-mode reservation, rollback and cancellation

`SessionPool` remains process-wide and reserves two sessions per station's Lyria
engine, not per program. Admission must succeed before changing the desired
program, without an await between the final supersession check and installation.

| Transition | Reservation and FB-3 behavior |
| --- | --- |
| Catalog → generated | Acquire two Lyria slots first. BUSY leaves catalog music/program intact and frees only the failed request's cooldown. Once admitted, catalog stays audible while Lyria fills; a refusal/key/quota/outage restores that catalog program if still viable. |
| Generated → catalog | Keep the Lyria reservation while generated music remains desired or the FB-3 previous program. Its still-used source keeps playing; unused old producers retire. A failed catalog start restores the original Lyria program with its original deadline and its reservation. |
| Successful switch away from generated | Release only once neither desired nor retained previous program needs Lyria and every Lyria producer, including a closing/outgoing one, has ended. Buffered retired Lyria audio alone needs no open-session slot. |
| Generated → generated, or return before release | Reuse the same reservation. Retained/closing producers still count toward two; do not double-reserve or open extra sessions. |
| Stop, leave, permanent voice loss or shutdown | Settle pending starts with the existing terminal outcome, clear previous state, retire/cancel all work, await producer cleanup and release once. No old completion can restart a program. |

Restore the program still heard, not the last request that failed. Rapid replacements
must retain that audible program until a target really plays, and invalidate work by
program/request identity. An expired generated previous program is not restarted;
a catalog previous program is invalid after stop/leave/voice loss or explicit end.
Restoration may need new sources under preserved Lyria rules or the proposed catalog
retry/budget policy; existing buffered audio may not outlast an outage. After a replacement has
played, failure recovery belongs to that program, not a return to an older one.

Preserve bot admission/supersession and cooldown-token semantics across both modes:
only the request owning a cooldown can free it. `/stop`, `/leave` and shutdown also
invalidate interpretation, HTTP/decode and delayed announcement work. A cancelled
reply waiter must not cancel the shared start future. Cleanup must close network
responses and stop/join or reap decoder work using the method selected in #118;
a timeout/cancellation must not leave a worker appending to an abandoned buffer.
Repeated retirement must not interrupt cleanup already in progress, preserving
`Deck.retire()`'s existing cancellation behavior.

### Catalog buffers and track failures

Use incremental in-memory streaming/decoding, outside the event loop and player
thread, into the same PCM format. Decoding and URL trust/redirect policy are gated
by #118 and #116's actual stream observations. Do not pass a credential-bearing
URL to a decoder command line or persist audio, including decoder temporary files.

#### Existing Lyria retention and observed states

The current station limits **non-ended Lyria producers**, not the number of
retained source buffers. `_generate()` counts every non-ended deck and fills the
two reserved session slots. `_discard()` preserves ended sources referenced by
the mixer's live/incoming pair and, while no open unused source is ready, the most
buffered ended ready spare for the current plan. These roles can overlap; ending a
producer frees a producer slot without necessarily freeing its audio or releasing
the station's process-wide reservation. Preserve that
rule order, generation and spare retention through the Lyria refactor.

The [rotation test](../../tests/test_station.py)
`test_retires_sessions_before_lyrias_limit` retires both old producers and opens
two replacements while keeping the ended live source and an ended ready spare.
The [independent review reproduction](https://github.com/slackysoba/sobafm/pull/156#discussion_r4212507908)
at `aeec82d9fb0a1a99e6e4682e2ae7279a268f2a11`, repeated during this correction,
observed these synthetic states without changing runtime or test files:

| State | Retained nonempty buffers | Open producers | PCM payload |
| --- | --- | --- | --- |
| Both replacements below pre-roll | Ended live 8 s; ended ready spare 60 s; two generating replacements 2 s each | 2 | 13,824,000 bytes |
| One replacement reaches 6 s pre-roll | Ended live 8 s; generating replacements 6 s and 2 s; ended spare discarded by the normal rule | 2 | 3,072,000 bytes |

Four retained buffers are therefore an allowed observed state. Neither three nor
four is an existing unconditional buffer-count ceiling. Enforcing three here would
suppress a replacement producer or discard the ready fallback, changing preserved
Lyria behavior. The reproduction measures PCM payload only; it does not establish
worst-case retention, receive overshoot, object/copy overhead or NFR-3 compliance.

ADR-0004 describes a three-buffer handover estimate. Three 60 s PCM payloads are
34,560,000 bytes at 192,000 bytes/s, leaving 5,440,000 bytes of a decimal
40,000,000-byte budget **in that scenario only**. This arithmetic is neither a
station-wide enforced maximum nor headroom guaranteed to a decoder in every state.
Sixty seconds is Lyria's pause target; it is not a proven hard allocation ceiling.

#### Combined budget and qualification gate

The selected accounting policy uses one combined byte budget across both engines,
accounting for every retained
Lyria source (including ended live/incoming/spare and replacement buffers), catalog
PCM, encoded queues, HTTP/pipe buffers, decoder in-flight output, native audio
buffers, assembler tails, copies and overshoot. Account once for a source occupying
more than one role. Do not allocate independent per-engine budgets or derive a
catalog allowance by subtracting three nominal Lyria buffers from 40 MB.

This budget is a **selected design guardrail**, not present enforcement or a
measured qualification result. Align with ADR-0007's separately supervised FFmpeg
contract. The chosen limits are deliberately bounded proposals; neither metadata
HEAD results nor arithmetic establishes their runtime feasibility:

| Allocation or limit | Selected contract |
| --- | --- |
| Aggregate retained PCM across both modes | At most 34,560,000 bytes; every actual retained source and copy counts. |
| Parent-process retained audio | At most 38,000,000 bytes including PCM, encoded input, transport/pipe queues, assembler tails, copies and overshoot. |
| Total audio | Parent audio plus audited native/child audio reserves stays strictly below 40,000,000 bytes. The 128 MiB child process cap is containment, not an audio reserve or proof of this sum. |
| Catalog PCM per source | At most 60 s, reduced/deferred when shared reservations cannot fit; no whole-track decode followed by trimming. |
| Encoded stream | At most 16 MiB cumulative input; at most 256 KiB retained encoded input per child, with writes at most 16 KiB followed by drain. |
| Transports and in-flight output | Reserve each bounded queue, including each transport's at-most-256 KiB allowance and overshoot, before starting its producer; no unaccounted pipe or native output. |
| Track duration | Valid finite positive metadata duration at most 900 s; at most 43,200,000 stereo samples at 48 kHz. Missing duration rejects the candidate. |
| Worker and progress bounds | At most two decoder children per server/four per process; enforce 128 MiB per child before input under #118's cgroup v2/Windows Job containment. First PCM and expected-progress idle deadlines are 15 s; the absolute deadline is 1,830 s. |

Reserve reachable future Lyria growth as well as presently retained bytes before
admitting catalog work. Aliases count once, copies separately. The shared ledger
outlives caller cancellation and station replacement; release a reservation only
when the producer, transport, child tree and retained bytes have actually released
their allocation. Cancellation/timeout alone never refunds debt or a child slot.
ADR-0007 defines one monotonic 3 s cleanup budget, concurrent drain/closure,
terminate then kill/reap; incomplete cleanup retains its debt and blocks admission.
Keep that exact lifecycle contract rather than invent a separate catalog timeout.

Preserve Lyria's two-producer generation and ended-spare rules; do not suppress
replacement generation or discard a playable fallback to meet an invented source
count. Admit new catalog work only when its qualified reservations fit alongside
all actual retained audio and in-flight allowances. Otherwise defer/refuse catalog
admission or reduce proposed catalog prefetch under the accepted policy, retaining
FB-3's heard program. Retirement/drop still follows source ownership and lifecycle,
not a blanket count limit. If no feasible envelope preserves Lyria behavior and
NFR-3, block adoption and have primary resolve the concrete feasibility conflict
under delegated authority with regression verification. This grants no NFR-3 waiver or
Lyria behavior change.

Sibling Proposed [ADR-0007 / PR #157](https://github.com/slackysoba/sobafm/pull/157)
corrected the earlier three-buffer maximum characterization at
`d858ce922dae4ba95c219bf8b91a3e72e5038ffa`, as
[independently verified](https://github.com/slackysoba/sobafm/issues/117#issuecomment-6048274985).
The selected limits above replace its earlier open allocation fields while
remaining unqualified against all retained Lyria states, transitions and overshoot.
Decision acceptance authorizes implementing and testing this contract; it asserts
no native-memory pass or available decoder headroom. Issues #119/#121/#125/#127/#131
must qualify combined bounds through rotation, mode changes, reconnect, error and
cancellation before catalog runtime readiness. If those bounds cannot coexist with
unchanged Lyria behavior, catalog admission remains unavailable and the concrete
conflict returns to the issue for a reviewed revision; this record grants no waiver.

Validate metadata, license and duration before fetching, then validate actual bytes,
format and decoded sample count. A streaming source becomes ready at 6 s of PCM.
A shorter positive-duration track becomes ready only after strict successful
completion has been validated; do not require padding it to 6 s or reject it solely
for being shorter. Strict completion requires clean HTTP EOF, agreement with any
declared Content-Length, stdout/stderr EOF and drain, decoder exit zero and reaping,
aligned samples and actual decoded duration within 1 s of validated metadata,
measured before tail padding. Pad only a verified final partial frame once to
3,840 bytes within the ledger. A normal track ends audibly only when those checks
pass **and** the final buffered PCM is consumed. Early/truncated EOF, invalid frames,
timeouts, excessive size/duration and decoder failure fail one track; a streamed
prefix might already have been heard and must never be reported as full completion.
Try another eligible candidate within the bounds below; do not reselect the same
failed id forever.
A started catalog program may refill after transient failure while its buffers play;
initial candidate/search exhaustion gives a specific failure and invokes FB-3.

Proposed request behavior:

| Failure | Response |
| --- | --- |
| Not-music/injection classification or Gemini safety refusal | Refuse; send no search or audio request, preserve current music. |
| Catalog interpretation timeout/error/invalid output | Report interpretation unavailable, preserve current music; do not search raw request text. Lyria retains ADR-0002's fallback. |
| Successful empty search | Use all-tags → any-tags (`fuzzytags`) → validated descriptive text with the fixed bounds below, retaining explicit attributes/license constraints. Exhaust these steps, then report no match. |
| Invalid client id, quota/rate limit, unavailable service | Distinct safe failure; bounded retries only for recoverable causes. Do not treat error envelopes as empty results or bypass quotas by relaxing filters. |
| Missing/unknown license or attribution; failed stream/decode | Skip the candidate with a sanitized reason; a failure before first playback leaves the prior program heard. |

Reuse the probe's safe-envelope lessons, not its code as a runtime API: validate
HTTP status and Jamendo `headers.status`/code; report allowlisted cause/status values,
never raw bodies, query strings, requests, stream URLs or third-party error text.
Google-specific classifications remain in the Lyria/interpretation paths.

#### Bounded selection and retry policy

These are application limits, not claims about provider quota or measured
coverage. One selection cycle serves initial start, skip or a next-track refill:

- At most two pages of ten results for each of the three search stages, using
  validated `limit`/`offset` parameters and relevance ordering from the
  [tracks API](https://developer.jamendo.com/v3.0/tracks). Stop when an eligible
  non-recent candidate is found; retain no more than 60 candidate records.
- At most eight metadata attempts total, including at most two transient retries
  across the cycle, each with a 10 s deadline, all within a 30 s cycle deadline.
  Retry connection/server failures after 1 s then 2 s only if the deadline allows;
  honor provider Retry-After without exceeding the deadline. An authorization or
  quota failure is terminal; HTTP/API errors never become an empty search.
- Bound each metadata body to 512 KiB, including decompression output. Reject
  malformed or oversized responses. One metadata call runs per station, at most
  two per process, with at most one new call per second process-wide; provider
  restrictions and backoff take precedence. Make no automatic refill loop on
  exhausted results: retry a playing program after 15, then 30, then 60 s, retaining
  the 60 s ceiling; a new accepted request starts its own bounded cycle.
- Try at most three distinct stream candidates per cycle, each under ADR-0007's
  download/decode deadlines; never decode two alternatives for one next-track
  slot. Keep at most the audible catalog track and one prepared next track;
  obsolete candidates must finish cleanup before their slot/reservation is reused.
- Keep the last 64 audibly accepted track ids per station across programs, expiring
  entries after 24 hours; insert only after heard-source settlement. Keep at most
  128 failed ids for ten minutes. Both collections clear on process restart.
  Exclude current/retained/prepared ids independently of history expiry. Exhaustion
  reports no eligible non-recent match rather than clearing history to repeat.

The 30 s limit bounds metadata selection, not track completion: an eligible
decoder may need its separate 15 s first-PCM bound after selection. #119 must state
and #131 measure actual start/skip latency; no chosen timeout is a latency pass.

### Commands, configuration and feedback

The [delegated product disposition](https://github.com/slackysoba/sobafm/issues/117#issuecomment-6197413269)
selects these rules for the proposed decision and subsequent #119 reconciliation:

- `/freestyle` retains today's generated behavior, timed duration, instrumental/
  wordless-vocal limits, access checks, model/fallback and slow-start feedback.
- `/play` starts an indefinite catalog program until replaced/stopped, listener
  grace expires or voice is lost. Lyrics may be requested. Settings duration applies
  only to generated music; volume and server-wide change cooldown apply to both.
- `/skip` applies only to catalog, with `/play`'s voice-channel rule and cooldown.
  If no next track is ready, prepare one while the current track continues, then
  apply the normal license-aware transition. Failed/no-op skips free their own
  cooldown; skipping is not a new model call or a member queue.
- Use optional runtime `SOBAFM_JAMENDO_CLIENT_ID`, validated and secret-held per
  operator. The standalone probe retains its historical `JAMENDO_CLIENT_ID` contract.
  Without runtime catalog configuration, do not register `/play` or `/skip`; log a
  fixed startup explanation. Keep `/freestyle` available, with the existing
  required Discord/Gemini settings. Never silently select generated music for `/play`.
  Removing credentials must remove stale registered commands at synchronization.
- Command/help/setup text describes requests sent to Google and descriptive searches
  sent to Jamendo, without Discord identifiers. Relative requests receive only
  the same-mode current plan; a cross-mode request starts fresh rather
  than silently translating MusicPlan into CatalogQuery. Explain this limitation.
  #116 does not demonstrate cross-mode refinement quality.

The [#124 setting note](https://github.com/slackysoba/sobafm/issues/124#issuecomment-6193410811)
and [#126 fallback note](https://github.com/slackysoba/sobafm/issues/126#issuecomment-6193412671)
preserve the historical runtime-name and raw-text-fallback conflicts. The linked
product disposition resolves them: runtime `SOBAFM_JAMENDO_CLIENT_ID`, unchanged
standalone probe spelling, and no raw-text catalog search on interpretation
failure. #119/#124/#126 must carry those selected contracts into implementation.

### Quiet catalog credits (approved presentation)

On 15 October the maintainer [approved the quiet layout](https://github.com/slackysoba/sobafm/issues/117#issuecomment-6193733521):
song and artist beneath the voice-channel name, with full credits and links
available privately through the existing `/now` command. This resolves the surface
preference; no further choice between quiet credits and per-track chat posts is
pending. It introduces no automatic track messages or new Send Messages permission.
The same receipt retains the preference for broad music variety. The surface
choice itself grants no third-party rights; the later linked catalog disposition
selects eligible license families and transitions without accepting this whole ADR.

The program title is distinct from a catalog track's title. Use each server's
voice-channel status for the song and artist heard in that channel. Discord's
[voice-status endpoint](https://docs.discord.com/developers/resources/channel#set-voice-channel-status)
is channel-specific and accepts up to 500 characters. Ordinary profile activity
has no per-guild target in the [presence update](https://docs.discord.com/developers/events/gateway-events#update-presence);
it cannot describe independent songs across this client's servers. Reuse today's
connected-channel status path, ordered requests, permission checks and retries.
FB-2 currently makes status optional; this choice adds no mandatory status
permission, Manage Channels requirement or playback-stop rule.

For every audible catalog track, private `/now` carries its complete required
credit: title, supplied creator and designated attribution parties, exact license
name/version and URI, Jamendo provider credit such as “Music from Jamendo”, and a
direct track backlink constructed from a validated id. Preserve required supplied
copyright, license and disclaimer notices and associated licensor links, plus any
modification credit required by the eventually accepted use. The sample's
[BY 3.0 terms](https://creativecommons.org/licenses/by/3.0/legalcode) section 4(a)/(b)
illustrate why the four basic metadata fields alone do not prove complete notice
coverage; determine obligations for each exact accepted license. Render third-party
text with escaping, bidi-control removal and mentions disabled, keeping validated,
credential-free application-built links usable with previews suppressed. A status
summary may be shortened; do not silently truncate required credit information in
`/now`. Apply the complete bounded-response protocol below.

Keep the existing initial `/play` reply within its original scope, describing the
actual audible source with its credit. It is not the durable credit updater for
later tracks: interaction edits have a limited lifetime, and some listeners may
not see the invocation channel. `/now` remains private to each caller on request;
one invocation does not make the reply visible to all listeners.

Update current-track feedback when a connected player accepts the incoming source,
matching today's playing settlement. The selected policy allows no catalog overlap.
If a later decision with actual rights permits overlap, retain both audible
catalog tracks' full credits in `/now` until the outgoing audio stops and identify
that transition in status; this conditional rule grants no overlap permission.
During pending or failed replacement, retain the credit
for what is still heard and distinguish starting intent from that audio. Do not
announce the next track while disconnected; cancelled/stale callbacks cannot
replace current feedback. Cover consecutive tracks within one program as well as
program changes.

#### Complete private-credit protocol

The following implementation choices complete the selected quiet presentation;
they do not establish legal sufficiency or live-client behavior:

- Keep `/now` guild-only without a new manager or voice-membership check. Operators
  must provide intended listeners an accessible channel where Discord permits
  the command. Server administrators retain command permissions. Setup and #130's
  clean-machine check must verify this route for an ordinary listener; a known
  inaccessible route cannot be described as attribution-compliant.
- Build a source-bound immutable credit snapshot before catalog admission. Include
  all known required supplied notices and attribution parties from the actual
  source, not merely the four discovery fields. Missing/conflicting required
  information makes that candidate ineligible. Notice ingestion and its source
  coverage require fixtures and actual-source qualification in #124/#128/#130;
  do not infer completeness from an API field being absent.
- Bound the complete raw credit record to 32 KiB UTF-8 and the complete escaped
  rendering to 16,000 UTF-16 code units. If required information exceeds either
  bound or cannot be rendered safely and completely, reject that candidate rather
  than truncate its notices. Render at most ten pages, each at most 1,900 UTF-16
  code units including labels, with intact usable links; never split a link.
  These conservative application bounds fit Discord's documented
  [message-content limit](https://docs.discord.com/developers/resources/message#create-message).
- `/now` responds privately with page one and caller-bound previous/next buttons.
  Navigation edits that private response; it never posts automatic per-track chat
  messages. Label the heard song and snapshot time so a page of an older track is
  not presented as current. Keep one view per caller/server, at most 16 per server
  and 64 per process, expiring/evicting the oldest after at most ten minutes. Expired
  navigation directs the listener to invoke `/now` again. Bound these metadata
  objects separately from audio and release them on station shutdown.
- Acknowledge/defer interactions within Discord's
  [three-second response window](https://docs.discord.com/developers/interactions/receiving-and-responding#responding-to-an-interaction).
  Page edits and error responses remain private, disable mentions/previews, honor
  library rate limits and never retry beyond the view's lifetime. Tokens/interaction
  objects never enter logs or persistent state. No public fallback or Send Messages
  permission is introduced for a failed private response.
- Accept only bounded, credential-free HTTP(S) licensor links without userinfo,
  control characters or private/local destinations; preserve required links
  completely or reject the candidate. Build license and Jamendo track/provider
  links from validated exact identities. Do not fetch an arbitrary licensor link
  merely to render it. Safe-link and full-notice tests precede runtime readiness.
- Preserve current best-effort optional status: show a shortened song/artist within
  500 characters; serialize requests, retry missing permission every 10 s and
  other failures after 10 s doubling to five minutes, and clear/reconcile on the
  existing reconnect/move/empty-channel paths. A stale/rate-limited/overwritten
  status cannot replace the private snapshot or stop playback. A private `/now`
  still identifies actual heard audio and may explain unavailable status. Do not
  add mandatory Set Voice Channel Status or Manage Channels permission.

Status-only title/artist omits license, provider credit and backlinks; raw status
URLs have no verified clickability guarantee. #128/#130 must verify complete
pagination, ordinary-listener command access, expired views, supplied-notice
ingestion, safe links, desktop/mobile readability and actual failed/stale status
behavior. An unavailable private route needs correction before a deployment is
qualified; it does not silently change optional status into a playback-stop rule.
No client, notice-coverage or rights-compliance pass is claimed by this decision.

### Selected catalog rights and playback policy

The [15 October catalog disposition](https://github.com/slackysoba/sobafm/issues/117#issuecomment-6197676011)
selects all six reviewed CC 3.0 **Unported** families, with validated exact license
identities and the actual track's rights and required notices. This replaces the
earlier pending ND alternatives and sequential-ND-fade recommendation. An unknown
version, a jurisdictional port, conflicting metadata or known unavailable required
notices makes a candidate ineligible; a generic CC or API license flag is insufficient.

| Eligible exact family | Reviewed legal code |
| --- | --- |
| CC BY 3.0 Unported | [BY 3.0](https://creativecommons.org/licenses/by/3.0/legalcode) |
| CC BY-NC 3.0 Unported | [BY-NC 3.0](https://creativecommons.org/licenses/by-nc/3.0/legalcode) |
| CC BY-ND 3.0 Unported | [BY-ND 3.0](https://creativecommons.org/licenses/by-nd/3.0/legalcode.en) |
| CC BY-NC-ND 3.0 Unported | [BY-NC-ND 3.0](https://creativecommons.org/licenses/by-nc-nd/3.0/legalcode) |
| CC BY-SA 3.0 Unported | [BY-SA 3.0](https://creativecommons.org/licenses/by-sa/3.0/legalcode) |
| CC BY-NC-SA 3.0 Unported | [BY-NC-SA 3.0](https://creativecommons.org/licenses/by-nc-sa/3.0/legalcode) |

These codes distinguish public performance and collections of separate unmodified
works from adaptations and permit technically necessary format changes. The selected
implementation interpretation is successive separate performance, without a remix
merely because generated music plays elsewhere in the station. ShareAlike does not
automatically license the bot or other independent works in a collection. This is
not a finding that every uploaded track, excerpt, operation or jurisdiction is
covered; resolve a concrete rights conflict before using the affected candidate.

Play **all catalog recordings successively**, normally through natural completion.
Apply **no automated fade or overlap to catalog audio**, irrespective of license
family. At a catalog-involving boundary, outgoing audio ends before incoming audio
starts. Generated-to-generated fades/crossfades retain their existing behavior.
Explicit skip, replacement or stop ceases a performance rather than creating an
edited recording; prepare the requested replacement first. This playback
interpretation is not a universal legal conclusion about excerpts or adaptations.
Ordinary non-clipping server volume remains available. Do not introduce catalog
remixing, expressive edits, time/pitch changes, loops or loudness processing.
Necessary decoding and transport conversion do not confer adaptation rights.
Any future catalog crossfade needs a separate decision supported by actual rights;
it is not an unresolved launch choice or part of this implementation contract.

Jamendo's [API terms](https://devportal.jamendo.com/api_terms_of_use), checked on
15 October, require per-application registration, protected credentials, compliance
with each content license, creator/provider credit and a direct track backlink
(clauses 2, 3.1, 3.3 and 4.1). Free API use is non-commercial even when an individual
track's license permits commercial use. Advertising, affiliate revenue, paid access
or another commercial purpose cannot be assumed eligible. The terms also require
an appropriate privacy policy and allow only operationally necessary caching,
with limitations, termination and future terms changes possible. No new agreement,
purchase or operator eligibility is established here. Use transient bounded
streaming buffers with no download/offline feature or retained audio/replay cache;
do not imply endorsement. The [tracks documentation](https://developer.jamendo.com/v3.0/tracks)
distinguishes stream `audio` from `audiodownload` and exposes license/format fields;
those fields alone do not prove rights, complete notices or actual decoded format.

Retain the accepted Discord transport and add no copy restriction denying listeners
their CC rights. The [CC FAQ on effective technological measures](https://creativecommons.org/faq/#what-do-i-do-if-someone-tries-to-place-effective-technological-measures-such-as-drm-on-my-cc-licensed-material)
does not equate every encryption system with prohibited DRM; the
[DAVE specification](https://daveprotocol.com/) describes participant decryption.
This supports the recorded implementation interpretation, not universal contractual
or jurisdictional clearance. Resolve any concrete rights conflict; do not disable
encryption or treat DAVE as blanket permission to distribute music.

### Explicit reconciliation of original crossfade criteria

The original M5 [outcome and exit criteria 2/4](https://github.com/slackysoba/sobafm/issues/114)
require catalog track-to-track and cross-mode crossfades. Issue #117 also originally
required cross-mode crossfades and recommended sequential **fades** for ND tracks.
Those original criteria remain preserved as historical requirements. The linked
delegated catalog disposition explicitly selects their replacement for every
catalog-involving boundary: **successive playback without catalog fades or overlap**.
This is a deliberate product tradeoff for broad eligible variety, not a claim that
crossfading is forbidden under every license or that the original criterion passed.

Once this record is accepted, #119 must reconcile PLAY-1, the cross-mode/track
architecture and M5 verification against that explicit scoped replacement before
runtime work depends on it. Preserve the 60-minute playback duration, at-most-2 s
underrun, NFR-3's <40 MB audio bound, supported platforms, generated behavior,
every-track credits and all other unaffected acceptance limits. No endurance,
relevance, resource, license-compliance or platform pass is asserted here.

## Consequences

A shared station avoids duplicating voice workarounds and preserves one command,
settings and deployment path. Engines prevent normal track EOF from becoming a
Lyria failure, while keeping generated music's proven recovery isolated. Jamendo
adds terms, metadata trust, relevance, attribution and service-dependency risks;
its free/non-commercial path is not an availability or future-price guarantee.
Transient audio and bounded station history limit retention and restart behavior.

The command rename is a breaking user-visible change if released v1 uses `/play`
for generated music. Version/upgrade decisions belong to #132, not this record.
Catalog-involving crossfades and the older ND-fade recommendation are explicitly
replaced by the selected successive-playback criterion above. #119 must record
the accepted scope before code uses it. All other acceptance limits stay intact;
no accepted ADR is edited by this proposal.

### Acceptance and subsequent qualification

The recorded M4/research dispositions and delegated product/catalog/credits choices
resolve their specific gates. Independent review and required checks gate merging
the decision; the following implementation/qualification work gates catalog runtime
readiness and shipping, without circularly requiring implementation before its
architecture can be accepted. A waived research deliverable is not a measured pass.

| Field | Current evidence / acceptance condition | Owner |
| --- | --- | --- |
| M4 readiness and research start | Established by [M4's recorded dispositions](https://github.com/slackysoba/sobafm/issues/4#issuecomment-6097955604) and the [frozen research protocol](https://github.com/slackysoba/sobafm/issues/116#issuecomment-6104782646). Original 10 October start and missed 12 October deadline remain recorded above. | Maintainer/operator, coordinated by primary |
| Relevance and refusal controls | [D1](https://github.com/slackysoba/sobafm/issues/116#issuecomment-6104976415): all 48 classifications pass; 38/42 music cases have at least three candidates. [Maintainer acceptance](https://github.com/slackysoba/sobafm/issues/116#issuecomment-6145275917) closes #116 qualitatively and waives remaining structured ratings/export and calculated go/no-go. Frozen 28/42 audible-fit rule remains unmeasured; ratings/summary stay unchanged. Settle eligible-catalog coverage separately using the subset caveat above. | Maintainer on #117; #126 quality follow-through |
| Streams and service failures | [D1](https://github.com/slackysoba/sobafm/issues/116#issuecomment-6104976415) supplies metadata/HEAD outcomes, not audio GET/decoding/playback or causes of earlier failures. Implement #118's validated transport/decode contract and the fixed metadata/search/retry bounds above; qualify actual streams before catalog readiness. | #118/#124/#125 |
| Service, license and transitions | [Selected under delegated authority](https://github.com/slackysoba/sobafm/issues/117#issuecomment-6197676011): the six exact reviewed CC 3.0 Unported families, actual rights/notices and eligible non-commercial Jamendo use; successive catalog performances without catalog fade/overlap, with generated-to-generated behavior unchanged. #119 must explicitly carry the scoped replacement of the original crossfade criteria. Individual rights/notices and implementation compliance remain to be verified; the launch policy is no longer pending approval. | Primary/#119/#124/#128/#130 and independent reviewers |
| Track attribution | [Quiet presentation approved](https://github.com/slackysoba/sobafm/issues/117#issuecomment-6193733521); the bounded complete-credit and optional-status protocol above is selected. Notice completeness, listener access, links, pagination and actual status failure behavior require implementation/client qualification before catalog readiness. | #119/#124/#128/#130 |
| Product semantics and engine seam | [Selected under delegated authority](https://github.com/slackysoba/sobafm/issues/117#issuecomment-6197413269): one station/two engines, indefinite catalog/requested vocals, generated-only duration/fallback, shared volume/cooldown, ready-replacement `/skip`, same-mode context, safe bounded catalog search, namespaced runtime setting and generated-only behavior without catalog configuration. Preserve the source-specific handover responsibility and verify EOF versus starvation. These are no longer pending maintainer choices; #119 reconciles requirements before implementation. | Primary/#119 and later implementation reviewers |
| Bounded audio envelope | The shared byte-ledger and numeric guardrails above are selected with #118; no combined playback, native-audio reserve or usefulness is qualified. Implement enforcement, reserve actual reachable Lyria growth and prove strict <40 MB across rotation/transitions/reconnect/error/cancellation before catalog readiness. No three-/four-buffer count maximum or guaranteed headroom is asserted. | #119/#121/#125/#127; qualification [#175](https://github.com/slackysoba/sobafm/issues/175) before #131/#132 |
| Decoder distribution/isolation | [Separately supervised FFmpeg selected](https://github.com/slackysoba/sobafm/issues/118#issuecomment-6193735484), subject to accepted ADR-0007. The [candidate assessment](https://github.com/slackysoba/sobafm/issues/118#issuecomment-6068002879) and [correction](https://github.com/slackysoba/sobafm/issues/118#issuecomment-6068937334) do not qualify containment, resource enforcement, compiler-free supported-platform installation, exact source/notices/rebuild/replacement or distribution. Complete those gates before catalog readiness/shipping. | Native supply [#174](https://github.com/slackysoba/sobafm/issues/174), qualification [#175](https://github.com/slackysoba/sobafm/issues/175), plus #125/#130/#131/#132 |
| Independent review and acceptance | [Prior preparation review](https://github.com/slackysoba/sobafm/pull/156#pullrequestreview-5449250778) is historical. Obtain separate review of this final exact head/base, record dispositions and verify required checks before merging. This record becomes Accepted only on that reviewed merge; no author-side merge or runtime qualification is implied. | Primary under delegated authority; independent reviewer |

The `maintainer-approval` label preserves decision review; delegated authority
resolves the routine choice gates. #116 is closed under its narrow qualitative
acceptance and structured-rating waiver. #119 follows acceptance of both decisions;
later issues retain actual requirements and qualification dependencies. A draft PR,
research waiver or successful offline tests do not bypass readiness/shipping gates.

### Follow-on verification and ownership

These are future gated acceptance checks, not experiments performed by this draft:

- **#119:** write stable M5 requirement IDs and architecture for the accepted mode,
  failure, privacy/terms, attribution and memory policies; carry the approved quiet
  presentation through CMD-3/FB-1/FB-2, specify remaining access/failure behavior
  and explicitly replace the original catalog-involving crossfade/ND-fade criteria
  with successive catalog playback while preserving every unaffected limit.
- **#120:** move Lyria rules only, keeping test files/constants/log lines unchanged.
  Require the full suite and unchanged test diff, then the authorized live behavior
  comparison. Preserve [station tests](../../tests/test_station.py) for dropped-deck
  release with `gc` disabled, failure ordering, deferred give-up and #93's
  final-retry/second-refusal/later-quota cases, plus [deck tests](../../tests/test_deck.py)
  for exception/cancellation cleanup.
- **#121:** exercise unchanged generated crossfades, successive catalog-involving
  boundaries and rollback both ways, BUSY preserving catalog, rapid A→B→C
  replacement, reservations during pending/failed switches and cleanup, old
  generated deadline expiry, stop/disconnect while interpreting/filling/fading,
  stale completions, weak-reference release and process-wide caps across stations.
  Preserve the rotation state with two replacement producers and an ended ready
  spare until an open unused source becomes ready; do not add a blanket count cap.
  Keep Lyria behavior tests with fixture-only adjustments.
- **#122/#123/#126:** preserve generated interpretation/refusal/fallback behavior,
  validate catalog output, test relative-context limits and catalog failure with
  no raw-text search. Evaluation calls remain opt-in and operator-authorized.
- **#124/#125/#127:** fake HTTP/decoder tests for normal EOF, short/truncated tracks,
  bounded relaxation/retries/no-repeat history, malformed external input,
  redirected URLs, all buffer bounds/copies and cancellation cleanup. Measure
  the accepted decoder on each supported platform under #118's gates.
- **#128/#129/#130:** verify actual-source quiet credits, complete
  supplied notices, exact safe links and escaped text without losing required
  information. Verify private `/now` access for intended listeners, long credits,
  desktop/mobile readability, independent songs in two servers, status/reply
  ordering through reconnect/slow start and failed/stale/missing-permission status
  under the accepted policy. Cover no-id registration cleanup, skip without a ready
  source and shared cooldown ownership. Verify no catalog fade/overlap at natural
  completion or either mode boundary, and cease-performance behavior for skip,
  replacement, stop and cancellation. If a future rights-backed policy permits
  overlap, test both complete credits then; no overlap is allowed by this contract.
  Document exact license identities, supplied notices, setup and service terms.
- **#131/#132:** perform the milestone's authorized live endurance/latency,
  successive catalog/cross-mode transition, attribution, memory and clean-machine
  checks only after earlier decisions/gates pass, including applicable #175
  supported-platform qualification and #174 native source/notices supply. #175
  follows implementation in #121/#125/#127; those implementation issues must not
  depend back on completed #175. Preserve the repository's version, publication
  and release authority requirements.

## Revisit triggers

- New quality evidence undermines the accepted research direction, or an eligible-license
  policy materially changes coverage/relevance; #116's numerical mark remains unmeasured.
- Jamendo changes terms, availability, formats, license metadata, quota or price.
- Legal/rights guidance changes the allowed transitions or attribution surface.
- The selected decoder cannot satisfy the combined footprint, isolation,
  installation or redistribution requirements on all supported platforms.
- Lyria changes the session/concurrency behavior underlying ADR-0004, or the
  extraction changes generated behavior, logging, reservations or cleanup.
- A second catalog is actually proposed; assess it separately before generalizing.
