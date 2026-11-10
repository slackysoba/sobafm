# L2 generated buffering

[L2](../../docs/linux-feasibility.md#later-application-qualification) exercises the installed
Lyria WebSocket reader, parser, PCM banks and renewal callback in one candidate image.
[ADR 0008](../../docs/decisions/0008-lyria-realtime-path.md) defines the copy owners and limits.

## Development check

On Linux, from the repository root with the locked development environment and OpenSSL:

```sh
uv run pytest -q evaluations/linux_resource/test_l2_buffering.py \
  evaluations/linux_resource/test_l2_runner.py
```

The client checks use an isolated loopback peer and a disposable TLS identity. The schedules
include an overrun during the surviving session's close, repeated cancellation of the cleanup
owner, and two late releases after a prior copy failure. The host checks substitute a local
command for Docker. They interrupt creates and waits, fail removal, and supply truncated,
inconsistent and complete synthetic results. This command supplies development evidence only.

## Proposed image run

Obtain independent review and specific execution approval for **one** run. Supply the full
reviewed harness commit, full source commit from the candidate's build record, immutable local
image ID, and the native amd64 Docker target and kernel release. Verify the image-to-source
mapping from that build record before starting. The wrapper checks the local image ID, daemon
architecture and kernel. It compares the host scripts with the reviewed Git objects, mounts
the client and peer from those objects, and records the supplied source's lockfile hash.
Use the clean reviewed checkout, a Linux shell with Python 3.14 available as `python3`, the
agreed local Docker daemon, a bind-mountable temporary directory, Git, OpenSSL and GNU timeout.

```sh
# Set HARNESS_SHA, IMAGE_SOURCE_SHA, IMAGE_ID and TARGET_KERNEL from the approved scope.
# Use a fresh result filename. Retain stdout only.
(
  set -o noclobber
  ulimit -f 64
  timeout --kill-after=35s 155s bash evaluations/linux_resource/run-l2-buffering.sh \
    "$HARNESS_SHA" "$IMAGE_SOURCE_SHA" "$IMAGE_ID" "$TARGET_KERNEL" > l2-result.jsonl
)
```

One peer and one client use the candidate's installed Python, default seccomp, non-root UID
1000, a read-only root, no capabilities, no new privileges and separate private cgroups.
Their shared network namespace has loopback only. The client imports the application dependency
set, runs the crash-dump controls and actual memory/swap/PID startup gate, checks its
effective two-CPU quota, and then drives
`GeneratedStream` directly. The client has 90,177,536 bytes, zero swap, 64 tasks and two CPUs.
The peer has a separate 128 MiB ceiling, zero swap, 64 tasks and two CPUs. Its serializer,
synthetic PCM, TLS key and writes stay outside client measurements. Neither container receives
catalog, database, service configuration or host network access.

The peer runs for at most 120 seconds, allows two connections per pair, and sends at most eight
two-second, 384,000-byte synthetic PCM messages per connection. The client requests five pairs
in sequence. Each starts one session, reaches six-second readiness, and reaches the real reader's
backpressure threshold. Then the harness releases only the renewal timer. It waits for the
replacement to pre-roll and reach the same threshold, records both bank occupancies, reads all
200 crossfade frames at 20 ms intervals, checks mixed and replacement samples, and waits for the
outgoing bank to release. The production stall, preparation and first-cancellation clocks run
normally. At most ten sessions, five renewals and 30,720,000 input PCM bytes are in scope.
PCM and frames are discarded in memory.

## Reading the result

- Each pair reports readiness, both PCM occupancies, Python allocation high-water marks by
  live owner count, frames, cleanup verdict counts, remaining leases and elapsed milliseconds
  from each bank's original cancellation to its observed refund. Each bank has its own entry.
  It stays null while the lease remains charged. A missing
  frame, missing handoff, retry, expired cleanup or retained bank fails the pair. Release
  observed after three seconds also fails, even if the stream eventually joins cleanly.
- Tracing starts before the first lease, spans renewals and keeps earlier pairs' live
  allocations. At each acquisition and actual refund the observer seals the outgoing peak
  and resets the high-water mark. The three reported peaks correspond to zero, one and two
  live owners. Close work on the survivor is charged to its single-owner interval. The
  allocation check includes bank
  backing, transient parser, serialization and PCM copies, Python object overhead, and a
  16,709-byte partial native TLS record allowance per session. The ceiling is one 2,304,000-byte
  bank and 1,572,864 private copy bytes per owner plus the existing 1,048,576 shared scratch
  and drain bytes. After the final refund the shared ceiling covers residual traced overhead.
  The trace is an aggregate check for this workload. Individual slot
  ownership, fragmentation and many-read assembly have separate evidence in ADR 0008 and the
  transport regression tests. Native allocations outside the Python trace contribute to the
  measured whole-group peak.
- Group memory high-water, sampled PID high-water and kernel memory/PID events cover the
  measured client and its init. Any resource event or exceeded bound stops further admission.
  The full reservation remains 90,177,536 + 4,608,000 + 4,194,304 = **98,979,840 bytes**.
- The pair workload has a 22-second deadline. Cancellation keeps joining the original finish
  task, records its settlement and final counters, and then propagates. An earlier failure
  stays recorded even when both banks subsequently release. A host timeout, OOM or hard kill
  leaves any unobserved application settlement unconfirmed.
- The host allows 120 seconds for setup and workload, including at most 90 seconds for the
  client wait. A separate 30-second budget covers cleanup. The outer watchdog signals at
  155 seconds and allows another 35 seconds, for a hard ceiling of **190 seconds**. The host
  owns each CLI child directly and can interrupt its wait. Before either create it records
  unique container names and their ownership label in a private `recovery.json`. On exit it
  looks up both exact names, verifies the label before stopping or removing a container, and
  checks absence after removal.
- If a create response is lost and the daemon cannot confirm what happened, or removal cannot
  be verified, keep the private run directory and fixture files. The first result row records
  the run ID. The directory has that name under the run's temporary directory. Use its two
  exact names and `org.sobafm.l2-run` label to reconcile this run before another attempt.
  A missing final wrapper verdict also leaves the run incomplete.
- Only the **last wrapper verdict** can pass the run. It requires a matching client target
  and caps, five ordered successful pair rows with both refunds and in-bound measurements,
  the ten-session/five-renewal summary, client exit zero, no OOM, verified cleanup and
  wrapper exit zero. Partial client records are retained as evidence on failure.
  Allow at most 64 KiB of retained JSON and one 64 KiB rolling log per temporary container.
  The client-record input is limited to eight rows and 16 KiB. No audio, prompts, keys,
  environment dumps or private paths belong in the result.

The result applies to this transport workload on the identified image and target. Live
provider behavior, network throughput to the provider, station continuity, Discord, listening
quality and the other qualification cases remain separate.
