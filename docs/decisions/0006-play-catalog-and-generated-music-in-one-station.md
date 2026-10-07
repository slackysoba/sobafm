# ADR-0006: Play catalog and generated music in one station

- **Status:** Proposed; not accepted or authorized for runtime adoption
- **Date:** 2026-10-07
- **Issue:** [#117](https://github.com/slackysoba/sobafm/issues/117)
- **Parent:** [M5, #114](https://github.com/slackysoba/sobafm/issues/114)
- **Evidence:** [#116](https://github.com/slackysoba/sobafm/issues/116), incomplete

## Context

M5 proposes `/play` for matching Jamendo tracks and `/freestyle` for today's
Lyria RealTime music in one bot. The maintainer chose the one-bot direction over
[#40](https://github.com/slackysoba/sobafm/issues/40); #117 still needs an accepted
service, product and engine decision. This record makes the proposal reviewable
while live research waits. Every design rule below is conditional on acceptance.

The [requirements](../requirements.md) and [architecture](../architecture.md)
still describe approved v1 behavior. This proposal neither changes them nor
supersedes accepted ADRs. Adoption would require #119 to reconcile CMD-1 to CMD-3,
PLAY-1 to PLAY-3 and PLAY-8, AI-1 to AI-7, FB-1 to FB-4, OPS-1, NFR-1 to NFR-3 and
USE-1 to USE-3. In particular, indefinite catalog programs, lyrics and an ND
transition exception differ from current requirements and need explicit approval.

### Current source and accepted decisions

The source assessment uses main commit
[`2dec12be698845a89bf88070eae07f2fdae3d69e`](https://github.com/slackysoba/sobafm/tree/2dec12be698845a89bf88070eae07f2fdae3d69e).
Relative source links below identify the modules to compare during review.

| Existing surface | Reuse and boundary |
| --- | --- |
| [ADR-0001](0001-build-on-python-discord-py-and-the-google-gen-ai-sdk.md), [mixer.py](../../src/sobafm/mixer.py), [pcm.py](../../src/sobafm/pcm.py) | Keep discord.py's player, Opus/DAVE, PCM framing, gain and fades. `FrameSource` already requires only a `deque[bytes]`; the mixer cannot fetch or decode audio. |
| [ADR-0002](0002-interpret-requests-with-gemini-structured-output.md), [interpreter.py](../../src/sobafm/interpreter.py), [plan.py](../../src/sobafm/plan.py) | Preserve validated structured interpretation, refusal controls and descriptive artist/work translation. `MusicPlan` and its raw-text fallback remain Lyria-specific. The probe's models are experimental evidence, not adopted runtime contracts. |
| [ADR-0003](0003-keep-server-state-in-sqlite-and-use-discord-command-permissions.md), [store.py](../../src/sobafm/store.py) | Reuse per-server settings and Discord command permissions. Programs, reservations, buffers and recent-track history stay in memory. |
| [ADR-0004](0004-stream-lyria-realtime-through-buffered-decks.md), [station.py](../../src/sobafm/station.py), [deck.py](../../src/sobafm/deck.py) | Preserve Lyria session rotation and failure semantics. `_retire`, `_generate`, `_discard`, `_count_failure`, `_may_start`, `_ready_deck` and `Program` retry fields contain the mode-specific rules. |
| [bot.py](../../src/sobafm/bot.py), [commands.py](../../src/sobafm/commands.py), [status.py](../../src/sobafm/status.py), [config.py](../../src/sobafm/config.py) | Reuse voice recovery, command admission/cooldown, request supersession, slow-start replies and ordered status updates. Playback/feedback/configuration wiring still needs adaptation; these entire modules are not source-independent. |

The offline [probe and operator guide](../catalog-probe.md) merged in
[PR #144](https://github.com/slackysoba/sobafm/pull/144). There is no measured
relevance result, listening rating or go recommendation. #116 still requires M4
readiness, operator credentials, about ten non-personal maintainer requests and
approval of the final manifest/model/pass mark before its two-day measurement.
No live Gemini, Lyria or Jamendo calls are part of this proposal preparation.

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
3. Keep the process-wide Lyria cap, default four sessions, configured by
   `SOBAFM_MAX_SESSIONS`. Catalog programs consume no Lyria reservation.
4. Never block the event loop with decoding or the player with I/O, decoding or
   waiting. Every mixer read returns a 3,840-byte, 20 ms PCM frame or silence.
5. Keep all retained audio below NFR-3's 40 MB per playing server, including mode
   transitions, compressed input, decoder queues/copies and ended sources.
6. Meet the selected catalog license, attribution, service and privacy obligations
   before playing a track. Keep credentials and credential-bearing URLs out of
   logs/replies; validate external metadata/model output before it changes state.
7. Retain M4 and #116's evidence gates and #118's independent decoder decision.
   No account, commercial service, dependency or distribution exception is
   approved by preparing this record.

## Options considered

| Option | Fit and reuse | Costs and risks |
| --- | --- | --- |
| **1. One station with one engine per mode** | Reuses the existing mixer and voice recovery. Sources share PCM; engines isolate Lyria lifetime/retries from catalog selection/EOF. One station retains FB-3 and transitions in both directions. | Requires careful program identity, reservation ownership and a shared audio budget. Jamendo adds per-operator setup, terms, attribution and availability risk. No new audio server or versioned shared package is needed. |
| **2. A station subclass per mode** | Initially less extraction; reuses voice and mixer code within each station. | Replacing the station loses the audible program and its recovery/reservation state. To provide cross-mode FB-3 and overlapping handovers, a further coordinator would be needed, duplicating option 1's core ownership. |
| **3. Separate bots with an extracted shared package** | Strong deployment isolation; generated music can release separately. | Two Discord applications and operating paths; versioned voice fixes and package maintenance. No shared crossfade or cross-mode FB-3; contradicts the one-bot direction recorded on issue #117. |

A new managed playback service/library is not a substitute for this seam: the
[existing-solution review](../architecture.md#existing-solution-review) found no
library that mixes Lyria's live PCM into discord.py's source. Reuse the existing
mixer rather than add another service. If #116 fails, the alternative is to retain
v1 and end M5 as its milestone specifies, not adopt a second catalog implicitly.

## Decision

**Recommendation, still proposed:** option 1, with Jamendo API v3 as the first
catalog, subject to the evidence and approval conditions below. Keep the surface
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
trigger unconditionally could silently cut tracks short; it needs maintainer review.
Catalog `reserve` needs no Lyria slots; source creation still requires audio-budget
admission. `going_live` updates track history/feedback only after an audible switch
is accepted, not when a candidate is fetched.

### Reconcile order and Lyria regressions

Preserve this order across all retained sources, including the old mode:

1. **End:** connection loss, generated-program deadline and listener grace take
   precedence. Catalog has no deadline in the proposed product policy.
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
4. **Generate:** the desired engine opens next sources within its limits,
   reservations, backoff and the station's remaining audio budget. Lyria may
   still have outgoing sessions, so a new one waits until a slot actually closes.
5. **Start:** choose a ready source and fade in only with a running player and
   connected voice. Report playing only then; disconnected readiness is not success.
6. **Hand over or stop:** no new switch while one is in progress. Lyria selects
   the most buffered ready source of its plan; catalog selects the earliest
   selected eligible ready track. A mode/plan replacement is station-driven. Lyria
   hands over below 4 s buffered; catalog hands over below 4 s only after normal
   decoded EOF, or on an explicit skip/failed producer. A transient low buffer
   alone plays silence/retries instead of declaring a track finished. Use the
   existing 4 s clamped equal-power crossfade except the proposed ND policy. Without a program, retire idle sources,
   finish any transition, fade out, stop and release resources. With no player,
   stop immediately rather than waiting for unread fades.
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
Restoration may need new sources under its engine's retry/budget rules; it is not a
claim that existing buffered audio can outlast every outage. After a replacement has
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

The station admits at most three retained source buffers in total: live/outgoing,
incoming and one next/preparing source. Retire/drop unused obsolete sources before
creating another; do not give each engine a separate allowance of three. A Lyria
handover can already use three 60 s buffers: 34,560,000 PCM bytes at 192,000 bytes/s.
That leaves only 5,440,000 bytes of a conservative decimal 40,000,000-byte budget
before queue objects, overshoot, compressed input and decoder copies. Sixty seconds
is today's Lyria pause target, not proof of a hard memory cap.

Recommend a hard 60 s catalog PCM queue ceiling with producer backpressure and a
station-wide byte budget covering every encoded chunk, PCM chunk/copy and queued
frame. Reserve room for decoder in-flight output and Lyria receive overshoot before
starting work; do not decode a whole track then trim its PCM. The exact encoded-byte,
track-duration, decode-time, chunk, history and search-page caps remain open evidence
fields below. If the accepted decoder cannot stay inside the envelope, reduce
catalog prefetch or reject a track, rather than weaken NFR-3 silently.

Validate metadata, license and duration before fetching, then validate actual bytes,
format and decoded sample count. Recommend requiring at least the existing 6 s
pre-roll for catalog readiness, rejecting a track that completes short of it. Normal
EOF retains its remaining PCM until consumed; early/truncated EOF, invalid frames,
timeouts, excessive size/duration and decoder failure fail one track. Try another
eligible candidate with bounded attempts; do not reselect the same failed id forever.
A started catalog program may refill after transient failure while its buffers play;
initial candidate/search exhaustion gives a specific failure and invokes FB-3.

Proposed request behavior:

| Failure | Response |
| --- | --- |
| Not-music/injection classification or Gemini safety refusal | Refuse; send no search or audio request, preserve current music. |
| Catalog interpretation timeout/error/invalid output | Report interpretation unavailable, preserve current music; do not search raw request text. Lyria retains ADR-0002's fallback. |
| Successful empty search | Recommend all tags → any tag (`fuzzytags`) → validated descriptive text, retaining explicit attributes/license constraints. Exhaust these bounded steps, then report no match. #116's experimental relaxation requires runtime acceptance. |
| Invalid client id, quota/rate limit, unavailable service | Distinct safe failure; bounded retries only for recoverable causes. Do not treat error envelopes as empty results or bypass quotas by relaxing filters. |
| Missing/unknown license or attribution; failed stream/decode | Skip the candidate with a sanitized reason; a failure before first playback leaves the prior program heard. |

Reuse the probe's safe-envelope lessons, not its code as a runtime API: validate
HTTP status and Jamendo `headers.status`/code; report allowlisted cause/status values,
never raw bodies, query strings, requests, stream URLs or third-party error text.
Google-specific classifications remain in the Lyria/interpretation paths.

### Commands, configuration and feedback

Recommend these changes for explicit product acceptance and subsequent #119 work:

- `/freestyle` retains today's generated behavior, timed duration, instrumental/
  wordless-vocal limits, access checks, model/fallback and slow-start feedback.
- `/play` starts an indefinite catalog program until replaced/stopped, listener
  grace expires or voice is lost. Lyrics may be requested. Settings duration applies
  only to generated music; volume and server-wide change cooldown apply to both.
- `/skip` applies only to catalog, with `/play`'s voice-channel rule and cooldown.
  If no next track is ready, prepare one while the current track continues, then
  apply the normal license-aware transition. Failed/no-op skips free their own
  cooldown; skipping is not a new model call or a member queue.
- Propose optional `JAMENDO_CLIENT_ID`, consistent with the probe, validated and
  secret-held per operator. Without it, do not register `/play` or `/skip`; log a
  fixed startup explanation. Keep `/freestyle` available, with the existing
  required Discord/Gemini settings. Never silently select generated music for `/play`.
  Removing credentials must remove stale registered commands at synchronization.
- Command/help/setup text describes requests sent to Google and descriptive searches
  sent to Jamendo, without Discord identifiers. For relative requests, recommend
  passing a same-mode current plan only; a cross-mode request starts fresh rather
  than silently translating MusicPlan into CatalogQuery. Explain this limitation.
  #116 does not demonstrate cross-mode refinement quality.

The program title is distinct from a catalog track's title. For catalog playback,
`/play`'s successful reply and private `/now` show the audible track's title, artist,
license label/deed link, provider credit “Music from Jamendo” and a track backlink
constructed from a validated id. Render third-party text with escaping, length caps,
bidi-control removal and mentions disabled; only application-built allowlisted
links remain clickable. Show title/artist in voice status within its limits, using
today's ordered requests, permission checks and retry policy.

Propose updating current-track feedback when a connected player accepts the incoming
source, matching today's playing settlement. During overlap, `/now` includes both
tracks' attribution until the outgoing source is no longer used; the initial reply
must describe the actual source, not a candidate or query title. During pending
replacement, `/now` distinguishes starting intent from what is still heard.
Do not announce the next track while disconnected; cancelled/stale callbacks cannot
replace current feedback. Tests must cover consecutive tracks within one program,
not only changes between programs.

Whether this is sufficient visible attribution for **every** track is unresolved:
voice status has no full license/backlink, `/now` is on demand, and an interaction
reply can be edited only within its lifetime. #117 proposes deferring per-track
chat posts to avoid a new Send Messages permission. The maintainer must approve
that attribution approach or authorize another persistent surface/per-track posts,
with its permission and privacy consequences, before #128; no permission change
is made here.

### Service terms and ND transitions

Official sources checked on 2026-10-07; factual terms and proposed policy are
separate. Jamendo's [API terms](https://devportal.jamendo.com/api_terms_of_use)
(clauses 2, 3.1, 3.3, 4.1, 7 and 8) require per-application registration, protected
credentials, compliance with each content license, creator/provider credit and a
track backlink. They allow free non-commercial API use, require an appropriate
privacy policy and permit only operationally necessary caching, not applications
designed for caching/offline access. They allow API limitations/termination and
terms changes. This is not evidence that every Discord deployment is permitted.
The [tracks documentation](https://developer.jamendo.com/v3.0/tracks) distinguishes
stream `audio` from `audiodownload` and its download-allowed flag, exposes CC license
filters and lists `mp31`/`mp32`. It does not establish measured formats/size/relevance.

The [CC BY-NC-ND 4.0 legal code](https://creativecommons.org/licenses/by-nc-nd/4.0/legalcode.en)
sections 1, 2(a)(1), 2(a)(4) and 3 permit non-commercial sharing of licensed material,
not sharing adapted material; necessary technical format changes alone are not
adaptation. Attribution includes supplied creator/notices, a license link and an
indication of modifications. This does not decide whether crossfading, volume fades,
truncation or mixing with generated music is an adaptation in the applicable law.
The [CC FAQ](https://creativecommons.org/faq/#when-is-my-use-considered-an-adaptation)
says adaptation depends on applicable copyright law; its discussion of ND/collections
also distinguishes older license versions. A 4.0 rule cannot approve all versions.

**Conservative proposed policy:** allow NC tracks only within a non-commercial
operator deployment; validate and retain each track's exact license/version and
attribution. Use streaming with transient bounded memory only, no downloads/offline
feature, no audio retention/replay cache. Do not use logos or imply endorsement.
If ND is admitted, never overlap it with any other catalog or generated source in
either direction: fade the outgoing source completely to silence, then fade in the
ready incoming source, even for skip/mode change. Keep the player alive between
those steps and wait for real connected reads to finish the first fade. This reduces
mixing risk; it is **not** a legal conclusion that fades/truncation are permitted.

| ND alternative | Tradeoff and approval needed |
| --- | --- |
| Allow ND with sequential transitions, as #117 recommends | More candidate tracks; a deliberate overlap exception and possibly an audible gap. Requires approval of the exact fade/skip/volume behavior and changes to M5's crossfade criteria. |
| Exclude ND until use is resolved | Preserves crossfade behavior for eligible licenses, but may harm relevance. Requires #116 results scored with the proposed eligible-license set. |
| Obtain appropriate rights/clarification for overlap | May support crossfades and more tracks; needs specific reliable permission and any separate cost/vendor decision. No contact or purchase is authorized here. |

Recommend the first alternative only if the maintainer establishes its use is
acceptable and accepts the product exception; otherwise exclude ND and reassess
relevance before a go. Unknown licenses fail closed. The exact version allowlist and
ShareAlike compatibility/attribution for overlap also need approval; API license
flags alone are insufficient. No blanket claim that “CC licensed” authorizes mixing,
DAVE transport, the operator's use or all supplied notice requirements is made.

## Consequences

A shared station avoids duplicating voice workarounds and preserves one command,
settings and deployment path. Engines prevent normal track EOF from becoming a
Lyria failure, while keeping generated music's proven recovery isolated. Jamendo
adds terms, metadata trust, relevance, attribution and service-dependency risks;
its free/non-commercial path is not an availability or future-price guarantee.
Transient audio and bounded station history limit retention and restart behavior.

The command rename is a breaking user-visible change if released v1 uses `/play`
for generated music. Version/upgrade decisions belong to #132, not this record.
The chosen ND/attribution policy may change M5's exit criteria; #119 must record
accepted behavior before code uses it. No accepted ADR is edited by this proposal.

### Evidence and decisions required before acceptance

Keep these fields open until actual evidence and explicit decisions are linked on
issue #117. “Pending” is not a passing result.

| Field | Current evidence / acceptance condition | Owner |
| --- | --- | --- |
| M4 readiness and research start | Pending. Confirm ready-to-ship on #4/#116 and record the actual two-day start. | Maintainer/operator, coordinated by primary |
| Relevance and refusal controls | Offline preparation only. Link the approved final manifest digest/model/pass mark, about ten additional non-personal requests, completed real ratings/results and a go recommendation. Proposed pass: 2/3 of music cases have ≥3 fitting top-five tracks, separate refusal controls; not yet approved. Recheck eligibility after license policy is settled. | #116 maintainer/operator |
| Streams and service failures | Pending. Record real mp31/mp32 sizes/formats, latency, sanitized error-envelope behavior, redirects and credential propagation; establish approved URL/redirect bounds without posting URLs/credentials. | #116, informs #118/#124 |
| Service, license and ND policy | Pending. Approve non-commercial use, exact license/version allowlist (including SA), fade/skip/overlap behavior, attribution obligations and any ND crossfade/exit-criterion exception. Recheck current official terms before acceptance. | Maintainer on #117 |
| Track attribution surface | Pending. Approve on-demand/current-track attribution or a persistent/per-track surface with precise permission/privacy implications; verify every played track remains attributable. | Maintainer on #117; #128/#130 |
| Product semantics | Pending. Accept indefinite catalog/lyrics, duration scope, `/skip` readiness/cooldown, missing-id registration and same-mode relative context, or record chosen alternatives. | Maintainer on #117; #119 |
| Bounded audio envelope | No catalog measurement. Before decision acceptance, use #116/#118 to document a feasible numeric envelope for encoded input, chunks/in-flight output, duration and decode time, plus proposed retry/page/history caps. Runtime proof of combined <40 MB at transitions/error/cancellation is a later #125/#127/#131 gate, not a prerequisite that requires implementing before issue #117. | #116/#118 and maintainer; later #119/#125/#127/#131 |
| Decoder distribution/isolation | #118's [feasibility evidence](https://github.com/slackysoba/sobafm/issues/118#issuecomment-6032402289) is provisional: PyAV's inspected bundle has GPL obligations; miniaudio lacks the required ARM64 wheel; subprocess feasibility needs binary/license/isolation approval. No decoder selected. | Maintainer on #118 |
| Independent review and acceptance | Primary requests independent proposal review and records dispositions; required checks pass on final head. Maintainer settles open choices, changes Proposed to Accepted in the reviewed proposal and merges it explicitly. No agent merge or runtime adoption in this preparation slice. | Primary and maintainer |

Issue #117 remains Blocked with `maintainer-approval` for the final decision. #116's live
measurement/go-no-go and #118 remain incomplete; #119–#132 remain Backlog. A draft
PR or successful offline tests do not bypass these gates.

### Follow-on verification and ownership

These are future gated acceptance checks, not experiments performed by this draft:

- **#119:** write stable M5 requirement IDs and architecture for the accepted mode,
  failure, privacy/terms, attribution and memory policies; reconcile any exceptions.
- **#120:** move Lyria rules only, keeping test files/constants/log lines unchanged.
  Require the full suite and unchanged test diff, then the authorized live behavior
  comparison. Preserve [station tests](../../tests/test_station.py) for dropped-deck
  release with `gc` disabled, failure ordering, deferred give-up and #93's
  final-retry/second-refusal/later-quota cases, plus [deck tests](../../tests/test_deck.py)
  for exception/cancellation cleanup.
- **#121:** exercise crossfades/rollback both ways, BUSY preserving catalog, rapid
  A→B→C replacement, reservations during pending/failed switches and cleanup, old
  generated deadline expiry, stop/disconnect while interpreting/filling/fading,
  stale completions, weak-reference release and process-wide caps across stations.
  Keep Lyria behavior tests with fixture-only adjustments.
- **#122/#123/#126:** preserve generated interpretation/refusal/fallback behavior,
  validate catalog output, test relative-context limits and catalog failure with
  no raw-text search. Evaluation calls remain opt-in and operator-authorized.
- **#124/#125/#127:** fake HTTP/decoder tests for normal EOF, short/truncated tracks,
  bounded relaxation/retries/no-repeat history, malformed external input,
  redirected URLs, all buffer bounds/copies and cancellation cleanup. Measure
  the accepted decoder on each supported platform under #118's gates.
- **#128/#129/#130:** verify correct source/overlap attribution and escaped text,
  status/reply ordering through reconnect/slow start, no-id registration cleanup,
  skip with no ready source and shared cooldown ownership. Verify the approved
  ND path with both directions, stop and cancellation; document setup and terms.
- **#131/#132:** perform the milestone's authorized live endurance/latency,
  cross-mode/ND transition, attribution, memory and clean-machine checks only after
  earlier decisions/gates pass; maintainer owns version, publication and release.

## Revisit triggers

- #116 fails the approved mark or the eligible-license set changes its outcome.
- Jamendo changes terms, availability, formats, license metadata, quota or price.
- Legal/rights guidance changes the allowed transitions or attribution surface.
- The selected decoder cannot satisfy the combined footprint, isolation,
  installation or redistribution requirements on all supported platforms.
- Lyria changes the session/concurrency behavior underlying ADR-0004, or the
  extraction changes generated behavior, logging, reservations or cleanup.
- A second catalog is actually proposed; assess it separately before generalizing.
