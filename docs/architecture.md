# Architecture

The offline catalog selector, the pure station-fact and credit-rendering logic, and the Discord adapter for the first playback slice are implemented. The adapter has no decoder-backed producer yet and has not been tested live. The Docker resource model for the first amd64 playback slice is [accepted](decisions/0004-amd64-whole-service-adoption.md) but not yet implemented or qualified. The rest of the station, playback and deployment architecture below remains proposed and unqualified.

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

Each bank is a single-producer, single-consumer ring. The producer borrows a contiguous free region (suitable for a pipe `readinto`), fills it outside the bank lock and commits the filled prefix. It must settle every borrow with `commit_write`, using `commit_write(0)` on error, even if it already released the region view. `write()` is a copying convenience. The voice callback's `read_frame()` takes the bank lock without waiting and copies at most one 3,840-byte frame. It returns `None` when no frame is available now (not ready, underrun or a brief contended operation) and `b""` once no frame will follow. Frames are readable only after readiness: 1,152,000 buffered bytes (six seconds), or the producer's verified end of a shorter non-empty, frame-aligned stream. An empty stream or a partial final frame is a recorded failure; partial bytes are never emitted or padded with silence.

Transfer completion and decoder completion are recorded by their owners. Valid PCM comes from the verified end-of-PCM check. Audible drain means every whole frame of an ended, admitted stream has been handed to the voice callback. Each fact is tracked separately and none implies another. The module has no Discord library, decoder process, station logic, threads or dependency. The completion policy, producer wake-up, cleanup deadline owner and voice integration belong to later slices. The 3,840-byte frame copy returned to the callback is application audio copy payload under the separate copy reservation; this module does not account for it. Offline tests do not qualify voice timing, Linux containment or useful fit.

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

## Implemented station facts and credits

`station.py` keeps one server's desired program, prepared audio, claimed switch and heard source as separate facts in a single-owner `Station`. Each accepted request gets an increasing generation and reports the unheard program whose work the caller must cancel. Preparation results, failures and switch reports carry their program or claimed audio; anything for a superseded program is returned as stale and changes nothing. Prepared audio is recorded only after its credits render. The player claims ready audio and then reports an actual switch or failure; only the switch changes the heard source. A failed, rejected or cancelled replacement reverts the desired program to the one still heard and leaves its credits unchanged. Stop ends the desired program; the heard source clears only when the player reports silence. The module holds no audio, tasks, threads, persistence or clock, and has no member queue, skip, replacement scheduling, generated mode or settings.

`credits.py` renders a recording's existing `catalog.py` credit snapshot: title, creators, source backlink, license and every notice. Provider text has bidirectional controls stripped, other control and line/paragraph separators replaced by spaces, and Discord markdown, mention, emoji and autolink characters backslash-escaped. Links are rendered only after the catalog's credential-free HTTPS validation plus an RFC 3986 character check, inside angle brackets. Pages are filled continuously up to 1,968 UTF-16 code units plus a footer, within Discord's 2,000-unit message limit; credits needing more than 16 pages raise `CreditRejected`, and the station treats that as a rejected candidate rather than truncating notices. Each page states that mentions are disallowed and previews suppressed for the Discord layer. `CreditViews` retains at most 256 caller-bound views, each expiring a fixed time after opening (600 seconds by default, at most 900) under injected monotonic time; when full it evicts expired then oldest views. Wrong-caller, unknown/expired and out-of-range requests receive refusals without page content.

## Implemented Discord adapter

`discord_adapter.py` registers `/join`, `/play request:<text>` (1 to 200 characters), `/now`, `/stop` and `/leave` with an `InteractionBot` that requests exactly the Guilds and Voice States intents. Commands are registered for the server context only and also refuse a missing server at runtime. `/join` and `/leave` carry Manage Server as their default member permission, so administrators customize access through Discord command permissions rather than a second role system. `/play` and `/stop` require the caller to be in the bot's voice channel; `/stop` also accepts a caller with Manage Server from elsewhere. `/now` has no extra requirement. Without a joined voice connection, `/play` and `/stop` explain that a manager must use `/join`. `/now` answers at once; the other commands defer before any voice, selection or cleanup wait, all ephemerally and with mentions disabled.

`/play` interprets the request with `catalog_prompt.py` and selects one match from the configured prepared snapshot with `catalog.py`. No query or no match changes nothing and substitutes nothing. A match becomes a new `station.py` program; its credits render before it is prepared. The adapter then claims the audio, leases a `pcm.py` bank from `GLOBAL_PCM_BANKS` and starts the injected `Producer` on the event loop. A refused lease reports a failed switch, leaving current audio heard. Only one unheard track is pending per server: a newer request displaces it, reports its switch as failed and joins its retirement (bounded by five seconds) before leasing again. Responses say the request is accepted and preparing, never that it is audible.

`StationSource` is the voice callback. It takes its lock without waiting and copies at most one frame from the pending or current bank. It switches when the pending bank yields its first frame, which `pcm.py` allows only after readiness. Before that, and during an underrun, it returns a silent frame so the player keeps its timing; it never pads a partial final frame. Switch, failed, drained and retired reports go to the event loop with `call_soon_threadsafe`. Only the switched report changes the heard source, so `/now` keeps the heard title and full credits until then and states when a newer request is starting. `/now` sends a status line followed by every rendered credit page as private follow-ups, with previews suppressed. A drained track that is still the desired program ends it, because continuous selection is outside this slice. When nothing is current or pending, the source returns end of stream and the player stops; the next claim restarts it.

Every retired, displaced, failed or stopped track keeps its bank and producer task in one retirement task: cancel the bank, cancel and join the producer, then release. A bank that cannot yet be released stays leased in the adapter's debt list. `/stop` removes all audio from the callback, reports silence and joins retirements for up to five seconds; ownership continues after that. `/leave` also disconnects. SIGTERM and SIGINT set a stop event: `run` stops and disconnects every server, waits for all retirements, retries release of retained banks, closes the client and exits 0 only when every bank was actually released.

Configuration comes from environment variables (see `.env.example`). `SOBAFM_DISCORD_TOKEN` is required and is removed from the process environment when read, so child processes cannot inherit it; it is excluded from the configuration's representation, and configuration and client errors are logged without values or messages. `SOBAFM_CATALOG` and `SOBAFM_REVIEWS` must be set together; without them `/play` is not registered and a safe warning is logged. `main(producer)` is the entry point for a deployment that supplies the decoder-backed producer. Offline tests use faked disnake objects and make no token, gateway or voice contact; they do not establish voice behavior, timing or DAVE negotiation.

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
Receiver JSON parsing and Unicode/shape-dependent peaks remain unproved within
the intake/controller partitions. Sender construction does not pay for receiver
copies, close full payload accounting, or establish native fit.

The finite FMA ingress owner now retains each original bounded stream, admits
closed producer shapes/domains before maintained JSON decoding, and caches each
complete record once. It keeps original prefix counts/hashes and rejects later
extra, changed or partial records. Cleanup envelopes remain independent of
successful detail validation, preserving earlier clocks through rejection.
Selected Docker projections separate cleanup identity/exit facts from initial
configuration; obsolete setup and completed exception payloads end before
cleanup. Helper retirement publishes history once, while active process/FD and
journal operation debt remain owned. Unicode paths, Latin-1 notices and broader
qualification timestamps retain distinct supported domains. Receiver conversion
peaks and retained packet/report/receipt overlap still require an actual ledger;
there is no `J <= 2R`, fixed `F` or full native-fit claim. Public intake and
operator packet/recovery loaders remain outside this receiver change.

Receive stdout now has exact fixed R backing with an independent used extent;
opaque helper/management stderr retains prefix facts only. Maintained strict
buffer decoding removes the explicit whole-record conversion copy, without
claiming no internal codec/JSON copies. The existing container-name grammar is
bounded before materialization; real Unicode paths remain supported. Inert
commands register before launch/allocation, original deadlines gate subsequent
work, and exported views prevent backing retirement. Receipt publication is
idempotent while partial owners stay registered. The evaluation's visible phase
ledger replaces the old receive subtotal; complete J/F closure is still open.

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
