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
bounded by monotonic time. Later phases only tighten it. A decoder phase expiry
noticed late, including readiness, setup-wait and stopped-native gates, starts
the obligation at the phase deadline rather than at its observation. The successful reader
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
prevents resume. Admission-report entry, successful return and exceptional
return observe the original preparation phase before failure or bank allocation;
a late report build cannot start a later cleanup clock.

The single traversal/escaping/count/fixed-backing implementation now lives in
production `sobafm.json_records`; evaluation adapters retain their strict policy
and `ProbeError` codes. Production never imports the evaluation.
`encode_report(value, limit=65536)` retains canonical no-newline bytes for hashes,
argv and journals. `newline=True` includes LF in that same limit. `json_size`
checks exact builtins, depth 12, 8,192 value nodes, 128-bit integers, finite floats
and the full escaped size before allocating backing. Fill repeats those checks,
uses indexed writes into the exactly proved bytearray and verifies the final
offset before `bytes(memoryview(...))`. There is no whole-document `dumps`,
iterencoder chunk, encoded text or extra newline copy. Controls, DEL, BMP,
non-BMP and lone-surrogate escapes, Unicode key sorting and maintained builtin
integer/float spellings preserve the prior stdlib JSON bytes, including negative
zero. Inputs must remain stable and caller-owned during both passes; the checks
do not promise detection of every same-size concurrent mutation.

For R = 65,536, construction owns work R + immutable result R = **2R**.
At most one prior failed immutable record adds R, for **196,608 bytes (3R)**.
Scalar-conversion and sorted-key scopes end before freeze; sort containers alias
the original strings. Source facts retain their separate payload charge.
The empty owner is constructed before backing allocation, which is inside its
`try/finally`; even a failed owner constructor cannot retain an unprotected arena.
Rejected work backing is cleared in `finally`, including when an exception is
retained. There is no recursive encoder closure cycle holding its arena. Before
metadata fallback, completed parser/build frames, exception arguments and
cause/context links are explicitly released, outside the next construction's
exception context. These properties are tested with garbage collection disabled.

The shared FMA `RecordWrite` retains one immutable record across partial/EINTR,
held, zero and failed writes, using borrowed memoryview tails. Submission is
sticky before the syscall: offset zero does not prove that nothing was sent.
Metadata chooses a bounded rejection only before success-record submission; any
confirmed prefix or uncertain first write fails without a second terminal.
Rejection construction/write failure cannot trigger another fallback. The failed
record remains owned through actual synchronous settlement. Journals likewise
use memoryview `pwrite` tails, retaining the record through worker, identity,
truncate/fsync and error handling; a live worker or error bars replacement.
A journal save begun during the case waits for its write under the case's
original `CaseClock`, not a fixed time (#76): until the case ends or cleanup
begins. One begun in cleanup keeps the unchanged 0.25-second bound, capped by
the original cleanup window, so a slow evidence write cannot spend the stop
budget. A write outliving its bound fails with `journal_operation_retained` and
keeps its worker and record. The controller and preflight share the journal's
clock.
Historical Linux probe emitters remain unchanged.

The runner's existing three-report reservation covers this component without
changing its 786,432-byte partition. Cancellation does not renew its original
clock or certify held operations as released. Complete retained-fact/receiver
accounting, PTY userspace settlement and native topology fit remain separate
gates. The [accepted narrow kernel exclusion](../../docs/decisions/0003-decoder-pty-exclusion.md)
does not adopt a PTY reservation subdivision or claim kernel reclamation.

### Bounded receive ownership

`ingress.py` admits only the finite FMA producer and selected Docker families
before calling the maintained JSON decoder. Each original stdout/stderr stream
has one aggregate **65,536-byte** prefix budget, including empty lines. Parsing
and retiring a record never refund this budget. A sentinel read detects excess;
receipts count and hash the original accepted prefix, without normalization or
re-encoding. Stdout uses one exact R-byte backing with a separate used extent;
empty and small responses still own R. Scans, views, hashes and sentinel budgets
use only that extent, never the unfilled tail. Opaque helper/management stderr
keeps only count/hash/nonempty/error/EOF facts for the first `min(received, R)`
bytes, including a chunk crossing the cap, then bounded discard/drain. Native
decoder diagnostic text and its reservation are unchanged. Completed records are
materialized once into at most one metadata/owner slot or two decoder slots.
Later chunks, extra records, changed cached prefixes and partial EOF remain
errors. Metadata retains its final-record-without-LF behavior; decoder and owner
terminals require LF. Empty lines remain permitted and charged.

Helpers are registered before backing allocation. Management commands are inert
until registered, then explicitly start their owned launch worker. Allocation
success and failure check original case/operation deadlines before process
launch; interruption during start retains the registered command. An attempted
start without both the launch target's completion signal and actual thread exit
remains uncertain debt, even if an early thread identity is already visible.
It never becomes an inert success or automatic retry. Held allocation/read/hash/close and actual
process/descriptor settlement keep their owner. A borrowed stdout view prevents
backing retirement; the unchanged backing, used extent and records remain
charged until the borrow ends. Both helper and management receipt publication
are idempotent across interrupted retirement.

Generated report channels require ASCII JSON/LF; selected Docker output requires
strict UTF-8. UTF-16/32 and BOM autodetection are not framed-channel contracts.
Whitespace, key order, JSON escapes and meaningful finite numeric spellings do
not need to be canonical. Escaped-equivalent duplicate keys are rejected. The
maintained `str(buffer, encoding, 'strict')` route borrows the original buffer for
complete documents and authenticated envelope spans. There is no explicit
whole-record `bytes(...)` intermediate or fallback. This does not prove that the
codec or JSON implementation makes no internal copy. The
index-based pass bounds depth, nodes, keys, collections, scalars and decoded
string domains before materialization, including unsupported/rejected fields.
Unknown or huge keys and allocation-amplifying unsupported wide values never
reach `json.loads`. Metadata preserves Latin-1 notices up to 4,096 characters,
string/list/nested-list variants with 128-item limits, v1/v2 conflicts and opaque
frame evidence. Resource deltas retain up to 32 unique, 32-character ASCII
alphanumeric/underscore keys and values through `2**63`, including empty failed
observations. Failure, null and absent optional producer states remain distinct.
Settled intake reports retain the producer's finite numeric monotonic
`released_at`; an unobserved release remains null.

The cleanup envelope is checked independently of successful detail validation.
A complete, role/case/pin-bound envelope can tighten the original clock even
when its detail is rejected. Invalid syntax cannot authenticate a new envelope;
earlier trusted timing remains, with sticky unknown/missed evidence. A malformed
or partial later suffix cannot renew or erase that clock. Admission graphs are
retired only after gate/identity checks, preserving case/runner/order facts.
Helper history publication is idempotent across interrupted retirement; the
active process, descriptor and raw owners remain until actual settlement.

Docker projects fields before transfer: cleanup keeps ID/name/image, exactly
three ownership labels and Running/Pid/Status/ExitCode/OOMKilled. Initial
configuration additionally keeps every existing security/resource/restart/log,
entrypoint and four-mount comparison. Bind mounts export Source, volume mounts
export Name; unrelated extensions are omitted. Engine security is the selected
builtin-seccomp predicate. Image and volume reports contain only compared fields.
The shared cleanup/configuration name field admits at most 43 ASCII characters,
matching the existing `/' + case_id` domain; the exact ownership comparison
still follows decoding. Escaped equivalents remain accepted without normalization.
Packet-bound paths and volume CreatedAt retain their existing 4,096-character
non-control Unicode domain and exact codepoints/UTF-8 hashing. Operator packet
and recovery loaders are unchanged. Qualification raw timestamps retain their
broader date/time domain, including a Unicode date/time separator and long
fractional spelling within the aggregate wire cap; normal intake's short
timestamp assumptions do not apply to them.

Setup response graphs end before attach; completed exception arguments and
frames and traceback links are released before ordinary cleanup outside the
active exception suite. A pending KeyboardInterrupt/SystemExit keeps its original
verdict and arguments until actual command drain, with completed setup frames
detached; a rejected drained record cannot sanitize that pending verdict. Finished
launch argv copies are dropped, while retained attach/cleanup commands and
journal record/worker/error debt remain owned. There are still at most two live
management owners and 20 operations; up to two older retained-command receipt
snapshots also remain relevant. Final controller serialization is unchanged.

The visible receive phases now have these separate payload terms. Here `R =
65,536`, `n <= R` is the used document/span size, `T` is decoded document text,
`P` is the new parsed graph, `I` is maintained conversion/decoder scratch and `F`
is already retained packet/report/receipt payload. Views alias fixed backing.

| Phase | Known visible backing, before unclosed `P`, `I` and `F` |
| --- | --- |
| Inert owner, before allocation | No receive backing yet; the admitted slot reserves its future R. A pending launch cannot release that reservation. |
| Active helper or one management command, including empty output | Exactly R stdout; no retained opaque stderr body. One executing pump retains at most one 4,096-byte `os.read` result; explicit `del` ends it before the next read. |
| Two retained management owners | 2R stdout, including a settled command whose borrowed view prevents retirement. The one controller thread pumps sequentially; a retained owner does not retain an earlier successful read result. |
| Whole FMA or selected ASCII cleanup document | Its R raw owner plus `T <= n`; cleanup may coexist with another R attach owner. Parsing adds `P + I`, with earlier records/envelopes in F. |
| Valid initial configuration/volume Unicode document | One R command owner plus `T <= 4n` conservatively. These setup responses precede attach. Parsed path/CreatedAt/Mountpoint values and codec scratch remain separate charges. |
| Lexical numeric conversion | One token of at most R bytes plus maintained numeric-conversion scratch, alongside all retained raw owners/F. This scope ends before whole-document text/JSON decoding. |
| Rejected detail with authenticated envelope | Original raw owner(s), earlier cached graphs and accumulated envelope values coexist with one current borrowed-span text and decoder scratch. No joined envelope wire or explicit span bytes copy. |
| Non-JSON create/stop response | R raw plus an immutable used-prefix result of at most R until raw retirement; subsequent caller text conversion still needs its own phase charge. |

Held read/hash exceptions retain their actual argument/view until the operation
ends and completed exception frames are cleared. They cannot be credited as
released merely because the ordinary pump deletes its local read result. Opaque
hash failure is sticky and cannot retry later bytes as a new accepted prefix.
The fixed stdout backing can retire only after actual producer/FD settlement and
all exports end; retirement never resizes or replaces backing beneath a borrow.

The former `8R - 12` rejected-name diagnosis is no longer an admitted decoding
path: non-ASCII cleanup names fail before materialization. Subtracting the removed
stderr/copy extents to call `6R - 12` a surviving family maximum would be wrong.
Valid Unicode setup families are tested separately and still widen text backing;
the table is a conservative phase ledger, not a family-maximum proof.

These component changes **do not prove `J(shape, wire) <= 2R`**, fixed retained
facts `F <= 3R`/`2R`, or full intake/controller fit. Prior records, selected facts,
parsed graphs, conversion scratch and rejected exceptions remain in the ledger.
Supported Unicode paths/timestamps can widen decoded backing. Normal packet raw
notice/evidence strings and the separately parsed request do not all alias;
qualification has broader timestamp/integer admission boundaries. Compact
`LocalIntake` construction has the separate phase bounds below; selected fact
subtotals do not pay for its source graphs or concurrent operations.
Pinned decoder-source binding/accounting and final controller serialization
remain separate open work.
No partition, dependency, residual pool or runtime qualification is added.

### Compact intake records and hashes

`LocalIntake` uses the same production traversal for its existing 4,096-byte
acquisition/notice gates, 16,384-byte compact report gate and state construction.
Canonical fingerprints and notice/evidence hashes stream sorted ASCII bytes
through one fixed `H = 4,096` buffer, using borrowed views for hash updates.
Ordering is explicit in every sink mode. There is no new fingerprint-output cap;
v1 still omits the schema field from its historical fingerprint, while v2 keeps
its exact wire. Integer identities above 128 bits, maintained builtin numeric
spellings, reachable nonfinite literals, tuple/list values and accepted string
data preserve intake compatibility. String subclasses use underlying codepoints
without added conversion/iteration hooks or a full-string normalization copy.
The evaluation's exact-builtin, finite, 128-bit policy is unchanged.

Let `L = 16,384` for state/report and `B = 4,096` for notice/evidence gates.
Source values and maintained conversion scratch are additional terms throughout:

| Compact operation phase | Visible construction payload |
| --- | --- |
| Size-only gate | No whole-document backing/result. Scalar spelling is still charged: conservatively at most 2L (or 2B for that gate). An impossible integer bit-length lower bound rejects before decimal conversion; the interpreter's own conversion failure remains effective. |
| State fill | Exact proved work backing at most L plus current scalar spelling at most 2L: conservative **3L = 49,152**. Sorted-key containers retain aliases. |
| State immutable freeze | Scalar/sort frames have ended: work at most L plus result at most L, **2L = 32,768**. |
| State persistence | One immutable record at most L, with short-lived borrowed write views. It remains owned through write, fsync, close, identity check, replace and parent fsync. |
| Canonical hash | H document bytes plus current scalar conversion/source facts. H is not a global bound on arbitrary directly constructed huge scalars, retained exceptions or whole-owner fit. |

State construction/rejection completes before temporary-file removal or open.
Neutral encoding failures cross the existing `IntakeError` workflow boundary
without retaining the completed failure as implicit context. Rejected arenas
and hash views release in `finally`; a completed persistence call drops its
immutable-record reference even when its traceback is retained. Held operations
retain their actual backing and views until return. Failed/uncertain file close,
temporary identity and worker/reader debt remain with the original owner and
clock; record retirement does not certify descriptor or slot release.

Buffers are per operation. A report/hash can overlap a worker's state operation,
so those phases and retained source/receipt graphs must be added, not assigned
one shared allowance. These bounds do not close J/F or the complete intake
786,432-byte reservation. The contained owner constructs `LocalIntake` directly,
joins its worker before the final report and uses the existing bounded evaluation
writer. The ordinary CLI still uses its insertion-order/spaced `json.dumps` and
`print(..., flush=True)` path. Its text/encoded/transport buffers, partial or
uncertain submission, flush/cancellation ownership and output error/terminal
behavior remain explicit follow-up work; compact gates do not bound that output.
Packet source closure now includes `src/sobafm/json_records.py`. Native topology,
decoder-source binding, recording expiry and #7 gates are unchanged; the
interpreter gate is described below.

### Interpreter facts

The receiver ledger's CPython-dependent assumptions hold only for the pinned
image's interpreter. Besides the packet's interpreter executable hash, the owner
preflight observes four facts once, before any environment observation, and
rejects unless all match ([#67](https://github.com/slackysoba/sobafm/issues/67)):
version `3.14.7` (a pre-release spelling such as `3.14.7rc1` does not match),
empty `sys.abiflags`, a non-debug build (no `sys.gettotalrefcount`) and a GIL
build (`Py_GIL_DISABLED` exactly 0; an absent value fails closed). Each fact has
its own rejection code. The owner report carries the same observation as a closed
`interpreter` object, and the controller rejects a report whose facts differ.

The frozen runtime-identity packet and a CPython producer lookup are not gates on
the first real-file evaluation: no requirement names them and no code reads their
output. The evaluation's CPython-dependent bounds stay explicitly conditional on
these facts, as retained debt that a later release claim can discharge if needed.
These checks run inside the reviewed image only; Linux tests here fake the
observation and do not qualify the image.

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
group cap or copy partitions. The separate [decoder kernel PTY exclusion](../../docs/decisions/0003-decoder-pty-exclusion.md)
does not expand the treatment of userspace payload as control scratch.

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

The known simultaneous runner payload subtotal is 712,704 bytes: selected native
facts 32,768; pins 8,192; selected resources 16,384 (detailed below);
input bytes plus input buffer 131,072; two existing PCM
scratch chunks 131,072; the original diagnostic reservation 196,608; and three report extents
196,608 (proved work/result pair plus a conservatively retained line). The PCM bank stays
in its separate P reservation. This subtotal deliberately is **not full runner
closure**: other retained userspace report evidence and receiver parsing still
require complete accounting. Decoder kernel PTY
backing is separately excluded under the accepted contract, without a reclamation
or backlog bound. The difference is not an unallocated allowance or execution release.

Receiver copies coexist with the sender. The earlier intake receive/resource
subtotal, seven R extents plus 32,768 selected native bytes and 16,384 selected
resources = 507,904, is a **preliminary ledger, not a proved receiver peak**.
In particular, assigning JSON decoding and newly parsed values one R extent
each does not establish their actual shape-dependent peak `J(shape, wire)`.
Rejected input and widening Unicode backing must be covered too. Intake's
786,432-byte and the controller's 524,288-byte partitions remain unclosed;
The fixed receive/parse-once phases above supersede that preliminary assignment.
Other intake-owner source/fact payload remains separately charged.
No receiver copy is hidden in the sender's alias accounting. Tests preserve every
selected record through serialization/receive and exercise old/staged evidence,
report rejection before bank allocation/resume and original cleanup deadlines.

The bounded environment/counter observations below replace the preliminary
legacy environment probe in FMA only. No full runner envelope, native fit, Linux containment or release
qualification follows from these deterministic tests. The runner remains
786,432 bytes and the total remains 98,979,840 bytes, with no residual pool.

### Environment, mount and resource observations

FMA environment preflight uses the same `ObservationWorkspace`, one arena and
one descriptor/lease. Self identity/security/cgroup, mountinfo, effective limits,
current memory/events/swap and the decoder's core route share one cumulative
**65,536-byte** transaction, including overflow probes and complete EOF. The
existing 8,192-byte arena's line region extends to `[4096:6144]`: mountinfo rows
use `[5120:6144]`, bytes previously described as reserved, while `[6144:8192]`
stays reserved. No allocation, partition or capacity changes, and no extra read
thread or nested transaction is introduced. Component maxima cannot all coexist.
The historical Linux probes are unchanged. Unused version/hash projections are
omitted, while authenticated packet source hashes, native/runtime identity,
amd64/topology, single-thread, 4096-byte page and slot/statvfs gates remain.

The preflight requires all self UID/GID values 1000, NNP=1, seccomp=2, CapEff=0 and
exact `0::/` membership. Mountinfo admits at most 256 complete rows, 2,048 bytes
per row (a measured Docker Desktop containerd-snapshotter root overlay row with 12
`lowerdir` entries is 1,353 bytes), 64 spans per row and an exact separator with three trailing fields.
Only required gate bits are retained. Destinations must be absolute and
unescaped/canonical; duplicate required destinations, ambiguous paths and
conflicting/duplicate `ro`/`rw` option tokens fail. `/sys/fs/cgroup` must be
read-only cgroup2. Intake additionally requires read-only `/source`,
`/input/original` and `/case/packet.json`, writable `/pending`, and no mount below
`/pending`. No whole mount table becomes selected payload.

Scalars are complete newline-terminated ASCII records of at most 64 bytes,
with at most 20 decimal digits and overflow-safe uint64 parsing. Required limits
remain memory.max=90,177,536, memory.swap.max=0, pids.max=64 and exactly two
numeric cpu.max tokens with quota twice a positive period. `max` and overflow
fail; all four limit files must be unwritable by the application. The decoder's
core route is exactly `core` plus newline. Preflight validates memory.current,
events and swap but discards those control values; the separate baseline remains
at the original point immediately before intake/native preparation.

Resource observations combine events and swap in one transaction. Events admit
at most 4,096 bytes, 32 unique keys of at most 32 ASCII alphanumeric/underscore
characters, 127 bytes per row, and uint64 values. `oom` and `oom_kill` are required;
other keys are retained. Baseline/final key sets must match, counts cannot
decrease, and deltas cannot exceed the existing terminal wire ceiling `2**63`.
Immutable `ResourceFacts` retain selected event pairs and swap; the existing
`memory_event_delta` dictionary and decimal `swap_current` string remain on wire.

The owner takes final resources after intake joins and before its clean/debt
decision. The decoder takes one final snapshot after child/I/O settlement and
before setting `released` or committing a clean terminal result; `finish()`
performs no fresh resource reads. Failure never triggers a fresh-snapshot retry.
Held operations keep the workspace lease, descriptor, arena and old/new selected
facts; uncertain close retains debt and prevents local release. The first error
starts cleanup before close, and original-clock checks continue through EOF,
parse, close and commit. Cleanup observations may settle later ownership without
erasing missed/unknown timing or a failed verdict.

Each affected workspace prepays **16,384 selected-resource bytes within its
original owner partition**, separately from pins, native records and report/IPC
copies. A conservative four-version allowance is `4 * 32 * (32 + 32) = 8192`
bytes for keys and bounded integers: retained baseline, prior committed evidence,
staged/partial final evidence and delta/projection overlap. Immutable aliases do
not create new payload backing, but this calculation charges them conservatively.
Another 1,024 bytes covers bounded selected key conversion, swap/scalar fields
and fixed projection keys. Actual callers retain only preflight, baseline and
one final attempt, not an unbounded resource history. Container/object/allocator
headers and discarded raw parsing remain in the full G charge.

Adding this reservation makes the known runner subtotal **712,704 bytes**
(696,320 + 16,384). The former intake receive/resource subtotal **507,904 bytes**
(491,520 + 16,384) used an unproved receiver assignment and is superseded by the
visible receive-phase ledger above. Report construction and receiver text/parsed copies
coexist under separate charges; outer received records remain charged too.
These are partial subtotals, not full runner, intake or controller closure.
Receiver parsing and other retained payload still need complete accounting and
exact-image native fit. No reservation
is expanded, and no new pool or residual allowance is created.

The outer controller owns bounded Docker command launch, pipes, output and actual
child exit. It journals intent before dispatch and records the full exact container
ID before start. It verifies image, ownership labels, mounts, limits and entrypoint,
then observes stopped container state/PID zero separately from the owner report.
It never uses `--rm`, auto-restarts, deletes volumes or recopies after failure.
Command creation/close stuck in the kernel can keep a retained worker alive after
the finite verdict; durable debt survives controller loss or OOM. Host command
outputs are capped, and at most two unsettled management commands are retained.

## Metadata and native diagnostics

FMA captures diagnostics into a **fixed 65,536-byte backing** with a separate
**4,096-byte read/overflow slab** and a logical received count. Both allocations
occur after the native fork and before banner reception; the child does not
inherit this backing. Reads use bounded views and `readv`, never bytearray growth.
At capacity the next read requests one sentinel byte: any extra byte fails,
retaining only the exact bounded prefix for failure count/hash. Unused capacity
is never received data or EOF evidence. Banner reception preserves exact bytes,
stdout silence, native exit and original preparation deadline gates. Zero/EIO
means diagnostic EOF; EAGAIN/EINTR does not. No channel outcome proves actual
descriptor, child, kernel PTY or payload release.

The validator scans indices and transient views over that backing. It does not
copy the complete suffix, split lines, copy tag values or join the tag block.
All six full labeled tag rows remain printable ASCII with no new small value
limit. Their hash consumes the original row views separated by exactly five LF
bytes, without a trailing LF. Raw SHA-256 consumes the received-prefix view.
Only a candidate format line of at most **34 bytes** enters the small regex and
ASCII conversion. Completion-minute digits are scanned without whole decimal
copying/conversion. Existing blank/order/repetition/completion behavior and an
optional final newline remain unchanged; source text never becomes control text.
Bytes-based `validate`/`check` callers retain the same report fields and hashes.

The runner validates once after actual input/output/error completion and native
exit, then retains immutable `DiagnosticFacts`. Terminal projection reuses those
facts; `finish()` neither copies nor reparses the diagnostics. Failure projection
contains only the bounded prefix count/hash, reusing validated hashes when
available. A required failure-prefix hash finishes before terminal cleanup timing
is projected, so a held hash cannot publish its earlier clock verdict. Held reads own their FD and slab
view; held validation/hash owns its borrowed view and blocks local producer
release. Failed/uncertain close retains the original descriptor/debt. Cancellation
uses the first clock, including after blocked operations return. Normal and
exceptional capture/validation returns adopt the original phase deadline before
failure/close. Late allocation, banner reap/select, or scan/hash exceptions retain
that original obligation and propagate the source exception; partial allocation
also remains owned. Live checks before, between and after the two allocations
stop expired/cancelled preparation before more allocation, identity observation
or descriptor close, including after successful allocation returns. The backing,
slab, cached facts and report aliases remain owned through report rejection or
write completion, even after producer settlement; EOF and `released=True` do not
refund this payload. The runner never clears/resizes diagnostic backing during
PCM retirement. FMA overrides capture, admission and finish paths; historical
Linux probe defaults remain unchanged.

The **existing 196,608-byte diagnostic reservation stays unchanged**. Its bounded
implementation target is **73,728 bytes**: backing 65,536 + slab 4,096 + selected
facts/format temporaries 4,096. A conservative facts calculation is
`2 * (4 * (34 + 64 + 64 + 86) + 32) + 34 + 4 * 34 + 2 * 32 + 512 = 2794`:
two current/staged fact versions cover a 34-character format, two 64-character
hashes, 86 characters of fixed projection keys and a bounded count, charging four
bytes per character; the remainder covers simultaneous small format bytes/text
and bounded scalar temporaries. The additional 512 bytes conservatively charge
eight 64-byte SHA-256 blocks for partial/final block and copied-context payload
overlap in the two digests; borrowed input views retain the original backing.
Failure projection's shorter keys/hash fit this
same envelope. Dictionary projections alias immutable values and the actual
caller caches one validation, not a history. Headers, indices and regex
bookkeeping remain in G; diagnostic/tag text and selected facts remain in C.
This does not subdivide/reallocate the reservation or make its unused portion
a residual pool. The known runner subtotal remains **712,704 bytes** within
786,432, explicitly partial; bounded canonical construction fits its existing
three-report reservation, while owner/controller receiver parsing and retained
fact overlap remain pending. G/P/C and their 98,979,840-byte total are unchanged.

Deterministic synthetic regressions cover near-cap six-tag input, line/read
fragmentation, exact/+1 capacity, long completion digits, hashes/wire parity,
invalid framing/ASCII/schedules, EOF/retry distinctions, held reads/hashes/writes,
failed close, retained facts after rejected reports and original-clock races.
They do not establish native terminal filtering, useful fit or execution release.

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

Technical decode is reported separately from embedded-notice review
([#62](https://github.com/slackysoba/sobafm/issues/62)). For the `night-owl`
case only, a parser-scope rejection is retained as the metadata verdict and the
decoder still runs: `unsupported_id3_version`, `unsupported_id3_text_encoding`,
`unsupported_nested_id3`, `unsupported_commercial_notice`,
`unresolved_duplicate_frame_id`, `metadata_decoded_payload_reservation`,
`metadata_notice_exceeds_supported_bound`, `metadata_tag_limit` and
`metadata_copy_reservation`. The last two are decided from the ten ID3 header
bytes, before library entry or any use of the parser copy reservation, so the
helper settles under its existing limits either way. The owner first observes
the helper's one bounded rejection record, its fixed stderr line, exit 1 and
every descriptor close, then retires it through the ordinary path under the
original clock. A held descriptor keeps the decoder from starting until the
metadata deadline cancels the case. The rejection stays in `validation.metadata`
with `credits_complete: false`; it is never upgraded to an inspected report.

Every other code, `metadata_inspection_failed`, read/seek/deadline/timing,
case-binding and helper failures remain terminal and start no decoder. The
synthetic `metadata-rejection` case is unchanged. The validation receipt carries
`metadata_verdict` (`inspected`, `rejected`, `incomplete` or `not_run`) and
`decode_verdict` (`passed`, `incomplete` or `not_run`); the owner's `passed`
covers decode and release only. The parser subset, capacity partitions, clocks
and cleanup ownership are unchanged.

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
do not release another fixture derivation, image/build/volume work, PTY preflight,
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
in the whole-group charge. The maintainer [accepted option 2's narrow decoder
kernel PTY exclusion](https://github.com/slackysoba/sobafm/issues/44#issuecomment-6593814582),
recorded in [ADR 0003](../../docs/decisions/0003-decoder-pty-exclusion.md), alongside
the existing kernel pipe/socket exclusion. Only kernel backing is excluded;
userspace diagnostic/audio/application/IPC payload stays charged. The stock
decoder, 98,979,840-byte total and every partition above remain unchanged. The
proposed 524,288-byte runner / 262,144-byte PTY subdivision is not adopted.

A subsequent pair may be admitted only after actual userspace/process/FD
settlement and all other applicable gates. Preserve the original cancellation
clock, failed verdicts and retained userspace ownership/debt; timeout is not
release. This does not claim prior excluded kernel objects were reclaimed.
Close, EOF and helper/container exit do not prove kernel reclamation. Repeated
sessions may leave an unquantified physical/kernel backlog; no three-second
kernel-release bound or eventual reclamation result is established.

The exclusion does not complete runner/other-owner accounting or qualify native
execution. Exact parser allocation/copy overlap, userspace settlement and the new
process topology still require fresh fit evidence. Old native proofs qualify
only their exact unchanged source/image/target cases, not this topology. The
separate #7 decision is now [ADR 0004](../../docs/decisions/0004-amd64-whole-service-adoption.md); it does not qualify this topology.

The metadata reservation explicitly includes decoded Unicode backing and keys,
not just raw bytes. Its conservative source-bound accounting is:

| Simultaneous payload allowance | Bytes |
| --- | ---: |
| Original tag, shrinking slices, current/retained opaque frames: `3 * 524288` | 1,572,864 |
| Admitted raw/text/Unicode/key transients: `24 * 8192` | 196,608 |
| Projected fields, framing IDs, opaque hashes and v1 evidence | 131,072 |
| Report construction/rejection: work R + immutable R + at most one prior record R | 196,608 |
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

The construction/rejection row explicitly assigns the former two-R serialization
allowance and one-R fixed margin; there is no capacity increase or residual pool.
The indexed canonical construction and owned writer above replace the old
materialized JSON string, newline concatenation and write-tail copies. The
selected notice projection retains its 32 KiB encoded bound. These gates apply
to rejected inputs too: near-limit wide text
and cumulative selected payloads fail before the library, while oversized
escaped reports fail before work-buffer allocation. Source tracing and synthetic
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
for each reviewed qualification packet. `preflight.py` implements this slot
preflight at the owner's `/pending/slot` together with the PTY preflight below.
Before any owned-audio qualification,
review the exact-image implementation against the accepted narrow PTY exclusion,
userspace accounting and actual process/FD settlement rules, and require a
separately released, successful bounded no-media PTY preflight. Before actual
Night Owl intake, separately review that evidence, fresh init -> owner ->
sequential-helper topology fit and native terminal-filter qualification evidence.
The contract acceptance itself releases none of these operations and does not
renew the original **2026-11-09T00:00:00Z** recording expiry.
No inherited real-file input is transported through Docker CLI descriptor numbers.

### No-media preflight and packet freeze

[#63](https://github.com/slackysoba/sobafm/issues/63) adds `preflight.py` for the
facts no other repository tool collects. The **operator order is preflight,
freeze, then owned cases**; each step is one explicit invocation after its own
release, and nothing here runs in ordinary CI, where Docker is a local fake.

1. **Preflight** (`preflight run --execute-preflight`) is one finite Docker
   operation under one original `CaseClock` (90 seconds, three-second cleanup
   obligation) and the existing management owners: at most two live commands
   and 20 operations, no retries. It verifies the engine and image, creates one
   new volume labeled for #44 and this run's random identity (an existing
   volume of the same name lacks that label and fails), then runs two
   containers with the owner container's limits, read-only bind mounts and
   volume mount. The setup container is the only root process, restricted to
   `CAP_CHOWN`, and creates the UID/GID 1000, mode 0700 slot. The probe
   container runs as UID/GID 1000. Both stat the slot and the input without
   opening it; the slot and input identities must match across the two
   containers. The probe also hashes the init and interpreter executables and,
   with synthetic bytes only, checks PTY raw readback, write backpressure within
   1 MiB and that EOF arrives only after the last held slave closes. A failure
   stops a running container once under the original clock; containers and the
   volume are never removed, and debt stays in the receipt.
2. The private ledger written through the existing `Journal` is the bounded
   receipt. **Freeze** (`preflight freeze`) accepts only a settled, passed,
   debt-free receipt for this checkout, writes one new mode 0600 packet at a
   chosen end time T with `synthetic_preflight_sha256` set to the receipt's
   SHA-256, and immediately round-trips it through `load()` or
   `load_qualification()`. T must fall within 210 seconds of that load and the
   request expiry, so freeze directly before the controller. Freeze creates the
   packet owned by whoever ran it, mode 0600, and the controller reads it as
   UID 1000, so `chown 1000:1000` the frozen packet before the controller runs;
   with root as the operator, an unchowned packet fails with `PermissionError`.
3. Run the owned cases with the controller invocation below. Each owned case
   needs its own preflight and freeze at the frozen commit, because each
   receipt binds one volume and slot.

This preflight is not native qualification or execution permission. Its PTY
facts do not establish kernel PTY reclamation, and executing it locally is a
separate, later step.

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
