# Catalog qualification — issue 175

Status: **incomplete; catalog unavailable**. This record follows
[ADR-0007](decisions/0007-bound-catalog-audio-decoding.md),
[requirements](requirements.md) and [issue 175](https://github.com/slackysoba/sobafm/issues/175).
It distinguishes implemented controls, retained evidence and missing measurements.
It grants no resource allowance, production trust, release or endurance pass.
The [fixed-field checkpoint](catalog-qualification-results.json) retains every
required platform as incomplete; null bounds mean unknown, never zero usage.

## Source and evidence boundary

The initial source is main `4fc157559a1036a4e5e3f20ec0591c8000df3541`, tree
`6b77282c0579d323c74ece1a7ba82ea090e86443`. Issue 129 has a separate owner.
Station/Mixer/command integration must bind its actual integrated source before
affected qualification; this checkpoint changes none of those implementations.
Issue 129 subsequently merged in PR 195 as
`6e261bbbdc896e3209ac6298b859386a4e4e3b93`. The qualification branch incorporates
that actual integrated base while retaining the earlier WJ1 snapshot. Affected
native integration is still unmeasured; ordinary combined-source offline checks
cannot replace it.
Every executed batch records exact source-file, protocol, fixture, interpreter,
configuration and applicable runtime-package identities. Changed bytes require a
new reviewed protocol revision; results are never transferred by equal inputs.

Reuse the [verified supply pairs](native-supply-identities.json) and their
[original limits](native-supply-qualification.md). They establish the specific
native installation/rebuild/replacement observations recorded by issue 174.
The four owned-fixture capability checks in issue 125 establish codec loading,
not untrusted-input containment or a complete audio envelope. Historical C1
measurements, failed attempts, emulated image checks, source-oracle gaps and
consumed diagnostic routes retain their original meanings. No archive download,
source authentication, scanner or unchanged capability rerun is part of this
protocol. A subsequent upload needs its own observed artifact identity.

## Requirement and proof map

| Contract | Implemented control / retained proof | Missing qualification |
| --- | --- | --- |
| OPS-3/4, NFR-4: compiler-free source and applicable images on all native targets | `native.py`, native supply recipe, recipient and replacement receipts in the supply record | Current application installations bound to each actual native Linux x86-64/ARM64 and Windows x86-64 qualification; emulation cannot fill a row |
| NFR-7: containment before input, child-only cap failure | `Child.launch` prepares a private boundary; `TrackDeck._run` binds before constructing its input; cgroup limit/membership and Windows Job readbacks; fake ordering regressions | Actual selected decoder startup, inherited containment, cap failure and bot survival per native installation |
| PLAY-10/11: incremental decoding, readiness and strict completion | `catalog_audio.py`, `TrackDeck._feed/_output/_run`; fake split, duration, truncation, padding, EOF and backpressure tests | Native decoder and HTTP behavior for the finite owned fixture cases below; useful actual catalog format/duration coverage remains separate |
| NFR-8: one total cleanup deadline and retained debt | `TrackDeck._begin_cleanup/_cleanup`, `InputCloser`, supervisor registry and `Slots`; fake pending-launch, close, cancellation and descendant-debt tests | Actual native cancellation, inherited pipe/tree and handle closure; all stages must share the original three seconds |
| NFR-3/6: complete source audio strictly below 40,000,000 bytes | `AudioLedger`, `AudioLease`, synthetic envelope arithmetic; unknown obligations refuse admission | Audited native/HTTP/pipe/copy/overshoot/player bounds, all retained Lyria states, reachable future growth, and actual storage-release signals |
| PLAY-9, FB-7: successive audible boundaries; generated fades unchanged | Source-bound Mixer receipts and station settlement; existing offline transition regressions | Rebind issue 129 integration, then actual first/final audible frame and stale/cancel behavior; generated comparison and live latency remain later gates |
| FB-5/6, ACC-5, OPS-8: source notices and listener access | Immutable credits, bounded private views and empty production composition | Actual source-notice completeness, ordinary-listener access, usable desktop/mobile links and optional-status failures; no private-client probe in this batch |
| Useful catalog input and completion tolerance | Metadata schema, 900-second/16MiB guards and chosen one-second tolerance | Separately reviewed cumulative provider protocol; HEAD metadata and owned fixtures cannot establish useful catalog coverage |

The current `LyriaEngine` creates an unknown audio obligation and retains it across
shutdown/station replacement. There is no hard qualified Lyria receive, copy,
transport or release envelope. `DecoderSupervisor` supplies no production
`Envelope`; the native trust registry is empty. Those are explicit unresolved
prerequisites, not numerical measurements, observed overruns or unused headroom.
The initial decision remains **no-go for enabling catalog playback**.

### Complete resident-audio audit

This is a source audit, not a memory measurement. The installed locked
Google Gen AI 2.26.0, websockets 16.1.1 and discord.py 2.7.1 sources supply the
following ownership edges; freeze their actual file hashes in an execution packet.

| Storage / growth edge | Current owner and release condition | Remaining proof |
| --- | --- | --- |
| Lyria message, raw JSON, validated chunks and decoded byte objects | SDK `AsyncMusicSession._receive`, then `Deck._receive` | SDK/WebSocket message limits do not establish total retained audio, copy overlap or cumulative post-pause delivery; audit the complete locked receive path |
| Lyria current/ended/spare/replacement deques | `Deck.frames`; retained under existing engine rules | `regulate` sends pause at 60 seconds and retries failed pause; `_receive` continues appending. There is no local hard total byte cap. An enforceable future-growth envelope preserving the approved behavior is still absent |
| Frame assembly and copies | `FrameSplitter.split` extends a bytearray, slices it, builds distinct bytes and returns a list before deque insertion | Charge simultaneous input, pending storage, temporary slices and returned frames; for Lyria the input bound is not yet qualified |
| Catalog retained PCM and splitter tail | `TrackDeck.frames`, splitter and complete allocation lease | Fixed per-read and deque bounds exist; actual copy/transport/native maxima and the separate final storage-release owner remain unqualified |
| PCM popped from a source | Mixer read and discord.py `AudioPlayer._do_run` local `data` | A frame may remain held during a voice reconnect wait. Deque empty, source completion or station detach does not prove its release |
| Gain/mix, Opus, DAVE and encrypted packet copies | Mixer, `VoiceClient.send_audio_packet`, encoder and voice transport | Audit distinct backing allocations, native state and in-flight packet ownership, including failed send/reconnect and actual release; no RSS substitute |
| Catalog HTTP/TLS, encoded chunks and each stdin/stdout/stderr pipe | `AudioStream`, `Child`, CPython transports/readers | Flow-control thresholds and handle closure do not bound all retained reader/native storage; include every overshoot and pending copy |
| Pending launch, descendants and cleanup tasks | Supervisor cleanup registry, child slot and source lease | Actual task/tree/pipe release can refund child capacity; the complete source-audio lease remains charged separately. Removing a debt record cannot supply the missing storage proof |
| Old engine, player, armed handoff and delivered receipt | Server ledger plus source references retained across station replacement | Rebind issue 129 and audit every retained role once per backing allocation; aliases are not copies, and dropping a reference is not allocator release proof |

Consequently, supply integrity plus a successful decoder run cannot establish
NFR-3/6. Resolving just native decoder storage would still leave the Lyria,
player/transport and release obligations above. No new hard Lyria cap, spare
discard policy, numerical waiver or readiness toggle is selected by this audit.

## Finite protocol WJ1: owned Windows boundary fixtures

The single reviewed WJ1 batch is now **executed and consumed**. Its fixed-case
results are below; this section retains the protocol, not authorization to rerun
it. The original pre-execution protocol and source are preserved byte-for-byte in
snapshot commit `97c99f53cefc10677d8b5d4fd338ae5bda6a1b2b` (tree
`7751c4fa952ce6715b261b0373e614975001d381`). That snapshot commit was created
after execution to preserve the already-reviewed staged tree; it is not a new
execution identity. The actual execution records base `4fc15755` plus exact file
hashes. [Source binding](catalog-qualification-evidence/wj1-a1/source-snapshot.json)
verifies all fifteen executed source/protocol hashes against the snapshot's
actual Git bytes.

Purpose: measure the production `WindowsJob`, `Child.bind/write`, process-tree and
pipe-release adapters with new owned Python fixtures on the current native
Windows x86-64 source host. This is a mechanism check. It cannot qualify FFmpeg,
clean installation, audio accounting, TrackDeck's complete cleanup owner, Linux,
an image or catalog readiness. It does not use native supply archives.

Before execution, independently review this protocol and the exact runner/helper
bytes. Freeze their hashes together with `decoder_process.py`, `uv.lock`, the
current source commit, signed installed CPython executable hash/version and the
parent interpreter. Record review and dispositions on issue 175. Any correction
invalidates the reviewed executable packet until re-reviewed. Run only after the
locked pre-edit baseline passes. Ordinary fake tests are independent of this gate.

The proposed runner is `scripts/qualify_catalog_boundary.py`; its only executable
fixture is `scripts/catalog_boundary_fixture.py`. Review the actual runnable
files separately before execution. It creates a fresh private Job per case,
using the production 134,217,728-byte process/job caps and kill-on-close. The
fixture uses isolated CPython (`-I -S`) with the production minimal environment.
It waits for one owned byte before doing case work. Nothing may write that byte
until Job assignment and membership have succeeded. No secret, provider, catalog
URL/audio, bot, Docker/WSL guest, host setting or existing process is inspected.

| Case | Fixed action and oracle |
| --- | --- |
| `ordering` | A pre-bind production `Child.write` must refuse. After binding, one byte releases the fixture; exactly `O` and zero exit are required. |
| `memory` | After binding, attempt one 144MiB allocation inside the 128MiB Job. The owned helper catches `MemoryError`, emits exactly `M`, exits 23; successful allocation or unexpected output/exit fails the case. This tests refusal, not an OS termination guarantee. |
| `descendant` | After binding, the fixture creates one isolated Python descendant inheriting stdout/stderr and the Job, emits exactly `D`, then the leader exits. The descendant emits `H` and holds inherited pipes for at most 60 seconds. Before retirement, require both markers, actual zero leader exit, a nonempty Job and both readers still without EOF. This positive control prevents an already-dead descendant from satisfying the oracle. Job termination must then empty the tree and close pipes within the original cleanup deadline. |

Reuse `tests.test_track_deck.Rig` and inject a real `Child`/`WindowsJob` spawn
instead of its fake process. The existing fixture-only metadata, placeholder
runtime and envelope are constructor controls, never verified FFmpeg identities
or measured audio allowances. They are not used to execute a decoder. `O`, `M`
and `D`/`H` are deliberately invalid PCM; these cases exercise native boundary
and production `TrackDeck` cleanup/failure ownership. They cannot qualify MP3,
strict completion, readiness or audio capacity. Keep the complete fixture audio
lease charged; child-slot release is a separate observed condition.
Pass additionally requires the exact production outcome: `OUTPUT`/failed for
`ordering`, `DECODER`/failed for `memory`, and retired without failure for the
explicitly cancelled descendant case. A false completion or different failure
cannot satisfy those oracles. Observation/report exceptions produce a fixed
failed/uncertain result, and the outer retention guard still awaits the same
cleanup owner; include the raising-observer/repeated-cancellation offline control
in executable review.

Cumulative bounds: one attempt per case, no retry/fallback; at most four fixture
processes total, two simultaneously, one Job at a time. Startup/response deadline
five seconds per case, then one three-second monotonic cleanup budget covering
stdin close, both drains, task joins, terminate/kill, direct-child reap, empty Job
readback and actual pipe/Job closure. Terminate wait at most one second and kill
wait at most one further second, both inside that same budget. Stop admission of
new cases at 30 seconds. Output is capped at 64 bytes per pipe per case; only fixed
allowlisted result fields are saved. No raw diagnostics or process identifiers.
The largest allocation request is 150,994,944 bytes, charged to the private Job;
even a failed enforcement control has only that finite single request.

Each result distinguishes `pass`, `fail` and `incomplete`, with fixed reason
codes, exit status, cleanup duration and separate tree/pipe/Job closure booleans.
Overall catalog qualification always remains incomplete. Write identity and each
case decision to fresh fixed-field files once; preserve failed/partial results.
OS rejection or uncertain cleanup stops case admission with remaining cases
incomplete; do not relocate, retry, bypass policy or refund unverified ownership.
Thirty seconds is the case-admission/reporting budget, not permission to terminate
the supervising interpreter or cancel retained cleanup. On a missed three-second
decision, save the failed decision before continuing to await the same production
cleanup owner. Retain the runner, rig, Job, tasks, slots and leases until actual
release; the descendant's independent 60-second maximum lifetime still applies.
Do not let `asyncio.run` tear down a pending owner. If release remains uncertain,
leave that owner active and require attention; never report a final release file.
A safety failure is not permission to continue with more cases.

### Observed WJ1 result and limits

The [execution receipt](catalog-qualification-evidence/wj1-a1/execution-tool.json)
records one batch at 07:20:36–07:20:43 UTC on 21 October 2026, exit zero. The
[identity](catalog-qualification-evidence/wj1-a1/identity.json),
[runtime preflight](catalog-qualification-evidence/wj1-a1/runtime-preflight.json),
[independent pre-execution review](catalog-qualification-evidence/wj1-a1/pre-execution-review.md)
and its [23-file index](catalog-qualification-evidence/wj1-a1/pre-execution-index.json)
are retained. The author read the full review/index and verified all 23 pins before
execution. Prior blocked reviews retain their original findings and snapshots.
The committed JSON records use UTF-8/LF formatting. The
[projection manifest](catalog-qualification-evidence/wj1-a1/projection-manifest.json)
binds each actual original and public byte identity and verifies identical parsed
values. Original local records remain unchanged; formatting does not transfer an
artifact identity or create a new execution.

| Fixed case | Actual outcome | Observed cleanup decision |
| --- | --- | ---: |
| [ordering](catalog-qualification-evidence/wj1-a1/ordering.json) | Pre-bind write refused; assigned input produced expected invalid-PCM `output` failure | 3.621 ms |
| [memory](catalog-qualification-evidence/wj1-a1/memory.json) | The 144MiB allocation was refused inside the 128MiB Job; helper exited 23 and TrackDeck failed `decoder` | 1.244 ms |
| [descendant](catalog-qualification-evidence/wj1-a1/descendant.json) | Live post-leader descendant and inherited pipes positively observed; intentional retirement cleaned the owned tree | 12.805 ms |

All three cases observed released child slots and actual pipe/Job handle closure
while retaining their synthetic complete-audio leases. The
[final observation](catalog-qualification-evidence/wj1-a1/finished.json) records
unchanged executed source and finished cleanup owners. These are individual
observations on the fixed Python cases. They are not a general three-second
guarantee, an FFmpeg containment/completion result, measured audio headroom,
clean installation, native hardware/platform coverage or production readiness.
No audio/provider input was used. The complete native cleanup matrix, repeated
native cancellation and all other installation rows remain unqualified.

Offline failures are retained separately from this native result. The first
focused correction round exposed the old final-padding test's 30ms fill
assumption (281 rather than 300 frames); its deadline now expires only after
the actual padding wait is reached. A later full offline run retained
1,721 passes and one failure when 191,996 one-byte fake reads exceeded the
test helper's five-second guard. The split fixture now uses two frames while
retaining every split size and exact tail-padding comparison. An attempted
fractional-duration fixture was rejected by the integer metadata schema in six
cases; that correction attempt is also preserved. The final fixture uses valid
whole-second metadata within the unchanged one-second completion allowance.
No schema, production timing or decoder behavior was changed.

The smaller split fixture was corrected **after** WJ1. The WJ1 observation stays
bound to its executed snapshot; it is not relabeled as an execution of later
test/document bytes. No native replay is needed to preserve a historical result.

## Decoder and complete-audio follow-through

The following is a finite candidate matrix for a later reviewed batch, **not
authorization to execute it**. Bind exact runnable bytes, runtime/package hashes,
native installation, enforcement preflight, fixture digests, complete measured
envelope and expected outcomes before review. Missing prerequisites produce an
incomplete row. No guessed fixture envelope enables production.

Use existing `TrackDeck`, `AudioStream`, supervisor, ledger and fixture factories.
Construct new owned zero-spectral MP3 cases from the documented frame format;
do not repeat the four completed load checks. Feed from owned incremental input
and a credential-free controlled HTTP fixture, never a real catalog stream in
this initial matrix. Save counters/outcomes only, never PCM or encoded audio.

| Group | Finite cases per native installation | Required observations |
| --- | --- | --- |
| Admission/launch | Missing executable, assignment refusal, child-cap breach | Zero input before verified boundary; no bot failure; slots and debts remain charged through actual release |
| Completion | 8-second ordinary stream; 0.36-second strict short stream; empty/malformed; zero-exit truncation; metadata ±1-second boundary; unaligned final sample; aligned partial-frame tail | Six-second readiness or strict short completion; actual samples before one padding frame; both EOFs, clean HTTP, exit/reap and later audible drain are distinct |
| Transport/progress | Oversize advertised length; actual encoded overrun; split headers/body/output; stalled input/output; intentional PCM backpressure | Counters before writes/retention, no replay/redirect/compression, real timeout and lifetime bounds; reserve transport overshoot rather than infer it from reader limits |
| Cleanup | Connecting cancellation; repeated waiter cancellation; inherited-pipe descendant; retained cleanup failure | One original total three-second deadline, complete task/pipe/tree closure or sticky charged debt; later release cannot erase the failure |
| Integration | Failure before handover and after audible prefix; catalog succession; both mode boundaries; replacement/stop/voice invalidation | Current source preserved where viable; no false completion, catalog fade/overlap or stale settlement; generated behavior unchanged; issue 129 source binding required |

The later batch ceiling is 24 cases per installation, one child group at a time,
128MiB per child, at most two children per server/four globally. At most 16MiB
encoded input and 2MiB retained PCM per case, 64MiB encoded cumulative per
installation, 30 seconds per case and 12 minutes total. A case needing the full
1,830-second lifetime cap must use deterministic clock/component verification
and remain explicitly unmeasured natively under this batch. No limit may be
silently shortened and described as a native pass at the production limit.

Before running even those bounded cases, audit disjoint backing allocations:
retained PCM/current/armed/event/task references, splitter and mixing copies,
HTTP/TLS and each pipe buffer including overshoot/in-flight reads, native decoder
storage, Opus/DAVE/transport storage and every retained Lyria state/future receive.
Assign an actual storage owner and verified release event to each. The envelope
must fit unchanged Lyria rules plus nonzero useful catalog capacity: aggregate
PCM ≤34,560,000, parent ≤38,000,000 and total strictly <40,000,000 bytes. RSS,
GC, deque drain, direct-child reap and nominal sums are insufficient evidence.
If a finite bound or release signal is missing, retain unknown debt and no-go.

## Platform and gate status

| Installation | Current qualification |
| --- | --- |
| Linux x86-64 source | Incomplete: no new exact-source native decoder/resource result |
| Native Linux ARM64 source | Incomplete: no native ARM64 host bound to this protocol |
| Windows x86-64 source | Incomplete: WJ1 passed only its fixed Python mechanism cases; decoder/audio/clean installation and native hardware qualification remain open |
| Linux x86-64 image | Incomplete: no application qualification with delegated child cgroup and complete accounting |
| Native Linux ARM64 image | Incomplete: emulated ordinary CI does not qualify this installation |

Actual catalog usefulness, source rights/notices and listener-client access need
their own bounded protocol and evidence. Provider cumulative budgets remain zero
in this checkpoint. Issue 131's actual 60-minute endurance, generated comparison
and separate admitted play/skip latency, issue 130's guide walkthrough, and issue
132's durable source/notices/distribution and shipping gates remain open.
