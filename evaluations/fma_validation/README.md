# Bounded Night Owl validation

Issue [#44](https://github.com/slackysoba/sobafm/issues/44) implements a concrete
consumer and host controller for the existing one-copy intake. **Source and
synthetic tests only:** no supplied-file parse, decode, image build, container,
volume provisioning, playback or admission result is established by this change.
Actual evaluation remains a separately released phase of the same issue.

The [topology proposal](https://github.com/slackysoba/sobafm/issues/44#issuecomment-6577397904)
and [independent acceptance](https://github.com/slackysoba/sobafm/issues/44#issuecomment-6577471943)
require maintained Docker init at PID 1, a separate Python intake owner at PID >1,
then sequential metadata and fresh single-thread decoder helpers. The owner
uses `Popen(pass_fds=...)` for the borrowed O_RDONLY descriptor, never Python
`fork()` from its threaded reader. Only the fresh decoder runner forks the native
decoder. Every admitted role binds PID/start time, executable inode, UID/GID,
cgroup and security settings. Only these exact roles are excluded from descendant
inventory. Python processes receive no blanket exception.

## Ownership and timing

The production entry accepts only the original supplied file. A closed packet binds its exact
8,054,458 bytes and known SHA-256, unadmitted catalog/credit snapshot, original
notices, acquisition evidence and absolute expiry. A new, persistent volume holds
one UID/GID 1000, mode 0700 slot and one mode 0600 owned payload. The same pending
inode is inspected and streamed, then released by `LocalIntake`; no metadata
helper opens a media pathname. Metadata process exit, output EOF and every owned
descriptor must settle before the decoder helper is created.

One fixed **210-second whole-case deadline** includes Docker management, intake,
metadata, native work and report drainage. Metadata has a 30-second limit; the
decoder retains 150 seconds wall time and 15 seconds to prepare six seconds
(1,152,000 bytes) of PCM. Its cap is 46,080,000 bytes of 48 kHz stereo s16 PCM,
counted and hashed incrementally in complete four-byte frames. The web page's
194-second duration is reported separately and is neither a wall-time limit nor
an independent exact PCM oracle.

The first failure, expiry or normal completion starts one **three-second cleanup
obligation**, conveyed across helpers as an original UTC timestamp and locally
bounded by monotonic time. Later phases only tighten it. The successful reader
is observed complete before intake cancellation adopts the decoder's earlier
clock. Unknown timing and missed deadlines remain sticky after eventual physical
release. A signal without a trusted cross-process timing record is unknown timing.
Retained threads, blocked closes, helpers, descendants, pipe writers and backing
remain owned after a failed deadline. No timeout proves their release.

A bounded decoder terminal envelope binds its case, runner identity, counters
and original clock independently of successful decoding. The consumer captures
it before rejecting nonzero exit, early write failure, cancellation or retirement,
including records received during the final drain. Missed and unknown timing
propagate to intake and the external controller without renewing the original
deadline. Missing, malformed, partial or cross-case records remain failed/unknown;
bounded output counts and hashes survive retirement. A receipt never releases a
still-owned descriptor or descendant. Rejection codes allow digits, preserving
the explicit `unsupported_id3_text_encoding` result.

Decoder admission and terminal records validate canonical ASCII JSON size,
escaping, types and structure **before** serialization. Each record reserves its
newline within 65,536 bytes; the helper's aggregate output cap still applies.
Admission serialization occurs while the verified native process is stopped,
before bank allocation or native resume. Native mapping/security evidence remains
in the record and its existing owner. Cancellation or expiry during preflight
prevents resume.

Charge the explicit serialized representations to the unchanged 786,432-byte
runner partition: at most 65,535 bytes each for JSON text/ASCII bytes during
encoding, then at most 65,535 plus 65,536 bytes when adding the newline. The
synchronous writer retains that immutable line across partial/interrupted writes
and uses memoryview slices without allocating payload tail copies. Cancellation
does not renew its original clock or certify a held write as released. These
local bounds do not establish the complete runner backing/copy maximum, encoder
internal allocation, PTY accounting/release or native topology fit; those remain
separate qualification gates. No PTY reservation subdivision is adopted.

### Process observation transactions

`ObservationWorkspace` is specific to this evaluation. The intake owner creates
one workspace with its original `CaseClock` before preflight and shares it with
the consumer and sequential `OwnedHelper` operations. The separate decoder
process owns its own workspace. A non-reentrant lease covers every read, parse,
executable lookup, directory iterator and actual close. Cancellation only records
intent on the original clock; it cannot borrow or replace an active workspace.

Each capture, verification, inventory or stopped-native observation is one
transaction with a cumulative **65,536-byte input policy**. All bracket reads and
returned executable/directory-name bytes count; individual files do not reset
the counter. This is an input policy, not a universal proc-size or allocation
maximum. Reads use an 8,192-byte fixed arena: 2,048 bytes for input, 2,048 for
whole-record carry, 512 for line carry, and unused reserved backing. At a file
limit, one extra byte in existing storage distinguishes EOF from overflow.

The closed schemas reject unsupported layouts rather than accepting prefixes:

- `stat`: at most 2,048 bytes and 64 fields, at least the 52 current fields;
  preserve the complete record through the final `)`, including newline in the
  at-most-64-byte ASCII command name.
- `status`: at most 16,384 bytes, 128 nonempty rows and 512 bytes per row before
  newline; keys at most 64 bytes. Selected fields must occur exactly once.
  All four UID/GID values are 1000, NNP is 1, seccomp is 2 and CapEff is zero;
  parent and thread count must agree with `stat`.
- `cgroup`: at most 16 bytes and exactly `0::/` followed by newline.
  `cgroup.procs`: at most 64 distinct positive PIDs, ten digits each, complete
  newline-terminated rows, no duplicate or omitted pinned role.
- Directory scans admit at most 256 total entries and 64 numeric process IDs;
  extra task/FD entries fail before retention. Executable selection requires an
  absolute printable ASCII target of at most 64 bytes, plus device and inode.
  Capture brackets security/membership with start and executable identity checks.

First failure starts cleanup before close. Complete validated facts are staged;
they replace the prior committed snapshot only after all actual closes and a
final clock check. Failed or vanished observations mean **unknown inventory**,
never an empty set or cached success. An uncertain descriptor or iterator close
retains its handle, arena and staged facts, prevents another lease, and blocks
local release certification. Timeout never clears that debt. Cleanup observations
may establish later physical settlement but retain missed/unknown original timing.
Both inherited cancellation group lookups and the decoder's live/release checks
use this path. Historical evaluations retain their default observation behavior.

The approved metric distinguishes contained control scratch from selected
audio/IPC payload. Raw proc parsing, path construction, stat caches, libc directory
backing and C-stack scratch stay inside the **full 90,177,536-byte group cap**.
Maintained `os.readlink`, `os.stat` and `os.scandir` calls therefore do not require
a proposed 32,768-byte whole-libc bridge bound. No such bound is claimed or used
for admission. This supersedes that proposed prerequisite without changing the
approved metric, group cap, copy partitions or PTY decision gate.

Selected executable text, immutable pins, native map evidence and report copies
remain payload. Each workspace prepays **8,192 bytes within its existing owner's
copy partition** for selected pins through their actual lifetime. The current
callers have at most 16 simultaneous pin versions/aliases across retained roles,
two helper-history records, old/staged snapshots and bracket temporaries. Charging
64 executable bytes, one group byte and a conservative 32 bytes for each of six
bounded integer values per version, plus two 64-byte selected readlink extents,
is at most 4,240 bytes. Object/container/allocator headers stay in the full group
charge. Four staged pin slots are checked before capture; external aliases remain
charged after snapshot replacement. `record()` preserves immutable value aliases;
wire validation preserves the same eight keys and caps types/ranges before pin
construction. Controller and terminal callers still charge their received JSON.
Report projections, serialized text/bytes and receiver copies retain their
separate existing reservations; this allowance cannot cover native maps or audio.

### Native limits and complete maps

The FMA runner reads native limits once before exec and once while stopped. Each
scan admits at most 16,384 bytes, 64 rows and 256 bytes per row. DATA=12,000,000,
STACK=524,288, AS=12,524,288 and CORE=0 must each occur exactly once, with equal
soft/hard values and exact `bytes` units. Decimal input is bounded before
conversion; missing, duplicate, truncated, overflowing or changed values fail.

The stopped observation includes complete identity/security/thread/inventory
checks, one limits scan, maps, and a second complete identity/inventory bracket
in **one original-clock transaction**. Both brackets, executable/directory bytes,
file reads and overflow probes share 65,536 input bytes. Component maxima cannot
all coexist. The preparation deadline is checked throughout, after actual close,
and before commit; existing admission serialization, PCM allocation and resume
ordering remains in force. Historical evaluations keep their default stopped
gate behavior; only FMA supplies the combined observation callback.

Maps admit at most 65,536 input bytes, 64 complete rows and 512 bytes per row
(the row/width policy also implies a tighter 32,832-byte file maximum). Address
and offset hex fields have at most 16 digits, device halves eight, and inode
decimals 20, all bounded before conversion. Only canonical reviewed ELF paths,
anonymous/heap/stack and the existing kernel-special paths are selected. Every
row retains its path, permissions and extent; row 65 fails before retention.
Private mappings, no writable-executable pages, ordered positive page-aligned
extents, the complete ELF set, anonymous/special restrictions and the ordinary
AS ceiling remain mandatory. SHA-256 consumes the **original read views**,
including whitespace; it never hashes a canonicalized or truncated projection.
Special totals/exclusion text and `sampled_pcm_proof=false` are unchanged.

`native_records.py` defines immutable selected facts and projections that alias
their values. The closed source packet includes this policy module; missing or
changed bindings fail before journal/controller resource creation for both FMA
and owned qualification. The decoder workspace prepays **32,768 bytes within the existing
786,432-byte runner partition**, separately from the 8,192-byte pin reservation.
A conservative version costs at most `64 * (48 + 4 + 32 + 20) + 1024 = 7680`
payload bytes: each row charges a 48-byte ASCII path, four permissions bytes,
32 bytes for its bounded integer and 20 bytes of keys, even when backing aliases.
The final 1,024 bytes cover limits/keys, totals, digest and fixed scope fields.
Four such versions fit: prior committed/external evidence, new staged/partial
evidence and projection/failure overlap. Actual callers make one native admission
attempt; this is not permission to retain an unbounded history. Container/object
headers remain in G. Selected facts, partial rows and external immutable aliases
remain charged during held reads/closes, failed reports and snapshot replacement.

The known simultaneous runner payload subtotal is 696,320 bytes: selected native
facts 32,768; pins 8,192; input bytes plus input buffer 131,072; two existing PCM
scratch chunks 131,072; three diagnostic extents 196,608; and three report extents
196,608 (encoding pair plus a conservatively retained line). The PCM bank stays
in its separate P reservation. This subtotal deliberately is **not full runner
closure**: diagnostic parsing/string splits, encoder internals, other retained
report evidence and asynchronous PTY backing still require their own complete
accounting. The difference is not an unallocated allowance or execution release.

Receiver copies coexist with the sender: the intake owner's existing partition
charges its stdout buffer, raw copy, complete-prefix copy, split lines, JSON
decoding, newly parsed values and prior retained record at a conservative seven
65,536-byte extents, plus 32,768 selected native bytes (491,520 total for this
native receive overlap). Other intake-owner payload remains separate pending work.
No receiver copy is hidden in the sender's alias accounting. Tests preserve every
selected record through serialization/receive and exercise old/staged evidence,
report rejection before bank allocation/resume and original cleanup deadlines.

Counters, mountinfo and the preliminary legacy environment probe remain later
capacity work. No full runner envelope, native fit, Linux containment or release
qualification follows from these deterministic tests. The runner remains
786,432 bytes and the total remains 98,979,840 bytes, with no residual pool.

The outer controller owns bounded Docker command launch, pipes, output and actual
child exit. It journals intent before dispatch and records the full exact container
ID before start. It verifies image, ownership labels, mounts, limits and entrypoint,
then observes stopped container state/PID zero separately from the owner report.
It never uses `--rm`, auto-restarts, deletes volumes or recopies after failure.
Command creation/close stuck in the kernel can keep a retained worker alive after
the finite verdict; durable debt survives controller loss or OOM. Host command
outputs are capped, and at most two unsettled management commands are retained.

## Metadata and native diagnostics

[Dependency review and notices](NOTICE.md) document the exact Mutagen artifacts.
Before parsing, the helper applies AS 67,108,864 bytes, DATA 50,331,648 bytes, ten
CPU seconds, zero core/file output and a 30-second alarm. The borrowed file is
regular, single-link and read-only. A framing pass caps the complete ID3v2 tag at
1,048,576 bytes, 512 frames, 512 seeks and cumulative reads at three file lengths.
The supported subset is **ID3v1 and fixed-framing ID3v2.3**, with a maximum
**524,288-byte** v2 tag. v2.2/v2.4 are rejected before parsing; in particular,
Mutagen's v2.4 size-repair heuristic may otherwise select different boundaries
from preflight. Nested CHAP/CTOC, COMR commercial notices, duplicate frame IDs,
compression, encryption, unsynchronization and extended headers also fail
explicitly before parsing. No recursive parser or silent repair is added.

The maintained `known_frames` API admits only T/W frames and COMM, USER, IPLS,
OWNE to frame-specific parsing. Each admitted frame payload is at most **4,096
bytes**, and all such source payloads together are at most **8,192 bytes**,
checked before decoding or HashKey construction. Other frames stay opaque, with
explicit ID/hash/length and omission evidence; artwork/lyrics are not decoded or
dumped. Unrecognized textual-looking IDs are unresolved failures. Raw framed
IDs must agree with the returned known and opaque occurrences, so a known
malformed notice silently dropped by Mutagen cannot produce a successful report.
Duplicate IDs are rejected even when different discriminators might be valid;
this conservative subset avoids losing source evidence during consolidation.

Every admitted frame with an encoding byte (T frames, WXXX, COMM, USER, IPLS
and OWNE) must use **encoding 0, Latin-1**. UTF-16, UTF-16BE, UTF-8 and unknown
encodings fail with `unsupported_id3_text_encoding` before library entry,
including otherwise valid text. Ordinary W URL frames are already Latin-1.
This excludes Mutagen's Unicode repair path: repeated missing-BOM UTF-16 values
can retain exception/traceback cycles and shrinking input suffixes before a
later projection-count rejection. Post-parse garbage collection would not bound
that peak. Latin-1 decoding is total and does not create those repair exceptions.

Reports project explicit textual fields and use only a frame ID as their safe
key. No Mutagen HashKey is exported. COMR remains an explicit unsupported notice,
including its terms and optional logo, rather than exposing its serialized-frame
key or silently losing a term. ID3v1 and v2.3 notices stay separate with conflicts,
URLs and contributors. Bounded rejection codes remain visible to the owner;
parser exception text is never returned. No URLs are followed and
`credits_complete` remains false. The actual recording's tag version, size and
contents are unknown; an unsupported case needs a reviewed alternative, never
truncation or increased capacity.

The pinned mpg123 CLI remains noisy enough to reject warnings, resynchronization,
format changes and malformed completion. `--no-frankenstein` is deliberately
absent: its advertised-frame-count early stop would undermine complete input.
Quiet mode also cannot establish the required diagnostics. Only stderr is a PTY,
with raw slave settings/readback, fixed geometry, no controlling terminal,
no echo/canonical processing/signals/flow control/OPOST, and no packet mode or
flush. Its bidirectional nonblocking master is used solely for reads. stdin and
PCM stay ordinary pipes. C locale, no `--utf8`, `--no-control`, `--no-visual`,
`--skip-id3v2`, `--no-icy-meta` and `--long-tag` select the reviewed terminal path.
The exact six labeled ID3v1 lines may contain printable ASCII only; their values
cannot become diagnostics. Unknown, repeated, misplaced or post-completion lines
fail. Linux PTY EIO means drained peer closure only; EAGAIN is not EOF. Actual
native exit, every duplicate writer/handle, EOF, complete input count/hash,
zero exit, aligned PCM and absence of OOM/debt remain separate gates.

Primary pinned source supporting this choice is mpg123 1.33.7
[mpg123.c](https://scm.orgis.org/mpg123/tags/1.33.7/src/mpg123.c),
[metaprint.c](https://scm.orgis.org/mpg123/tags/1.33.7/src/metaprint.c),
[local.c](https://scm.orgis.org/mpg123/tags/1.33.7/src/local.c),
[term_posix.c](https://scm.orgis.org/mpg123/tags/1.33.7/src/term_posix.c) and
[parse.c](https://scm.orgis.org/mpg123/tags/1.33.7/src/libmpg123/parse.c).
Python-only PTY tests establish channel behavior; they do not qualify the native
terminal filter. Frozen synthetic native tag/encoding/ordering/cancellation cases
must pass under the exact later image before actual source intake.

## Capacity and execution release

### Closed owned-synthetic qualification

The [reviewed next slice](https://github.com/slackysoba/sobafm/issues/44#issuecomment-6582979315)
uses the same controller, `LocalIntake`, consumer, metadata helper and fresh
`StreamRun`. The explicit `qualification_controller` / `qualification_owner`
entries accept only the `sobafm-owned-qualification-case-1` packet. Production
entries still reject synthetic requests. There is no command, decoder, hash or
arbitrary case override. One frozen `CaseBinding` follows the request through
every boundary; helper byte limits come from that binding.

`LocalRequest` is the small ownership interface for exactly two callers:
`IntakeRequest` and `OwnedQualificationRequest`. FMA v1/v2 request fingerprints,
ledger serialization, original acquisition evidence and expiry retain their
existing semantics. Synthetic requests, slot ledgers and reports use a separate
namespace and explicit purpose/case; their finite creation/expiry window cannot
be renewed during recovery. This does not create a general intake framework.

The [recipe/oracle manifest](qualification-recipes.json) freezes source-generated
components and the exact observed owned derivative. These cases run serially
after separate execution release:

| Case | Input and expected observation | Current binding |
| --- | --- | --- |
| `tagged-full` | 524,288-byte v2.3 tag + unchanged 1,920,768-byte owned MPEG + 128-byte adversarial v1 tag; prior base's 23,040,000-byte PCM oracle | Observed derivative hash frozen; native qualification pending |
| `metadata-rejection` | 4,113 generated bytes; TIT2 encoding 1 with 1,023 missing-BOM values; `unsupported_id3_text_encoding`, no decoder | Exact synthetic hash frozen |
| `first-write-cancel` | Tagged input; real cancel immediately after native admission and first completed owner-to-helper write; no sleeps/retry | Same observed derivative hash frozen; cancellation qualification pending |

The tagged recipe exercises 4,096 bytes per decoded frame, 8,192 bytes total and
an opaque APIC frame. One separately reviewed derivation produced exactly
2,445,184 bytes, SHA-256
`2a589e0025dbc09b7fae0f5ce45a6b00e75fe27d0c5b9e3474d5f526ef914480`.
The [sanitized derivation evidence](results/2026-11-02-owned-tagged-derivation.yaml)
records matching base pre-hash/copy hash, matching output write/read hashes after
fsync, stable identities, final descriptor release and externally observed timely
exit 0. The earlier host admission failure and its corrected cause remain
recorded; exactly one derivation process ran. No media is committed or regenerated
by ordinary tests. Null, base-file or supplied-recording hash substitutions fail
closed case binding. The manifest retains the prior base's PCM oracle as the
expected derivative result, without claiming a new parser, PCM or native result.

Expected rejection/cancellation keeps pipeline `passed: false`. A separate
`qualification_passed` requires the exact expected observation, valid original
timing, all owner/reader/helper release evidence, no OOM/swap, and external
container exit. Offline Python-child tests establish control flow only. They
do not release another fixture derivation, image/build/volume work, PTY accounting,
native qualification, supplied-file access or catalog admission.

Before another serial case, separately review and release explicit reconciliation
of the same slot. Payload release retains `state.json` and `owner.lock`; the
controller records `slot_reusable: false`, and fresh intake rejects retained
state. Preserve bounded ledgers, original request/expiry/cleanup clocks and failed
verdicts. Verify slot/file identities, exclusive ownership and actual reader,
helper, container and CLI release before authorizing scoped retirement of
completed state. This evaluation has no automatic slot-reuse procedure; another
slot, ledger replacement or expiry renewal cannot bypass unresolved debt.

### Unchanged reservations and later release

The unchanged capacity equation is **90,177,536 group + 4,608,000 PCM + 4,194,304
aggregate copies = 98,979,840 bytes**. The single copy reservation is partitioned:

| Owner | Reserved copy bytes |
| --- | ---: |
| Intake owner | 786,432 |
| Metadata helper | 2,097,152 |
| Decoder runner | 786,432 |
| Outer controller | 524,288 |

These reservations include simultaneous input, parser byte buffers, projected
reports, serialization and IPC copies. Parser object/allocator overhead remains
in the whole-group charge. Approved kernel pipe/socket-buffer exclusions do not
establish a PTY-backing exclusion. Concrete PTY backing accounting and containment
remain an unreleased exact-target qualification gate within the unchanged
98,979,840-byte capacity and existing approved exclusions. The code bounds I/O and
report buffers, but exact parser allocation/copy overlap and the new process
topology require fresh fit evidence. Old native proofs qualify only their exact
unchanged source/image/target cases, not this topology.

The metadata reservation explicitly includes decoded Unicode backing and keys,
not just raw bytes. Its conservative source-bound accounting is:

| Simultaneous payload allowance | Bytes |
| --- | ---: |
| Original tag, shrinking slices, current/retained opaque frames: `3 * 524288` | 1,572,864 |
| Admitted raw/text/Unicode/key transients: `24 * 8192` | 196,608 |
| Projected fields, framing IDs, opaque hashes and v1 evidence | 131,072 |
| Serialized string plus bytes, or bytes plus the current write slice | 131,072 |
| Fixed margin | 65,536 |
| Total metadata partition | 2,097,152 |

For pinned v2.3 `read_frames`, original input plus current/replacement slices and
accumulated opaque payload is bounded by three raw tag extents. The much smaller
admitted-frame budget separately covers raw text slices, conservatively at most
four-byte Unicode backing per source byte, retained decoded values and textual
uniqueness keys. The 24-times allowance covers six four-byte extents; Unicode
repair and its exception-retained suffixes, COMR serialization, recursion,
alternate framing and duplicate merge paths are excluded before parsing.
Supported Latin-1 multivalue decoding can still fail the projection-count bound;
its ordinary input slices do not accumulate in repair traceback cycles.
Projection reuses these bounded text values, exports
no library key and limits frames/hashes separately. Only object headers, container
overhead and allocator overhead remain solely in the whole-group charge.

Before `json.dumps` can allocate an escaped result, an allocation-light traversal
checks exact canonical ASCII JSON size, builtin value types, depth, node count,
integer width and finite numbers. It counts controls, DEL, surrogates and non-BMP
escapes without allocating their expansion; an iterencoder alone would still
allow an oversized single string chunk. Serialization is then at most 64 KiB
each for the string and bytes. The selected notice projection has a 32 KiB
encoded bound. These gates apply to rejected inputs too: near-limit wide text
and cumulative selected payloads fail before the library, while oversized
escaped reports fail before materialization. Source tracing and synthetic
counterexamples support review of these bounds; they are not measured whole-group
fit or actual-recording qualification. Independent exact-head review remains required.

Before any build/provision/native work, obtain a fresh development-storage
allocation and independent exact packet review. Prepare a small explicit build
context containing only the [Dockerfile](Dockerfile), two hash-verified Mutagen
artifacts, COPYING and NOTICE.md. No media, environment files or checkout caches
belong in that context. Verify the pinned base's interpreter/uv paths before
building. Supply `REVIEWED_BASE` only after resolving that build reference to the
exact local base image ID recorded in the Dockerfile; an image configuration ID
is not a registry manifest digest. The recipe is unbuilt and its executable paths
remain preflight requirements. Keep all native binaries and mapped ELF identities unchanged. Retain
the new image ID and target, dependency/license evidence and init/interpreter
executable hashes. No build or volume provisioning command runs in ordinary CI.

Before the closed owned-synthetic qualification cases, separately review and
release one new issue-labeled persistent volume and its bounded slot preflight.
Prepare `/slot` as UID/GID 1000 mode 0700, then perform a small
owned-synthetic mount/permissions/inode/close/cleanup preflight under the exact
future mounts and non-root settings. No actual recording read belongs in that
preflight. Record volume name, creation time, mountpoint hash and namespace slot
device/inode; these observed identities and the receipt hash are prerequisites
for each reviewed qualification packet. Before any owned-audio qualification,
require accepted exact-image PTY backing/accounting and a successful bounded
no-media PTY preflight. Before actual Night Owl intake,
separately review exact-image PTY backing/accounting, fresh init -> owner ->
sequential-helper topology fit and native terminal-filter qualification evidence.
No inherited real-file input is transported through Docker CLI descriptor numbers.

`packet.py` defines a closed schema, including the complete fixed runtime source
file/hash set, source commit/tree, exact image ID/amd64 target, interpreter and
init hashes, original v2 request, volume and namespace slot identity, private
absolute source/checkout/packet mount paths, synthetic preflight receipt hash,
review reference and fixed whole-case end/expiry. It accepts no placeholder
packet, alternative candidate, mutable image tag or fresh expiry on recovery.
Keep packet paths and personal acquisition evidence private. Reviewers must bind
the actual packet digest and source/image/input/target/window; a URL field alone
does not authenticate maintainer approval. Do not generate an execution-ready
packet until all identities and the exact time window are known.

After that separate release, the one explicit invocation is:

```sh
uv run python -m evaluations.fma_validation.controller \
  --packet /reviewed/private/packet.json --journal /reviewed/private/new-ledger.json \
  --execute-reviewed-packet
```

The journal path must not exist. Existing or partial journals, unknown creation
outcomes, retained volume state and uncertain reader ownership block replacement.
`--inspect-retained` reads the original packet/journal without starting anything;
it always retains the reconciliation requirement. It cannot reset timing, infer
release from disappearance or delete a named payload. For controller-loss cases,
retain the exact container/volume and inspect their identities, original slot
ledger and all reader/helper/container release evidence. A reviewed recovery
procedure must establish actual release before scoped cleanup. This source slice
intentionally does not automate destructive reconciliation of uncertain state.

Technical decode, source/recording correspondence, embedded-notice review and
catalog admission are four distinct outcomes. This implementation performs no
catalog write, listening, provider or Discord activity. Full credits still need
trusted source-bound review, and actual evaluation acceptance stays open on #44.
