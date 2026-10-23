# Windows worker bootstrap fit check

This is the first finite necessary-condition check for [#242](https://github.com/slackysoba/sobafm/issues/242).
It is **preparation only** until a different reviewer approves the exact source
and manifest and the orchestrator separately clears execution.

## Fixed scope

- One explicit Python launcher and its contained interpreter worker tree, one
  creation-time Job, one run. The virtual-environment launcher may redirect to
  an interpreter descendant; the whole Job is charged. No provider, decoder,
  sockets, authentication, host inspection or old fixtures.
- Process and Job private-commit ceiling: 35,439,999 bytes, with no subtraction.
  This is `70,000,000 - 34,560,000 - 1`, optimistically omitting every outside
  allocation. It is not a qualified audio reservation.
- Isolated Python mode, no bytecode writes, the current locked installed SDK .3,
  actual catalog imports, and one owned fake bounded SDK receive producing a
  single 3,840-byte PCM frame. No dynamic instructions or provider input.
- The inherited child environment contains only SystemRoot and System32 PATH.
  Input is one fixed byte followed by EOF. Output accepts at most 3,842 bytes;
  diagnostics accept at most 4,096 bytes and never appear in evidence.
- One 15-second useful-path fence, followed by the original single three-second
  cleanup verdict. Failure never becomes success after that verdict. Ownership
  continues until setup, readers, writers, exit, Job and storage actually retire.
  A still-running cleanup must be preserved, not killed or retried.
- A shared **512-operation** budget is charged before every explicit Job/Kernel32
  function invocation, including nested queries/closes and termination fallback.
  It also charges controlled pipe adoption/write/close submissions, reads and
  close checks. Default production callers have no new guard or changed limits.
- Cumulative stdout/stderr read requests, including useful-path reads and cleanup
  drains, reserve their requested size before forwarding against **65,536 bytes**.
  Unused request bytes are not refunded. No drain can extend this budget.
- Cleanup requests termination once and permits at most **64** asynchronous close
  checks within the shared operation budget. These are finite test controls,
  not measured requirements or memory allowances. Exhaustion or unresolved
  operation failure parks on an unset event with all owners/tasks/handles/storage
  retained; it does not poll or retry native work. Already pending operations
  remain owned. A parked case has no automatic resume or replacement path.

## Preparation and execution

Use the task-owned signed Python environment. `scripts/check_worker_fit.py prepare
<absolute-manifest-path>` creates a new manifest containing source, lock,
interpreter, DLL, virtual environment and installed distribution identities.
Preparation creates no Job or worker. Preserve the manifest and exact source
commit for independent pre-execution review.

Only after separate clearance, `scripts/check_worker_fit.py execute
<same-manifest-path>` verifies those identities and exclusively creates a
`.consumed` receipt before launching. The receipt prevents an automatic repeat.
Do not remove or rename it to rerun. The `.result.json` contains fixed categories,
exit code and release state; no raw errors, PCM, credentials or diagnostics.
The runner joins actual retirement independently of the useful-work task. Repeated
caller cancellation and result-file creation/write failure cannot take unresolved
ownership into event-loop shutdown. A reporting failure is raised with fixed text
only after actual retirement; a parked case keeps the runner alive indefinitely.

## Interpretation and stop conditions

`useful-path` requires the exact fake PCM, zero exit and actual storage release.
It only passes this necessary condition. It proves no complete IPC/kernel/player
envelope, physical parent capacity, full workload viability or production readiness.

`allocation-failed` means the worker explicitly caught MemoryError; it does not
alone prove the Job limit caused that error. `bootstrap-failed`,
`exit-inconclusive`, `launch-or-io-failed` and `run-timeout` likewise do not identify
the cap as their cause. This control records `cap_cause_proven: false`: it has no
memory-limit event collector and must not infer such an event from an exit code.
`cleanup-failed` or `cleanup-timeout` retains the owner and a sticky failed verdict,
even if later cleanup succeeds.

All non-success outcomes stop this one case and preserve its evidence. There is
no fallback, alternate cap, version change, automatic retry or follow-on study
within this control. The orchestrator records the concrete outcome, distinguishing
infeasibility supported by attributable evidence from an inconclusive run.

## Consumed a2 result and source-only diagnostic correction

The separately cleared a2 run returned `launch-or-io-failed`, a null exit code,
actual storage release and `cap_cause_proven: false`. The original generic catch
does not identify which setup or I/O boundary failed. Retained evidence therefore
does not establish a concrete source fault or show that a larger budget would
help. The active 70 MB ceiling is unchanged; conditional authority up to 100 MB
does not apply without that evidence.

The schema-2 diagnostic control adds only the first fixed `failure_phase` and
`error_category`, plus a bounded `win32_error` integer if already present on a
caught OSError. It performs no additional native query and records no raw error,
exception name, diagnostic stream or audio. Setup distinguishes Job creation,
limit setting/readback, each pipe, attributes, process creation/capture and pipe
adoption; subsequent failures distinguish bind/resume, request write/close, reads
and the existing waiter. Later cleanup errors do not replace the first fault.
An exit code returned by an already owned waiter survives an unrelated I/O
failure; a known-absent setup sentinel is not presented as a child exit.

This is a distinct diagnostic control identity, not permission to rerun a2.
All prior consumed receipts and results remain immutable. Any subsequent native
run requires independent review of its exact source/manifest and separate root
clearance. No cap attribution, production readiness or broader worker integration
follows from this source-only correction.

## Ceiling readback correction

The consumed a3 run stopped at `job-verify` with category `capacity`, before
pipe or process creation. It did not capture the returned limits or query result;
it establishes neither a rounding cause nor cap-caused allocation failure.

Both initial setup and pre-resume membership verification accept each returned
memory limit only when it is positive and no greater than the requested ceiling.
The successful query, exact limit flags and actual Job membership remain required.
This follows the [current-limit query contract](https://learn.microsoft.com/en-us/windows/win32/api/jobapi2/nf-jobapi2-queryinformationjobobject)
and [committed-memory limit definitions](https://learn.microsoft.com/en-us/windows/win32/api/winnt/ns-winnt-jobobject_extended_limit_information):
a tighter returned limit preserves the ceiling. No page size, rounding direction,
extra query or larger allowance is assumed. The correction does not establish the
actual a3 failure cause. Its distinct manifest requires fresh independent review
and separate execution clearance; a2 and a3 remain consumed.

## Pipe-stage diagnostic correction

The separately cleared a4 run passed Job verification and stopped at
`pipe-stdin` / `capacity`, before target creation. It did not establish which pipe
operation or readback predicate failed, or show insufficient worker memory.
A4 remains consumed. The schema-3 control adds a fixed `pipe` record to the first
failure, covering stdin, stdout and stderr: `server-create`, `client-open`,
`event-create`, `connect`, `event-close`, `info-query` or `info-validate`.
Failed calls can include a bounded Win32 error from ctypes' existing thread-local
last-error copy; no additional native query is performed. Successful existing
pipe-info queries can include the four numeric flags, outgoing/incoming quotas
and instance count. Failed queries do not publish partial output fields.
Only unsigned 32-bit integer diagnostics survive validation. No handles, names,
raw errors or streams enter the result. All call counts, guards, quota predicates,
ownership and cleanup remain unchanged. This distinct manifest requires its own
independent review and separate root clearance; it permits no replay of a4.

## Demonstrated a5 pipe-flags correction

The consumed a5 run returned `pipe-stdin` / `info-validate` / `capacity` with
flags `9`, outgoing and incoming quotas `16384`, and instance count `1`.
Every quota predicate passed; the expected-flags comparison against `1` caused
the rejection before target creation. Creation already requests remote-client
rejection (`8`), in addition to the server-end flag (`1`). Microsoft's
[pipe creation contract](https://learn.microsoft.com/en-us/windows/win32/api/namedpipeapi/nf-namedpipeapi-createnamedpipew)
defines that option, and its [pipe conformance test](https://learn.microsoft.com/en-us/windows-hardware/test/hlk/testref/e3bcbd3f-3e9c-484a-a587-3c081cb28f7a)
explicitly checks that pipe-info readback includes it. The corrected predicate
requires exact flags `9`; missing server/remote-rejection flags, message mode or
unknown bits still fail. Fakes now return the observed valid flags. Creation mode,
quotas, calls, budgets and ownership are unchanged. This source fault provides no
evidence of insufficient memory. A5 remains consumed; a new manifest requires
independent review and separate root clearance before execution.
