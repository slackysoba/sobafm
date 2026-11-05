# Architecture

The offline catalog selector, the pure station-fact and credit-rendering logic, the PCM banks, the first-slice decode producer and the Discord adapter for the first playback slice are implemented. `python -m sobafm` connects them with a startup limit gate, and a Dockerfile and Compose service package the result ([operations](operations.md)). None of it has been built as an image, run under its limits or tested live. The Docker resource model for the first amd64 playback slice is [accepted](decisions/0004-amd64-whole-service-adoption.md) but not yet qualified. The rest of the station, playback and deployment architecture below remains proposed and unqualified.

SobaFM has one station/player per server. Commands validate access and intent. A mode-specific interpreter produces a validated musical plan. Catalog selection or generated production prepares audio; the connected player settles what listeners actually hear. Settings and remembered channels persist in SQLite.

Catalog preparation happens ahead of playback: source admission, provenance, credits, curation and descriptors. Runtime selection ranks eligible candidates and retrieves audio according to source policy. Start small; add adapters or audio analysis only when useful.

## Implemented catalog boundary

`catalog.py` validates bounded prepared metadata, explicit queries and separate trusted admission reviews. Reviews bind complete credits and access readiness to an immutable recording digest. Unreviewed, changed, unavailable or incomplete recordings remain rejected. No provider or model approval field is accepted in recording metadata.

`catalog_prompt.py` translates a small, explicit phrase vocabulary into that query model. It reports unknown positive terms and refuses unsupported hard constraints rather than guessing. `catalog_demo.py` reads local JSON and a prompt, returning full match evidence, preserved credits, rejection reasons and coverage for each of Jamendo, ccMixter and FMA. It accepts reviewed real metadata as well as explicitly synthetic test fixtures.

`catalog_import.py` converts bounded saved Jamendo tracks responses, ccMixter info responses and explicit FMA operator manifests into the same validated snapshot schema. Source-specific field maps preserve identity, credit evidence and unknowns. A separate report records malformed rows, missing requested IDs and bounded source coverage; conflicting duplicate identities are quarantined together. Outputs contain no media URLs or generated admission decisions. The importer's draft metadata still needs complete-kind/access evidence and separate credit review before selection admission. See [saved metadata imports](catalog-evaluation.md#saved-source-metadata-imports).

Selection compares every supplied partition before applying the result limit. Strong supported preferences precede weaker evidence and duration; hard constraints filter first. Partition completeness describes only the dated prepared subset. The modules perform no provider calls, audio access, state changes or Discord operations. See the [selection contract and demo](catalog-evaluation.md#current-selection-contract) and [accepted decision](decisions/0002-prompt-catalog-selection.md).

The separate `ccmixter_proof.py` evaluation uses two closed typed recording definitions in `ccmixter_cases.py`: WILD WINDSTEPS (70993/file129564) and the complete Fruitless mix (70072/file125900). Each case can recheck its six fixed metadata responses and acquire one temporary candidate file only after explicit invocation with a reviewed, candidate-bound fingerprint manifest. Its versioned policy normalizes only a unique, bounded decimal value inside an exact hidden HTML anchor context; all other page bytes and the complete info JSON remain protected. Only the selected case's nominated info URLs additionally accept `text/plain`; byte-exact hashing, duplicate-key-rejecting JSON parsing and candidate validation remain mandatory. Receipts retain a bounded MIME type/subtype without parameters or other response headers. Raw response hashes stay separate from comparison fingerprints. Case, catalog digest, projection/context and complete manifest binding precede resource creation. Child requests carry the case identity; cross-case routes fail before HTTP. File selection is by unique ID; stems and Fruitless upstream Vox/Guitar files cannot become playback candidates. Its single request owner retains processes, counters and files across cancellation and failed cleanup; there are no automatic retries. It reuses the importer and catalog digest without granting admission. Decoding, embedded notices and complete-file validation remain pending. See the [finite source proof and execution boundary](../evaluations/catalog/source-proof.md); this is not a production provider or playback implementation.

The audio callback consumes prepared PCM without blocking. Producers and cleanup retain explicit ownership across cancellation. Capacity admission includes retained work and cleanup debt. Prove the chosen Linux enforcement configuration before depending on it throughout the application.

## Implemented PCM boundary

`pcm.py` fixes the playback format in one place: stereo signed 16-bit at 48 kHz, 192,000 bytes per second and 3,840-byte 20 ms frames. `GLOBAL_PCM_BANKS` is the process-wide owner of the two 2,304,000-byte banks that make up the accepted 4,608,000-byte PCM reservation ([ADR 0004](decisions/0004-amd64-whole-service-adoption.md)). It grants a lease before allocating a bank's backing and refuses when both leases are held, so nothing is allocated for a refused request. A lease stays charged until `release()` actually frees the backing. Release is refused while a producer region or any view of the backing still exists; cancellation only records its first monotonic time and stops the stream, and repeated cancellation changes nothing. The pool adds no capacity, baseline subtraction or other partition.

Each bank is a single-producer, single-consumer ring. The producer borrows a contiguous free region (suitable for a pipe `readinto`), fills it outside the bank lock and commits the filled prefix. It must settle every borrow with `commit_write`, using `commit_write(0)` on error, even if it already released the region view. `write()` is a copying convenience. The voice callback's `read_frame()` takes the bank lock without waiting and copies at most one 3,840-byte frame. It returns `None` when no frame is available now (not ready, underrun or a brief contended operation) and `b""` once no frame will follow. `ready_now()` reads the monotonic readiness flag without the lock, so the callback can tell an underrun from a stream that is not yet ready. Frames are readable only after readiness: 1,152,000 buffered bytes (six seconds), or the producer's verified end of a shorter valid stream. A decoder's output length need not be a multiple of the frame. At the verified end, a stream that is a whole number of 4-byte stereo samples and holds at least one whole frame is valid PCM; its remainder, shorter than one frame, is not emitted, and audible drain follows the last whole frame. A length that is not a whole number of samples means truncated output and fails as `partial_final_frame`; a nonzero stream shorter than one whole frame has nothing to emit and fails as `empty_pcm`, as does an empty one. Partial bytes are never emitted or padded with silence.

Transfer completion and decoder completion are recorded by their owners. Valid PCM comes from the verified end-of-PCM check. Audible drain means every whole frame of an ended, admitted stream has been handed to the voice callback. Each fact is tracked separately and none implies another. The module has no Discord library, decoder process, station logic, threads or dependency. `wait_writable()` lets a producer wait for free space without polling; the voice callback's frame reads notify it and never block. The decode producer below supplies the first completion policy and cleanup owner; voice integration belongs to a later slice. The 3,840-byte frame copy returned to the callback is application audio copy payload under the separate copy reservation; this module does not account for it. Offline tests do not qualify voice timing, Linux containment or useful fit.

The separate Linux-only `fma_intake.py` evaluation owns one explicitly supplied local Night Owl copy in one private destination slot. Its closed v2 request binds the unadmitted catalog, notices, exact file identity/hash, a neutral retention anchor with exact/lower-bound basis and bounded operator evidence, and fixed expiry. Historical v1 records retain exact-time semantics and fingerprints. One initially inert validation consumer may borrow the registered read-only descriptor for synchronous seekable inspection, then receive a bounded, identity/hash/count-verified stream. Reader and downstream handles, operations and backing remain owned across cancellation until actual join/close; blocking callbacks never hold the admission lock. Recovery is cleanup-only and refuses uncertain persisted reader ownership. A fresh local helper may inherit the descriptor through maintained process facilities; this does not establish Docker descriptor transport or authorize native execution. The supplied original remains untouched during this synthetic implementation work, and no catalog admission or availability changes. See the [operator workflow and reader contract](../evaluations/catalog/fma-local-intake.md).

Compact intake persistence, size gates and canonical hashes share one production
`json_records.py` traversal with thin strict evaluation adapters. State is proved
within 16,384 bytes before temporary-file mutation, then written through borrowed
views while the original owner retains file/worker debt. Canonical fingerprints
use fixed 4,096-byte hash backing without a new fingerprint-size or 128-bit scalar
restriction. Per-operation state scalar and freeze phases have separate 3L and
2L bounds; source facts and concurrent report/hash work remain additional charges.
The ordinary CLI's spaced output/flush ownership, complete J/F accounting and
native qualification remain open. See the evaluation's compact-record ledger.

## Implemented decode producer

`decode.py` is the first-slice producer for one admitted recording. A `DecodeInput` names the local file, its exact size and SHA-256, and a finite PCM output limit chosen by the caller; the limit is an overrun bound, not a duration oracle. One `DecodeProducer` owns the input file, one stock mpg123 child in its own process group, the child's stdin/stdout pipes, a feeder thread, a reader thread and one `GLOBAL_PCM_BANKS` lease. A caller that already holds a lease can pass that bank instead: the producer fills it at the same point in the launch, never acquires, keeps the bank's earlier first cancellation as its original clock, and leaves release of the lease to the caller.

Launch follows the [ADR 0004](decisions/0004-amd64-whole-service-adoption.md) premises. The decoder is started with `subprocess.Popen` (pipes, no PTY, stderr discarded, closed descriptors, a fixed minimal environment) before this producer leases PCM or starts a thread. `Popen` returns only after exec, so a forked child image is gone before any input; any bank it could have inherited was already leased and charged. The reviewed per-decoder limits (core 0, stack 524,288, data 12,000,000 and address space 12,524,288 bytes) are applied with `prlimit` after exec and before the first input byte. The argv is the one the #44 evaluation established: stereo signed 16-bit at 48 kHz on stdout, input on stdin, `--no-resync`. Nothing is imported from `evaluations/`; a test pins the argv to the evaluation's.

The feeder streams the file through a fixed 16,384-byte chunk, checking size and hash, and closes stdin only at exact verified EOF. A truncated, oversized, unreadable or mismatched input never gives the decoder end of input: stdin stays open until the child is reaped. The reader fills the bank through `reserve_write`/`commit_write` and waits on `wait_writable()` when the bank is full. Output beyond the limit is an overrun. Transfer completion, decoder completion (exit status zero without cancellation, because mpg123 also exits zero on SIGTERM) and valid PCM are recorded separately in the bank and the `DecodeReport`. The verified end of PCM is declared only after a complete transfer, exit status zero and no failure, so a nonzero exit after full output and an overrun fail. The bank then applies the length rule above: sample-aligned output ending inside a frame is valid and every whole frame stays readable after `finish()` returns, while output that is not a whole number of samples or holds no whole frame fails. Any failure stops the bank's stream. Consumers only read frames from the bank and stop a decode through `DecodeProducer.cancel()`, not by cancelling the bank directly.

`finish()` is the single cleanup owner. The original deadline is three seconds after the first of cancellation, a failure or the decoder's end of output; repeated cancellation does not reset it. Cancellation or failure sends SIGTERM to the group at once and SIGKILL two seconds in. Once the stream stops, the reader closes the decoder's stdout, so a decoder blocked writing to a full pipe gets EPIPE and acts on SIGTERM instead of waiting for SIGKILL. If the bank's owner stopped the stream before calling `cancel()`, the reader first adopts that cancellation, with the owner's original time, so the decoder's exit and any failed input write count as cancellation; a decoder that ends its output but does not exit within one second is a stalled failure. The leader's exit is observed through a pidfd, the group is swept with SIGKILL while the unreaped leader pins its ID, and then the leader is reaped. Threads are joined and descriptors closed only after that. If a feeder or reader thread cannot start, that is a `thread_start` failure, and only the threads that did start are owned and joined. Settlement after the deadline is an `expired` verdict that a later release never changes. `release()` returns the bank lease only after the process, threads and descriptors have actually settled, and only when the caller no longer needs the bank.

The input chunk is application audio/IPC copy payload under the separate copy reservation, which this module does not account for. Tests use small Python children in place of mpg123. They do not qualify native mpg123 behavior, the limits under the real image, or fit under the group limit, which is a later local step.

## Implemented station facts and credits

`station.py` keeps one server's desired program, prepared audio, claimed switch and heard source as separate facts in a single-owner `Station`. Each accepted request gets an increasing generation and reports the unheard program whose work the caller must cancel. Preparation results, failures and switch reports carry their program or claimed audio; anything for a superseded program is returned as stale and changes nothing. Prepared audio is recorded only after its credits render. The player claims ready audio and then reports an actual switch or failure; only the switch changes the heard source. A failed, rejected or cancelled replacement reverts the desired program to the one still heard and leaves its credits unchanged. Stop ends the desired program; the heard source clears only when the player reports silence. The module holds no audio, tasks, threads, persistence or clock, and has no member queue, skip, replacement scheduling, generated mode or settings.

`credits.py` renders a recording's existing `catalog.py` credit snapshot: title, creators, source backlink, license and every notice. Provider text has bidirectional controls stripped, other control and line/paragraph separators replaced by spaces, and Discord markdown, mention, emoji and autolink characters backslash-escaped. Links are rendered only after the catalog's credential-free HTTPS validation plus an RFC 3986 character check, inside angle brackets. Pages are filled continuously up to 1,968 UTF-16 code units plus a footer, within Discord's 2,000-unit message limit; credits needing more than 16 pages raise `CreditRejected`, and the station treats that as a rejected candidate rather than truncating notices. Each page states that mentions are disallowed and previews suppressed for the Discord layer. `CreditViews` retains at most 256 caller-bound views, each expiring a fixed time after opening (600 seconds by default, at most 900) under injected monotonic time; when full it evicts expired then oldest views. Wrong-caller, unknown/expired and out-of-range requests receive refusals without page content.

## Implemented Discord adapter

`discord_adapter.py` registers `/join`, `/play request:<text>` (1 to 200 characters), `/now`, `/stop` and `/leave` with an `InteractionBot` that requests exactly the Guilds and Voice States intents. Commands are registered for the server context only and also refuse a missing server at runtime. `/join` and `/leave` carry Manage Server as their default member permission, so administrators customize access through Discord command permissions rather than a second role system. `/play` and `/stop` require the caller to be in the bot's voice channel; `/stop` also accepts a caller with Manage Server from elsewhere. `/now` has no extra requirement. Without a joined voice connection, `/play` and `/stop` explain that a manager must use `/join`. `/now` answers at once; the other commands defer before any voice, selection or cleanup wait, all ephemerally and with mentions disabled.

`/play` interprets the request with `catalog_prompt.py` and selects one match from the configured prepared snapshot with `catalog.py`. No query or no match changes nothing and substitutes nothing. A match becomes a new `station.py` program; its credits render before it is prepared. The adapter then claims the audio, leases a `pcm.py` bank from `GLOBAL_PCM_BANKS` and starts the injected `Producer` on the event loop. A refused lease reports a failed switch, leaving current audio heard. Only one unheard track is pending per server: a newer request displaces it, reports its switch as failed and joins its retirement (bounded by five seconds) before leasing again. Responses say the request is accepted and preparing, never that it is audible.

`StationSource` is the voice callback. It takes its lock without waiting and copies at most one frame from the pending or current bank. It switches when the pending bank yields its first frame, which `pcm.py` allows only after readiness. Before that, and during an underrun, it returns a silent frame so the player keeps its timing; it never pads a partial final frame. Under the same non-blocking lock it counts, as plain integers on the track, the whole frames it handed to the voice player and the silence frames it returned while the track was current, or pending and ready (underruns). These are frames handed to the player, not packets confirmed delivered. Switch, failed, drained and retired reports go to the event loop with `call_soon_threadsafe`. Only the switched report changes the heard source, so `/now` keeps the heard title and full credits until then and states when a newer request is starting. `/now` sends a status line followed by every rendered credit page as private follow-ups, with previews suppressed. A drained track that is still the desired program ends it, because continuous selection is outside this slice. When nothing is current or pending, the source returns end of stream and the player stops; the next claim restarts it.

Every retired, displaced, failed or stopped track keeps its bank and producer task in one retirement task: cancel the bank, cancel and join the producer, then release. The cleanup deadline is three seconds from the bank's first recorded cancellation; a later cancellation does not reset it. A producer that settles after the deadline is a failed cleanup verdict, logged and counted, and the retirement keeps joining it and holds the bank until it settles. A bank that cannot yet be released stays leased in the adapter's debt list. `/stop` removes all audio from the callback, reports silence and waits up to five seconds for retirements before replying; ownership continues after that. `/leave` also disconnects. If the voice player cannot start, the claimed track is reported as a failed switch and retired.

SIGTERM and SIGINT set a stop event. From then on `/join` and `/play` are refused, a connection completed during shutdown is dropped, and no new bank is leased. `run` repeatedly disconnects every server and waits for all retirements until none remain, yielding to the event loop on every pass so a finished retirement's bookkeeping callback can run, retries release of retained banks and closes the client. It exits 0 only when no bank is retained, the pool reports no remaining lease and no cleanup deadline was missed.

The adapter logs what was played at INFO, under the requirements' logging rule and never with request text, tokens or interaction data: the recording key and credit snapshot ID when the heard source changes (and when it clears); for each settled retirement, taken before release, the frames handed to the voice player, underruns, `transfer_complete`, `decoder_complete`, `pcm_valid`, `audible_drain`, the bank's `failure`, whether the bank was released and the seconds from its first cancellation to release; voice connected and disconnected; and the shutdown outcome with the leases still held. A silence frame returned while the source's own lock is contended is not attributed to a track.

A voice connection lost outside the adapter is detected only when a command next runs: playback commands then report that the bot has not joined, and the lost player's tracks are retired at the next `/join` or `/leave`.

Configuration comes from environment variables (see `.env.example`). `SOBAFM_DISCORD_TOKEN` is required and is removed from the process environment when read, so child processes cannot inherit it; it is excluded from the configuration's representation, and configuration and client errors are logged without values or messages. `SOBAFM_CATALOG` and `SOBAFM_REVIEWS` must be set together; without them `/play` is not registered and a safe warning is logged. The entry point below reads this configuration and supplies the decoder-backed producer. Offline tests use faked disnake objects and make no token, gateway or voice contact; they do not establish voice behavior, timing or DAVE negotiation.

## Implemented entry point and deployment

`python -m sobafm` (`__main__.py`) runs in a fixed order. First a limit gate reads the cgroup v2 `memory.max`, `memory.swap.max` and `pids.max` that the process sees at `/sys/fs/cgroup` and refuses to start (exit 3) unless they are exactly ADR 0004's 90177536, 0 and 64. A missing, unreadable or different value refuses before any configuration, catalog, media, Discord or audio input is read. Then the adapter's `Config` reads the token, catalog snapshot and reviews from the environment, and `SOBAFM_MEDIA` names the media manifest, required exactly when a catalog is configured. A configuration error exits 2 without values. Finally the adapter's `run` starts the bot with a `MediaProducer` and handles SIGTERM through the adapter's shutdown.

The catalog snapshot has no audio location, so `media.py` reads a separate bounded operator manifest (at most 256,000 bytes and 1,000 entries, duplicate keys rejected). Each entry binds a recording key to a file relative to the manifest's directory that may not leave it, its exact size and SHA-256, and `max_pcm_bytes`, a whole number of frames up to four hours of PCM that becomes the decode's overrun bound. `MediaProducer` is the adapter's `Producer`: for each track it creates one `DecodeProducer` on the adapter's already-leased bank and runs start, finish and settlement on a worker thread. On cancellation it cancels the decode, keeps awaiting the worker through repeated cancellation, and raises only after the decoder process, its threads and descriptors have actually settled. A failed decode is logged by its failure kind and leaves the bank stopped, so the callback reports a failed switch and current audio continues. The adapter's retirement keeps the single lease, the original three-second clock from the bank's first cancellation, the failed-verdict count and retained debt unchanged.

The `Dockerfile` builds the locked runtime dependencies with uv 0.13.6 on the reviewed Python 3.14 base pinned by digest, then installs the reviewed mpg123 1.33.7 package set with its exact stable dependencies, including libopus, and runs as UID 1000. It contains no FFmpeg. `compose.yaml` defines one service with init, a read-only root, all capabilities dropped, no new privileges, memory and memory-plus-swap 90,177,536 bytes, 64 tasks, two CPUs, `unless-stopped`, a read-only `./data:/data` bind mount and two 1 MB log files. Offline tests check the gate against a fake cgroup root, configuration, the producer with a Python child in place of mpg123, and the deployment files statically. The image build, fit under the group limit, the multi-threaded parent's decoder launch and voice behavior are not yet qualified.

## Decisions to establish first

The opt-in [Night Owl validation evaluation](../evaluations/fma_validation/README.md)
connects that one-copy intake to sequential contained Mutagen inspection and a
fresh mpg123 streaming runner under maintained init. A closed source/image/input/
expiry packet and finite external Docker controller retain process, I/O, payload
and volume debt through actual release. Synthetic checks exercise this source;
fresh topology fit, native terminal-filter qualification and actual recording
validation remain separate pending execution gates. This adds no playback service
or catalog admission and does not alter the accepted resource ADR.

Its native admission uses bounded, original-clock process observations. One
limits scan per phase preserves the exact resource policy; the stopped scan
retains every admitted map row and its original-byte digest between complete
identity/inventory brackets. Selected map/limit facts, projections and receiver
copies remain charged separately from contained raw control scratch. This local
boundary does not close full runner/userspace accounting or release native work.

Environment preflight and resource counters now use that same bounded workspace.
Mount/security/limit input shares one cumulative transaction; only gate bits and
bounded immutable counter facts cross selection. Intake and decoder final
resource observations precede clean-settlement decisions, including actual
close and the original-clock check. Uncertain final observations retain debt;
the decoder makes one final attempt and performs no fresh resource read in its
terminal-report method. Selected resources reserve 16,384 bytes within each
affected original owner partition, separately from pins/maps and report copies.
The updated partial runner subtotal is 712,704 bytes; remaining payload closure
and native fit are still pending.

FMA diagnostic capture uses fixed 65,536-byte backing plus a 4,096-byte slab,
allocated after the native fork. A logical count distinguishes received bytes
from capacity; a sentinel read rejects overflow. Range-based grammar validation
hashes original views, retains bounded immutable facts and runs once at actual
stream/native completion. Terminal projection reuses those facts. Diagnostic
backing and aliases remain owned through reporting after producer settlement.
The existing 196,608-byte diagnostic reservation and partial 712,704-byte runner
subtotal remain unchanged. Canonical report construction now uses an exact
proved-size work buffer and immutable result, with newline included in the same
record limit. FMA helper writers retain one immutable record through borrowed
partial writes; metadata never emits a second terminal after possible submission.
Journal workers retain their record through actual write/fsync/error settlement; case-phase saves wait under the original case clock rather than a fixed time, while cleanup saves keep their short bound.
The construction ceiling of work + result + one prior failed record is 196,608
bytes inside existing report reservations. Metadata explicitly assigns its old
serialization allowance and fixed margin to that ceiling; no partition grows.
Receiver JSON parsing is bounded by one whole-record decoded-payload gate
(`DECODED_BYTES = 2R + 1,024`) before materialization, and the evaluation README
records the phase ledger: intake peaks at 509,474 of 786,432 and the controller
at 462,726 of 524,288. Those bounds are conditional on stated CPython 3.14
assumptions not yet measured in the evaluation image, which is retained debt.
Sender construction does not pay for receiver copies or establish native fit.

The finite FMA ingress owner now retains each original bounded stream, admits
closed producer shapes/domains before maintained JSON decoding, and caches each
complete record once. It keeps original prefix counts/hashes and rejects later
extra, changed or partial records. Cleanup envelopes remain independent of
successful detail validation, preserving earlier clocks through rejection.
Selected Docker projections separate cleanup identity/exit facts from initial
configuration; obsolete setup and completed exception payloads end before
cleanup. Helper retirement publishes history once, while active process/FD and
journal operation debt remain owned. Unicode paths, Latin-1 notices and broader
qualification timestamps retain distinct supported domains under the decoded-payload
gate. The receiver phase ledger charges conversion peaks with retained
packet/report/receipt facts, conditional on its CPython assumptions; there is no
full native-fit claim. Public intake and
operator packet/recovery loaders remain outside this receiver change.

Receive stdout now has exact fixed R backing with an independent used extent;
opaque helper/management stderr retains prefix facts only. Maintained strict
buffer decoding removes the explicit whole-record conversion copy, without
claiming no internal codec/JSON copies. The existing container-name grammar is
bounded before materialization; real Unicode paths remain supported. Inert
commands register before launch/allocation, original deadlines gate subsequent
work, and exported views prevent backing retirement. Receipt publication is
idempotent while partial owners stay registered. The evaluation's visible phase
ledger replaces the old receive subtotal; J/F closure is conditional on the
CPython assumptions recorded there.

The [accepted decoder-PTY amendment](decisions/0003-decoder-pty-exclusion.md)
excludes only kernel PTY backing alongside kernel pipe/socket storage. It keeps
the stock decoder, all userspace partitions and the 98,979,840-byte total. A
subsequent pair requires actual userspace/process/FD settlement and all other
admission gates; prior excluded kernel objects may remain unreclaimed, with no
physical/kernel backlog bound. Original cancellation clocks and userspace debt
remain. This contract amendment does not qualify runtime fit or adopt the
proposed application architecture.

The explicit owned-synthetic qualification entries reuse this same pipeline with
three finite case identities. `LocalRequest` shares ownership operations between
the existing FMA request and a separate synthetic request; FMA v1/v2 serialized
records and expiry semantics are unchanged. Synthetic request/ledger/report
namespaces cannot enter the production parser. Tagged-input and cancellation
cases bind the same exact derivative from the
[reviewed byte-integrity derivation](../evaluations/fma_validation/results/2026-11-02-owned-tagged-derivation.yaml).
Their parser/decoder qualification still requires separate reviewed execution.
Failure terminal envelopes preserve role/case-bound cleanup timing independently
of successful decode acceptance and retain actual resource debt through join.

- Source-specific acquisition/storage implementations for the accepted three-source pool, with complete recording credits and access evidence.
- The simplest viable non-root Docker resource model under the approved capacity definition: accepted for amd64 in [ADR 0004](decisions/0004-amd64-whole-service-adoption.md); application qualification remains open and native arm64 is on hold ([#60](https://github.com/slackysoba/sobafm/issues/60)).
- Supported dependencies and decoder distribution without a bespoke supply system by default. The Discord voice library is selected and locked: disnake with DAVE through dave.py ([ADR 0005](decisions/0005-discord-voice-library.md)). `discord_adapter.py` uses it. The decoder distribution remains open.

Record those decisions with evidence. Update this document to describe actual modules and data flow as they are implemented; do not pre-create a framework from this outline.

See [requirements](requirements.md) for product behavior and [engineering notes](engineering-notes.md) for implementation constraints and regression scenarios.
