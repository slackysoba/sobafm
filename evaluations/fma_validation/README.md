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

The original supplied file is the only candidate. A closed packet binds its exact
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
Within that absolute framing cap, only tags of at most **524,288 bytes** are
currently admitted to Mutagen: its original data, shrinking slices and frame
payloads can overlap. This explicit supported subset leaves projection/report
space in the 2 MiB metadata copy partition. Larger tags fail with
`metadata_copy_reservation`; their contents are never truncated. The actual
recording's tag size is unknown. A larger tag would need a reviewed alternative.
Compression, encryption, unsynchronization, extended headers and unsupported
transforms fail before library allocation. Selected notices have field and report
bounds; oversized notices fail without truncation. ID3v1 and v2 are preserved
separately with conflicts, URLs, contributors and notices; unknown frames retain
hash/length only. Artwork/lyrics frames are omitted explicitly. No URLs are
followed and `credits_complete` remains false.

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
[term_posix.c](https://scm.orgis.org/mpg123/tags/1.33.7/src/compat/term_posix.c) and
[parse.c](https://scm.orgis.org/mpg123/tags/1.33.7/src/libmpg123/parse.c).
Python-only PTY tests establish channel behavior; they do not qualify the native
terminal filter. Frozen synthetic native tag/encoding/ordering/cancellation cases
must pass under the exact later image before actual source intake.

## Capacity and execution release

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
in the whole-group charge. Existing kernel pipe/socket-buffer exclusions cover
PTY channel backing; this introduces no new exclusion. The code bounds I/O and
report buffers, but exact parser allocation/copy overlap and the new process
topology require fresh fit evidence. Old native proofs qualify only their exact
unchanged source/image/target cases, not this topology.

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
