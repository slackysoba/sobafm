# Architecture

The offline catalog selector is implemented. The Docker resource model for the first amd64 playback slice is [accepted](decisions/0004-amd64-whole-service-adoption.md) but not yet implemented or qualified. The rest of the station, playback and deployment architecture below remains proposed and unqualified.

SobaFM has one station/player per server. Commands validate access and intent. A mode-specific interpreter produces a validated musical plan. Catalog selection or generated production prepares audio; the connected player settles what listeners actually hear. Settings and remembered channels persist in SQLite.

Catalog preparation happens ahead of playback: source admission, provenance, credits, curation and descriptors. Runtime selection ranks eligible candidates and retrieves audio according to source policy. Start small; add adapters or audio analysis only when useful.

## Implemented catalog boundary

`catalog.py` validates bounded prepared metadata, explicit queries and separate trusted admission reviews. Reviews bind complete credits and access readiness to an immutable recording digest. Unreviewed, changed, unavailable or incomplete recordings remain rejected. No provider or model approval field is accepted in recording metadata.

`catalog_prompt.py` translates a small, explicit phrase vocabulary into that query model. It reports unknown positive terms and refuses unsupported hard constraints rather than guessing. `catalog_demo.py` reads local JSON and a prompt, returning full match evidence, preserved credits, rejection reasons and coverage for each of Jamendo, ccMixter and FMA. It accepts reviewed real metadata as well as explicitly synthetic test fixtures.

`catalog_import.py` converts bounded saved Jamendo tracks responses, ccMixter info responses and explicit FMA operator manifests into the same validated snapshot schema. Source-specific field maps preserve identity, credit evidence and unknowns. A separate report records malformed rows, missing requested IDs and bounded source coverage; conflicting duplicate identities are quarantined together. Outputs contain no media URLs or generated admission decisions. The importer's draft metadata still needs complete-kind/access evidence and separate credit review before selection admission. See [saved metadata imports](catalog-evaluation.md#saved-source-metadata-imports).

Selection compares every supplied partition before applying the result limit. Strong supported preferences precede weaker evidence and duration; hard constraints filter first. Partition completeness describes only the dated prepared subset. The modules perform no provider calls, audio access, state changes or Discord operations. See the [selection contract and demo](catalog-evaluation.md#current-selection-contract) and [accepted decision](decisions/0002-prompt-catalog-selection.md).

The separate `ccmixter_proof.py` evaluation uses two closed typed recording definitions in `ccmixter_cases.py`: WILD WINDSTEPS (70993/file129564) and the complete Fruitless mix (70072/file125900). Each case can recheck its six fixed metadata responses and acquire one temporary candidate file only after explicit invocation with a reviewed, candidate-bound fingerprint manifest. Its versioned policy normalizes only a unique, bounded decimal value inside an exact hidden HTML anchor context; all other page bytes and the complete info JSON remain protected. Only the selected case's nominated info URLs additionally accept `text/plain`; byte-exact hashing, duplicate-key-rejecting JSON parsing and candidate validation remain mandatory. Receipts retain a bounded MIME type/subtype without parameters or other response headers. Raw response hashes stay separate from comparison fingerprints. Case, catalog digest, projection/context and complete manifest binding precede resource creation. Child requests carry the case identity; cross-case routes fail before HTTP. File selection is by unique ID; stems and Fruitless upstream Vox/Guitar files cannot become playback candidates. Its single request owner retains processes, counters and files across cancellation and failed cleanup; there are no automatic retries. It reuses the importer and catalog digest without granting admission. Decoding, embedded notices and complete-file validation remain pending. See the [finite source proof and execution boundary](../evaluations/catalog/source-proof.md); this is not a production provider or playback implementation.

The audio callback consumes prepared PCM without blocking. Producers and cleanup retain explicit ownership across cancellation. Capacity admission includes retained work and cleanup debt. Prove the chosen Linux enforcement configuration before depending on it throughout the application.

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
Journal workers retain their record through actual write/fsync/error settlement.
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
- Supported dependencies and decoder distribution without a bespoke supply system by default.

Record those decisions with evidence. Update this document to describe actual modules and data flow as they are implemented; do not pre-create a framework from this outline.

See [requirements](requirements.md) for product behavior and [engineering notes](engineering-notes.md) for implementation constraints and regression scenarios.
