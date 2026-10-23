# ADR-0011: Isolate non-PCM audio in a bounded Windows worker

- **Status:** Proposed; accepted on maintainer-approved merge
- **Date:** 2026-10-23
- **Issue:** [#242](https://github.com/slackysoba/sobafm/issues/242), parent [#175](https://github.com/slackysoba/sobafm/issues/175)
- **Authority:** [maintainer approval](https://github.com/slackysoba/sobafm/issues/175#issuecomment-6365997057)

## Decision

Place each server's non-PCM Lyria SDK/TLS/model processing and catalog HTTP/decoder
work inside one Windows worker and its descendants. Reuse the existing suspended
creation-time Job backend, with both process and whole-Job private-commit limits
enforced before execution. Charge the whole enforced cap without subtracting a
runtime baseline. Do not put the bot or other servers in that Job.

Keep admitted PCM in the parent, including buffered playback after producer
failure. Preserve both generated sessions, spares, fades, catalog behavior and
the aggregate PCM maximum of 34,560,000 bytes. Keep the strict 70,000,000-byte
total and the original single three-second cleanup verdict. A timed-out or
partially closed owner retains its debt until actual release; replacing a station
does not create a new allowance or reset the deadline.

Before production admission, account for bounded IPC allocations, simultaneous
copies, kernel queues, parent/player/shared audio storage and overlap outside the
Job. Unrelated shared heap, code and schema storage need no artificial charge.
Residual arithmetic supplies no missing physical bound. This decision changes
neither the SDK version nor existing decoder defaults, trust or provider rights.

## Finite implementation sequence

First test a necessary condition before undertaking the larger composition change.
Even with every outside charge optimistically omitted, retaining the permitted
PCM leaves at most **35,439,999 bytes** for the whole worker Job. This number is an
upper ceiling for one bootstrap check, not a reservation or eligible profile.

The initial source slice provides an actual isolated Python entrypoint importing
the installed SDK and catalog dependencies and converting one fixed fake audio
message through the SDK's bounded receive path. It reuses creation-time Job
assignment and owned pipes. It opens no provider, HTTP, socket or decoder and
does not read credentials. The [finite control](../worker-fit-check.md) is prepared
and independently reviewed before separately authorized native execution.

If the bootstrap cannot operate at this ceiling with attributable cap-failure
evidence, the proposed current-runtime configuration is infeasible. Stop with
that concrete result; do not try other versions, budgets or repeated profiles.
An unrelated failure or an unproved cause is inconclusive, also stops this finite
case, and must not be described as proof that the cap caused it.

If it succeeds, continue the same issue's bounded IPC and actual per-server
composition with complete outside accounting and finite qualification. Success
of this small check alone cannot establish useful full-load operation, include
two retained sessions, qualify decoder descendants, or enable production.

## Consequences and acceptance

Whole-Job enforcement replaces an unprovable aggregate of individual non-PCM
allocator capacities with a conservative charged component. It also charges
runtime overhead, so the architecture may fail the available allowance before
audio work begins. The initial check intentionally answers that prerequisite
without spending effort on a broad rewrite first.

This first slice is preparation only. No production composition or readiness
toggle is changed. Issue #242 remains open until the complete integrated outcome
or the authorized finite infeasibility disposition is recorded. Accepted prior
ADRs remain unchanged; their storage, ownership and behavior obligations remain
in force except for the explicitly approved worker placement above.
