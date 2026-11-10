# L2 generated buffering

[L2](../../docs/linux-feasibility.md#later-application-qualification) exercises the installed
Lyria WebSocket reader, parser, PCM banks and renewal callback in one candidate image.
[ADR 0008](../../docs/decisions/0008-lyria-realtime-path.md) defines the copy owners and limits.

## Development check

On Linux, from the repository root with the locked development environment and OpenSSL:

```sh
uv run pytest -q evaluations/linux_resource/test_l2_buffering.py
```

This uses a separate loopback peer process and a disposable TLS identity. It exercises one
accelerated crossfade and a second pair that trips a bound during the fade. The failure must
survive a successful join. Additional checks cover exact copy arithmetic and a cgroup event.
This command supplies development evidence only.

## Proposed image run

Obtain independent review and specific execution approval for **one** run. Supply the full
reviewed harness commit, full source commit from the candidate's build record, immutable local
image ID, and the native amd64 Docker target and kernel release. Verify the image-to-source
mapping from that build record before starting. The wrapper checks the local image ID, daemon
architecture and kernel. It reads the two harness files from the reviewed Git objects and
records the supplied source's lockfile hash. No build, pull, deployment or real credentials
are part of this run. Use a Linux shell with the agreed local Docker daemon, a bind-mountable
temporary directory and OpenSSL. Run in the clean reviewed checkout.

```sh
# Set HARNESS_SHA, IMAGE_SOURCE_SHA, IMAGE_ID and TARGET_KERNEL from the approved scope.
# Use a fresh result filename. Retain stdout only.
(
  set -o noclobber
  ulimit -f 64
  timeout --kill-after=10s 150s bash evaluations/linux_resource/run-l2-buffering.sh \
    "$HARNESS_SHA" "$IMAGE_SOURCE_SHA" "$IMAGE_ID" "$TARGET_KERNEL" > l2-result.jsonl
)
```

One peer and one client use the candidate's installed Python, default seccomp, non-root UID
1000, a read-only root, no capabilities, no new privileges and separate private cgroups.
Their shared network namespace has loopback only. The client imports the application dependency
set, runs the crash-dump controls and actual memory/swap/PID startup gate, and then drives
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

- Each pair reports readiness, both PCM occupancies, one-bank and overlap Python allocation
  high-water marks, frames, cleanup verdict counts, remaining leases and elapsed milliseconds
  from each bank's original cancellation to observation of its actual release. A missing
  frame, missing handoff, retry, expired cleanup or retained bank fails the pair. Release
  observed after three seconds also fails, even if the stream eventually joins cleanly.
- Tracing starts before the first lease, spans renewals and keeps earlier pairs' live
  allocations. Each pair resets just the high-water mark. The allocation check includes bank
  backing, transient parser, serialization and PCM copies, Python object overhead, and a
  16,709-byte partial native TLS record allowance per session. The ceiling is one 2,304,000-byte
  bank and 1,572,864 private copy bytes per owner plus the existing 1,048,576 shared scratch
  and drain bytes. The outgoing bank's observed release also triggers a single-owner check
  of live allocations. After the join the same check allows zero bank or private producer
  bytes, with the shared ceiling covering residual traced overhead. The trace is an aggregate
  check for this workload. Individual slot
  ownership, fragmentation and many-read assembly have separate evidence in ADR 0008 and the
  transport regression tests. Native allocations outside the Python trace contribute to the
  measured whole-group peak.
- Group memory high-water, sampled PID high-water and kernel memory/PID events cover the
  measured client and its init. Any resource event or exceeded bound stops further admission.
  The full reservation remains 90,177,536 + 4,608,000 + 4,194,304 = **98,979,840 bytes**.
- The pair workload has a 22-second deadline. Cleanup joins its original owners, including
  after a failure. The host bounds the client wait at 90 seconds. A host timeout or OOM is
  a failure with unconfirmed settlement. Stopping the container cannot supply a clean verdict.
  The wrapper removes only its two containers and generated fixture files on exit. If the
  daemon becomes unresponsive, record incomplete cleanup and verify both containers before
  authorizing further work.
- Require the five successful pair rows, the ten-session success summary, client exit zero,
  no OOM and wrapper exit zero. Retain failures and partial output. The wall-clock ceiling
  is 160 seconds including the outer kill grace. Allow at most 64 KiB of retained JSON and
  one 64 KiB rolling log per temporary container. No audio, prompts, keys, environment
  dumps or addresses belong in the result.

The result applies to this transport workload on the identified image and target. Live
provider behavior, network throughput to the provider, station continuity, Discord, listening
quality and the other qualification cases remain separate.
