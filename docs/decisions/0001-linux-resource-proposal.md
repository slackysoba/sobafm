# 0001: Evaluate a Docker-owned whole-service resource group

Status: **Proposed, not accepted** (2026-10-27).
Issue: [#7](https://github.com/slackysoba/sobafm/issues/7).
Parent: [Outcome 1](https://github.com/slackysoba/sobafm/issues/1).

## Problem

The approved metric requires retained whole-worker caps plus prepaid parent PCM
and audio/IPC copies strictly below 100,000,000 decimal bytes per playing server,
with aggregate PCM at most 34,560,000. Containment must precede untrusted input
in non-root, default-seccomp Docker. Application cgroup delegation and process
migration cannot be assumed. A useful decoder/provider workload must actually
fit; merely choosing limits is insufficient.

## Options

1. **Evaluate one runtime-owned group covering the entire service.** Charge its
   full 90,177,536-byte cap to every playing server, plus fixed global parent PCM
   (4,608,000) and copy (4,194,304) pools. The total is 98,979,840 bytes. The
   parent also consumes the group, so this is intentionally conservative.
2. Create delegated application worker subgroups. This offers finer isolation
   but needs actual controller delegation, namespace reachability and migration
   permission that have not been demonstrated. Do not enable privileged mode,
   expose a runtime socket or change host cgroup security to obtain them.
3. Use per-process address-space limits. They are simple but do not aggregate
   descendants and can constrain mapped libraries before useful work begins.
   They are not a substitute for the proposed process-tree cap.

## Proposal and required decision

Recommend reviewing option 1 first. It uses one image, one Compose service and
ordinary Docker limits, without an application cgroup manager or native build
framework. Use distribution decoder binaries only after exact security and
licensing review. The source evidence, reservations, exclusions, cancellation
ownership, initial Q1-Q4 proof and later L1-L6 application qualification are in
[Linux feasibility](../linux-feasibility.md).

**Concrete maintainer question:** may this shared group be the whole-worker
accounting unit, with the full cap charged to each playing server, and may
runtime placement before process start satisfy the containment requirement
instead of delegating cgroup creation/migration to the non-root application?

Recommendation: approve only that interpretation and the reviewed experiment's
scope if its tradeoffs are acceptable. Retain the existing byte limits and
require permission, aggregate PCM, useful-fit and release evidence before
accepting the architecture. Alternatively, retain the literal application
delegation requirement and leave #7 blocked pending a supported deployment
design. Neither disposition has been recorded here.

This proposal does not itself change requirements, grant execution permission,
clear decoder vulnerabilities or authorize restricted-source access. Native PCM
buffering and exact decoder redistribution/source delivery remain explicit
acceptance gates. The documentation can be reviewed while these remain open.

## Consequences and fallback

The full container cap includes parent/SDK/voice overhead and may be too small.
All stations share an OOM failure domain. Global producer/bank leases bound
admission; the first experiment qualifies one active server with replacement,
not a scalable multi-server performance claim. Cleanup debt retains its owners
and slots until actual release, even after the original three-second deadline.

If useful fit fails, the sole fallback proposed for a later maintainer decision
is the same design with a 268,435,456-byte group cap: controlled capacity would
be 277,237,760 bytes. This changes the product guarantee and is **unmeasured and
unauthorized**. Present measurements from a separately authorized finite run
before adopting it. A permission failure cannot be fixed by raising memory.

## Acceptance evidence still required

- Independent review of the concrete plan and later qualification harness.
- Maintainer disposition of the accounting/delegation question.
- Cleared exact binary dependencies and source-access/compliance path.
- Non-root/default-seccomp runtime authority and containment before input.
- Aggregate PCM bounds, bounded copies and successful useful workloads.
- Deterministic cancellation/debt tests and actual resource release.
- Native amd64 and native arm64 results tied to the release source and image.

The initial proof uses a small process runner and actual decoder, without
creating a station/player/provider framework. Later integration evidence is
collected at the real application boundaries; it does not force speculative
application work before the eligible-recording end-to-end slice.

No item above is claimed complete by this record. A later accepted record must
include the decisions and evidence; accepted decisions are superseded by new
records rather than rewritten.
