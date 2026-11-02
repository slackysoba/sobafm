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
components only. These cases run serially after separate execution release:

| Case | Input and expected observation | Current binding |
| --- | --- | --- |
| `tagged-full` | 524,288-byte v2.3 tag + unchanged 1,920,768-byte owned MPEG + 128-byte adversarial v1 tag; exact 23,040,000-byte PCM oracle | Derivative hash **unfrozen; execution rejected** |
| `metadata-rejection` | 4,113 generated bytes; TIT2 encoding 1 with 1,023 missing-BOM values; `unsupported_id3_text_encoding`, no decoder | Exact synthetic hash frozen |
| `first-write-cancel` | Tagged input; real cancel immediately after native admission and first completed owner-to-helper write; no sleeps/retry | Same derivative **unfrozen; execution rejected** |

The tagged recipe exercises 4,096 bytes per decoded frame, 8,192 bytes total and
an opaque APIC frame. Only component hashes and the rejection fixture hash were
computed here. No retained owned media was accessed or derivative created.
Freezing the tagged derivative requires separate reviewed derivation and exact
hashing; the base hash cannot substitute. Pending derivatives fail before helper
launch, descriptor inspection or native setup. The manifest pins the prior owned
PCM oracle but does not claim new native evidence.

Expected rejection/cancellation keeps pipeline `passed: false`. A separate
`qualification_passed` requires the exact expected observation, valid original
timing, all owner/reader/helper release evidence, no OOM/swap, and external
container exit. Offline Python-child tests establish control flow only. They
do not release fixture derivation, image/build/volume work, PTY accounting,
native qualification, supplied-file access or catalog admission.

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

Provision one new issue-labeled persistent volume under a separately reviewed
bounded procedure. Prepare `/slot` as UID/GID 1000 mode 0700, then perform a small
owned-synthetic mount/permissions/inode/close/cleanup preflight under the exact
future mounts and non-root settings. No actual recording read belongs in that
preflight. Record volume name, creation time, mountpoint hash and namespace slot
device/inode; bind the receipt hash in the execution packet. Release this only
after reviewing the init -> owner -> sequential-helper topology's fresh fit and
native terminal-filter evidence. No inherited real-file input is transported
through Docker CLI descriptor numbers.

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
