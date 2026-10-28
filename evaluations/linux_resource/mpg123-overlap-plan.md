# Retained-fixture two-decoder overlap

Issue [#7](https://github.com/slackysoba/sobafm/issues/7). **Preparation only; no
overlap execution claimed.** The completed
[single-decoder confirmation](mpg123-fixture-plan.md#corrected-confirmation)
is reused as unchanged evidence. This next Q3 case uses the same owned compressed
fixture, image, executable, command, numeric capacities and security settings.
No fixture generation, package installation, source audio or Q4 case is included.

## Admission and payload ownership

The [overlap harness](owned_mp3_overlap.py) creates all twenty pipe ends and both
child owners before either fork. The parent has no PCM at either fork. Each child
closes the other child's endpoints as well as its own unused endpoints, so it
cannot keep the other child's EOF alive. A failed second fork or setup retains
the first child and all descriptors for cleanup.

Both children must report and expose the same verified hard/soft AS 12,524,288,
DATA 12,000,000, STACK 524,288 and CORE zero limits before either exec gate is
released. Each then emits the source-backed native banner and reaches the stopped
PID/start-identity, executable, thread and mapping gate. The whole-group inventory
must equal exactly the two admitted PIDs; each process group contains only its
owned leader. Both stopped gates must succeed before allocating any parent PCM.

Allocate and page-touch the existing two 2,304,000-byte banks and one
4,194,304-byte copy reserve. Authenticate one complete compressed snapshot in
the first 1,920,768 bytes, with the unchanged fixture digest. Both decoders use
that same read-only snapshot with independent input positions. Their 65,536-byte
PCM read views start at offsets 2,000,000 and 2,065,536 in the copy reserve.
The input and output partitions are disjoint and fit the original reservation.
Each decoder owns exactly one bank. Circular writes use contiguous memoryview
assignments; wrapped bytes remain in that bank, without an extra PCM object.

Conditional on the same reviewed native mapped/backing scope and retained-owner
limit, PCM capacity remains `2 * 12,524,288 + 8,802,304 = 33,850,880` bytes.
Controlled capacity remains 98,979,840 bytes. The shared group cap is 90,177,536
with no swap, two CPUs, 64 tasks, non-root identity, default seccomp, no added
capabilities, no new privileges, no network and a read-only root. A task cap
does not replace the explicit two-owner admission check.

## Measured overlap and completion

Each bank must hold at least 1,152,000 complete-frame bytes within 15 seconds
of its own fork. No bank drains until both readiness predicates hold and both
native identities are live. Then start the measured overlap interval.

For 30 seconds, drain 3,840-byte increments per bank at a 192,000-byte-per-second
clock schedule. A late observation catches up only against already retained
complete frames; if either bank lacks the due bytes, the case fails without
partially advancing the other bank. Repeated observations do not double-drain.
Reads stop when their exclusive bank is full, creating bounded backpressure.
Both native identities must remain live through the measured interval. The
30-second wall-clock pacing defines the workload; no sleep manufactures a test
race. Each bank must drain exactly 5,760,000 bytes during that interval.

After the interval, finish each full fixture through bounded accelerated drains.
Each decoder must accept exactly 1,920,768 input bytes, produce and drain exactly
23,040,000 aligned PCM bytes, match the prior PCM digest, pass the unchanged
source-bound diagnostic policy and exit zero. One successful decoder cannot
substitute for the other. Record independent readiness, bank high-water marks,
counts, hashes and exit evidence, along with the measured simultaneous interval.
The original overall workload deadline remains 150 seconds.

## Release and review boundary

One original three-second cleanup deadline covers both owners, all descriptors
and payload views. TERM/CONT is sent to every retained owned leader/group before
the aggregate wait; KILL follows within that same deadline. A reaped first
leader cannot release the second slot. Group and whole-cgroup inventories retain
unexpected process debt. Late release remains a failed deadline even if the
process subsequently disappears. Release requires both leaders reaped, no
retained processes or descriptors, and all payload references released.

The external operator must pin the committed sources and exact image/ELF/fixture,
mount only evaluation sources and the single fixture read-only, and use a fresh
container name and evidence directory. Its existing 180-second operation,
165-second attached-call and 60-second external ownership-cleanup limits remain.
Selected output is bounded to 500 KB and the operation evidence to 800 KB,
leaving room for source changes and a compact selected result in the 1 MB slice.
Normal removal
and verified absence apply only to the positively owned stopped container.

Deterministic offline regressions cover a failed second fork/native gate,
cross-child descriptor closure, one stalled bank, circular capacity and frame
accounting, the real full pacing interval, an exited second identity, and a
retained second child across forced termination and the original deadline.
They use explicit fake-clock/event outcomes and execute no native command.
Concrete independent review and release are required before one overlap attempt.
No automatic retry, new fixture, additional format, Q4 or native arm64 execution
follows from this preparation or a future pass.
