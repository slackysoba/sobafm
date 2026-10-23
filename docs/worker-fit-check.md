# Windows worker bootstrap fit check

This is the first finite necessary-condition check for [#242](https://github.com/slackysoba/sobafm/issues/242).
It is **preparation only** until a different reviewer approves the exact source
and manifest and the orchestrator separately clears execution.

## Fixed scope

- One new owned Python worker, one creation-time Job, one run; no descendants,
  provider, decoder, sockets, authentication, host inspection or old fixtures.
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
The runner stays alive if it still owns late cleanup after reporting a verdict.

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
