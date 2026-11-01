# Direct-decoder cancellation cases

This is the direct-decoder part of the bounded experiment for
[issue #7](https://github.com/slackysoba/sobafm/issues/7), following the accepted
[Q4 contract](../../docs/linux-feasibility.md#initial-stop-or-go-proof).
Both native-amd64 direct cases passed once after the
[initial Q3 acceptance](https://github.com/slackysoba/sobafm/issues/7#issuecomment-6555741812)
and exact source/operator review and per-case release. Results are recorded below.
Offline tests alone do not qualify cancellation,
Linux containment, native arm64, listening quality or application L4 behavior.

## Scope and retained inputs

`owned_mp3_cancel.py` implements only cancellation during feed and cancellation
with stdout backpressured. Each is a separate one-shot operation with its own
original owner. It imports the existing fixed decoder argv, native file
allowlist, fixture contract and pipe layout without changing the qualified
CBR, VBR or overlap implementations. It adds no application module, dependency,
image, native build, fixture generation or additional PCM reservation.

The bindings remain:

| Item | Binding |
| --- | --- |
| Native target | Linux amd64, one decoder, nonroot UID/GID 1000 |
| Image | `sha256:e3ff7b02740b55f52735b17afc9846d151f86bf7395341f5d53f7d3dbb235cfc` |
| mpg123 executable | `62ef82208722eda0fedd39de0ced0d8e3a8450d129760152ea16d075651f0a91` |
| CBR fixture | 1,920,768 bytes; `1a431c03098d8be79a522a3b1f64a8182445a8bf6c009f54c1b3bb1f6dad044d` |
| Native hard/soft limits | DATA 12,000,000; STACK 524,288; AS 12,524,288; CORE zero |
| Group | 90,177,536 bytes; swap zero; two CPUs; 64 tasks |
| Parent PCM/copy reservation | Two 2,304,000-byte banks and one 4,194,304-byte copy region |
| Submitted compressed prefix | At most 65,536 bytes; authenticated full snapshot remains owned |

Keep the existing default seccomp, no capabilities/new privileges/network,
read-only root and read-only source/fixture mounts, clean C locale and init.
The fixed command remains:

```text
/usr/bin/mpg123 --no-control --no-visual --buffer 0 --stdout --rate 48000 --stereo --encoding s16 --resample ntom --no-resync -
```

The conditional PCM arithmetic remains
`2 * 12,524,288 + 8,802,304 = 33,850,880` bytes, within 34,560,000.
The separate controlled-capacity ledger remains 98,979,840 bytes. Reusing the
qualified compressed fixture for interrupted prefixes does not authorize
another complete CBR, VBR or overlap run.

## Admission and observed triggers

Fork precedes all parent PCM allocation. The owner retains every pipe as it is
created, including partial acquisition and failed fork. Before exec it checks
the direct child's containment, limits and identity. Before any input, it
checks the actual executable, mapped files, single native thread, stopped
PID/start identity and exact process inventory. Its cancellation-aware setup
polls preserve partial diagnostics and can enter cleanup during banner or
stopped-state observation. These checks use the existing shared parsers;
historical harness files and results remain unchanged.

Only after this gate does the parent allocate and touch the existing banks/copy
region, authenticate the complete compressed snapshot, obtain read-only input
and bounded output views, and resume the decoder. The 65,536-byte output view
starts at offset 2,000,000 in the copy region, disjoint from compressed input.
Original nonblocking writes and `readv` calls retain their owner through
payload processing and view completion. A synchronous operation cannot be
replaced merely because cancellation arrived during its completion boundary.

Both cases have a 15-second phase-establishment deadline from preparation
start. The feed case requires at least four bytes of actual decoded PCM, a
still-live admitted decoder, open input and an unfinished compressed fixture.
The first cancellation is recorded immediately at that observed boundary.

The backpressure case first receives actual PCM, then pauses stdout consumption
while continuing bounded input and diagnostic handling. It requires all of:

- Actual full pipe occupancy, using `F_GETPIPE_SZ` and `FIONREAD`, without
  changing the pipe capacity.
- The native stdout descriptor naming the same pipe device/inode as the
  parent's owned reader.
- `/proc/PID/syscall` reporting amd64 `write(1, ..., positive_count)`, followed
  by the same PID/start identity in sleeping state and a second full-pipe
  observation.

Linux documents the capacity/occupancy interfaces in
[pipe(7)](https://man7.org/linux/man-pages/man7/pipe.7.html) and the syscall
register format and ptrace access check in
[proc_pid_syscall(5)](https://man7.org/linux/man-pages/man5/proc_pid_syscall.5.html).
The runner checks syscall-read access while the child is stopped, before
input. Missing access, unsupported/ambiguous data or an unestablished trigger
fails the case; no host-security adjustment, timed-delay substitute or pipe
expansion is permitted. A full pipe alone is insufficient evidence.

## One cancellation owner and deadline

The first cancellation records one original three-second deadline before
closing input, draining or signalling. The second request occurs at the
recorded transition after TERM dispatch. Additional requests cannot reset the
deadline or replace the owner. The same process identities, descriptors,
in-flight operations and payload views remain charged until actual release.

Cleanup stops admission, sends TERM to the owned process group, resumes a stopped
child if necessary, closes input and continues bounded stdout/stderr reads.
TERM is dispatched before input closure can turn the unfinished prefix into
ordinary EOF completion; a failed signal still closes acquired descriptors.
KILL escalation
is eligible one second after the original cancellation start. Before the
child establishes its own group, setup-failure cleanup signals only the
unreaped direct child. A changed admitted process group fails the case.

The direct child must be reaped, the whole native inventory empty, all owned
descriptors closed and all original I/O completed before releasing views and
buffers. EOF, leader exit, signal delivery and elapsed grace independently
prove none of those other releases. Failed close is not blindly retried;
`EBADF` observation can establish actual release, while an open or uncertain
descriptor remains debt. The first failure remains recorded.

A missed original deadline permanently prevents a passing verdict. Late actual
release clears debt but does not repair the deadline. A stuck operation or
remaining descendant retains ownership and refuses replacement admission.
Management continues without launching another workload. The 45-second native
retention backstop emits failure while retaining the original owner; an
external container stop is failure recovery, never successful producer cleanup.
Bounded polling observes release; it does not manufacture a race by sleeping.

Diagnostics are capped at 65,536 bytes. Cancellation accepts the exact native
banner, forced s16 note, fixed 48 kHz source format, playing line, SIGTERM note
and one final decoder status line. This predicate follows the retained
mpg123 1.33.7 signal/exit source: a zero exit and its final status line can occur
after interruption and never establish complete audio. Unknown, repeated or
missing required diagnostics fail the case. Results record only counters,
hashes, bounded diagnostics and ownership evidence; no PCM is exported.

The receipt distinguishes bytes accepted, bytes received/drained, bank high
water, discarded partial-frame residue, request/signal timestamps, original
deadline and actual release, exit/reaping, descriptor/process/view ownership,
memory-event deltas and swap. `complete_decode` remains false. Each passing
case qualifies only that direct-decoder case.
Deadline/backstop events include the outstanding descriptors, operations,
payload lease and current native inventory. An unavailable inventory is
explicitly unknown; late release reports cleared debt while retaining failure.
Each event is attempted once. A failed evidence write records failure and keeps
the original cleanup loop accountable until actual release.

## Offline regression scope

`test_cancel_contract.py` is explicitly included in ordinary pytest collection.
It uses local pipes, the actual reader/writer/owner and deterministic kernel
outcomes. It never forks, invokes a decoder or uses Docker. Covered schedules
include delivery before read/write completion; repeated cancellation during
drain; TERM/KILL grace; EOF before exit and exit before EOF; retained descendant
and stuck original I/O; close ambiguity; read interruption/failure; stderr
overflow; exact-deadline and late release; partial pipe acquisition; actual
preparation fork/limit failure; cancellation during setup; full-pipe/blocked
write observation; and complete pump release after EOF and reaping.
Actual-loop schedules also close the evidence sink at deadline/backstop reporting
while original I/O remains owned, then require later release without a retry loop.

The repository's locked sync, all-file hooks/strict Pyright and full pytest
remain required. Passing them is preparation evidence only.

## Execution and topology extension

After Q3 and exact independent review, prepare a separately reviewed external
operator and explicit one-shot release for each case. Its proposed operation
bound is 90 seconds, with the existing original three-second external cleanup
deadline, retained CLI/readers, verified container ownership and final absence.
Unknown creation or incomplete cleanup remains debt; no automatic retry.
Retain at most 700,000 bytes per case, at most 64,000 selected public bytes and
one bounded 32,768-byte Docker local log file. Stay within the agreed project
storage and artifact allowance. No concrete native operation is released here.

The [prepared topology extension](mpg123-topology-plan.md) supplies the other two Q4 cases: an owned child that truly
ignores TERM and a descendant surviving leader exit. Stock mpg123 replaces
inherited SIG_IGN; SIGSTOP or a blocked TERM mask cannot qualify as ignoring
TERM. The accepted proposed interpretation uses a narrow non-media launcher
with the unmodified decoder performing actual I/O. The
[concrete implementation design](https://github.com/slackysoba/sobafm/issues/7#issuecomment-6552315831)
was accepted, covering descriptor transfer, identities, pre-PCM forks and
capacity/ownership evidence. The extension has its own narrow launcher and
validator; these two direct-decoder cases retain their original PGID semantics.
Native execution still requires exact source/operator review and release.

The separately recorded topology cases also passed. Later application
qualification remains outstanding; this document releases no new operation.

## Recorded direct cancellation results

Both cases ran once on 2026-11-01 at source
`e5e2f8cc0f53478a83b6b22b15f20b5f1ec65ed5`, with the image and native bindings
above, on x86_64 kernel `6.18.33.2-microsoft-standard-WSL2`.
The [concrete operator review](https://github.com/slackysoba/sobafm/issues/7#issuecomment-6555746271)
and [independent four-case result acceptance](https://github.com/slackysoba/sobafm/issues/7#issuecomment-6556104431)
bind the exact issued owners and all 17 files in each frozen case manifest.

| Case and selected receipt | Observed cancellation trigger | Native release after first cancellation |
| --- | --- | --- |
| [Feed](results/2026-11-01-q4-feed.yaml) | Input remained unfinished after 65,536 accepted compressed bytes and 8,192 PCM bytes received. | 0.001828242 seconds |
| [Stdout backpressure](results/2026-11-01-q4-stdout-backpressure.yaml) | 65,536 unread pipe bytes equaled its capacity; the same decoder was sleeping in an amd64 write to stdout FD 1. | 0.001632895 seconds |

Each case dispatched TERM and recorded a second cancellation against the same
original three-second deadline. Both produced 74,428 prefix PCM bytes in total,
including 66,236 drained after cancellation; all original I/O, descriptors,
process and payload owners released. Exit zero is recorded with
`complete_decode: false`, not interpreted as complete-fixture playback.

Both original native and external cleanup deadlines passed. Memory-event deltas
and swap were zero; stopped/no-OOM containers were removed and exact ID/name
absence verified. No cleanup or CLI debt remained. Both banks and the copy
reservation retained their unchanged capacities. No PCM was exported, and these
results do not establish application L4 behavior, native arm64 or listening quality.
