# 0004: Adopt the Docker-owned whole-service group for amd64 playback

Status: **Accepted** (2026-11-04).
Issue: [#7](https://github.com/slackysoba/sobafm/issues/7).
Proposal: [scoped architecture proposal](https://github.com/slackysoba/sobafm/issues/7#issuecomment-6556349926).
Acceptance: [maintainer decision](https://github.com/slackysoba/sobafm/issues/7#issuecomment-6634180574).
Native arm64: on hold, tracked in [#60](https://github.com/slackysoba/sobafm/issues/60).

This record supersedes two parts of
[ADR 0001](0001-linux-resource-proposal.md), both decided by the maintainer: its
experimental-only scope, and its native-arm64 evidence item, which moves to #60
(see Consequences). ADR 0001 remains the historical experiment decision and is
not edited. The narrow decoder PTY exclusion in
[ADR 0003](0003-decoder-pty-exclusion.md) is unchanged and remains in force.

## Problem

ADR 0001 approved the Docker-owned whole-service group for a bounded experiment
only. It requires a later architecture-adoption decision that cites actual
results and states supported targets and scope before the application depends
on the group.

The experiment is complete for its recorded native-amd64 cases, integrated in
[PR #39](https://github.com/slackysoba/sobafm/pull/39):
[Q1/Q2 runtime authority](../../evaluations/linux_resource/README.md#recorded-amd64-result),
the [Q3 CBR/VBR/FLAC/overlap acceptance](https://github.com/slackysoba/sobafm/issues/7#issuecomment-6555741812)
and the [four-case Q4 cancellation acceptance](https://github.com/slackysoba/sobafm/issues/7#issuecomment-6556104431),
each under separately recorded source/image identities. See
[Linux feasibility](../linux-feasibility.md#recorded-native-amd64-decoder-proof).
Native arm64 has no container, limit or decoder evidence. Its read-only
preflight reported piped core routing as the only issue
[on November 1](https://github.com/slackysoba/sobafm/issues/7#issuecomment-6553570914)
and [again on November 4](https://github.com/slackysoba/sobafm/issues/7#issuecomment-6634028903).

## Options

1. **Adopt the tested design now for amd64.** Build the first eligible-recording
   playback slice on the whole-service group with every limit unchanged, and
   keep native arm64 as a separate later gate.
2. **Defer adoption until native arm64 evidence exists.** Keep ADR 0001
   experimental-only. Playback implementation waits for an accessible arm64
   target that satisfies the unchanged core-routing gate; none is known.

## Decision

The maintainer accepted **option 1** on 2026-11-04: adopt the tested
Docker-owned whole-service group for implementing the first eligible-recording
playback slice on amd64. Native arm64 is **on hold until after the first amd64
release**.

The adopted design is:

- One ordinary non-root, default-seccomp Docker/Compose service, with runtime
  containment before untrusted input and no application cgroup manager or
  runtime socket.
- The full 90,177,536-byte group charged to every playing server, two globally
  owned 2,304,000-byte PCM banks and one 4,194,304-byte audio/IPC-copy pool:
  98,979,840 controlled bytes.
- At most two charged PCM-bearing native address spaces, including descendants
  and cleanup debt. The conditional PCM bound remains 33,850,880 bytes. The
  exact reviewed native closure, backing restrictions and effective limits must
  hold.
- One original three-second cleanup deadline, and no capacity reuse until
  actual process, descriptor, I/O and payload ownership is released.
- The known whole-service OOM failure domain: exhausting the shared container
  limit can stop all stations.

The implementation scope is one admitted recording through join, play, correct
heard-source credits, stop and shutdown. Preserve the reviewed pre-payload,
single-threaded launch/admission premises. A different application startup or
replacement launch path must account any inherited PCM until actual exec/release
and qualify its process/thread ownership before input. Parent, decoder,
transport and voice overhead must fit the unchanged group. Application-owned
audio/IPC payload copies stay within the explicit global PCM/copy reservations;
decoder-native PCM stays within the reviewed address-space/process-count bound.
The requirements' named exclusions are preserved without treating them as extra
payload pools or claiming a total-RSS or physical-audio-memory ceiling. This
imposes no product limit of one server and claims no concurrent throughput.

This decision does not authorize provider, paid, Discord or voice activity or
use of the real recording. It does not qualify the application, and it adds no
capacity: ADR 0001's 268,435,456-byte alternative remains unmeasured and
unauthorized. Real-source access, complete-recording validation, credit
admission and application qualification remain separate gates. Human listening
remains optional and deferred.

Native arm64 is on hold until after the first amd64 release
([#60](https://github.com/slackysoba/sobafm/issues/60)) and is not a gate for
Outcomes 4 or 5. The ordinary `Linux checks (arm64)` CI job and the existing
read-only preflight tooling stay as they are. Nothing establishes arm64 as
infeasible; resuming it needs a new maintainer decision.

## Reasons

- The recorded amd64 cases exercise the adopted design's runtime authority,
  effective limits, owned useful fit, aggregate PCM and cancellation release
  under the same limits. That is enough to start a narrow slice; it is not
  release evidence.
- Deferral would hold all playback implementation until an accessible arm64
  target satisfies the unchanged core-routing gate, and
  [none is established](https://github.com/slackysoba/sobafm/issues/7#issuecomment-6553650111).
  Adoption lets independent implementation proceed.
- Adoption adds no service, framework, capacity or host-security change.

## Consequences

- The first eligible-recording playback slice, as scoped above, can depend on the
  whole-service group on amd64.
- The first release targets Linux amd64 only. ADR 0001's native-arm64 evidence
  item no longer gates that release; it moves to #60.
- All stations share one OOM failure domain.
- If useful application fit fails under these unchanged limits, preserve the
  measurement and return with one concrete alternative. No larger cap is
  accepted here.

Two limits of the current evidence remain:

- **WSL2 kernel.** Every recorded amd64 case ran on native x86_64 with kernel
  `6.18.33.2-microsoft-standard-WSL2`. No other amd64 kernel or host has run
  them.
- **Core-routing gate.** The harness requires the host's core routing to be
  exactly `core`. The gate is retained, but no requirement or decision record
  states its reason. An amd64 host that pipes core dumps would also fail it.
  [#4](https://github.com/slackysoba/sobafm/issues/4) tracks recording that
  requirement, or changing the gate, before a real host is chosen.

## Verification needed

- Affected requalification at the real boundaries of the actual application
  image, entrypoint, native dependencies, target kernel, page size and
  core-routing premises. Unchanged historical results qualify only their
  original source/image/target cases.
- Parent, decoder, transport and voice overhead fitting the unchanged group in
  the eligible-recording slice, with deterministic cancellation/debt tests and
  actual resource release.
- The staged L1–L6 integration evidence in
  [Linux feasibility](../linux-feasibility.md#later-application-qualification),
  collected at real boundaries;
  these are not prerequisites to writing the narrow slice.
- Independent review of the implementation and its qualification.
- amd64 release evidence tied to the release source and image.
- Native arm64 qualification under #60, after the first amd64 release.
