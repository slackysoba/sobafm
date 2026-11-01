# Read-only native arm64 runner preflight

The [first released run](https://github.com/slackysoba/sobafm/actions/runs/41271130954)
on `71512ddc69ffa0f0a73810659314cb46ef259a03` returned `unqualified` because
Python 3.14 was absent from the runner's initial PATH. Its fallback observed
aarch64, kernel `7.0.0-1017-azure` and 4,096-byte pages; Docker inspection did not
run. The [next Ready scope](https://github.com/slackysoba/sobafm/issues/7#issuecomment-6553143879)
permits preparation of the existing toolchain setup route. This revised workflow
has **not been dispatched**. The runner remains unqualified for native decoder
work; ordinary arm64 CI establishes tooling/tests only.

The merged setup preparation at `d72b22745256226b9b11a68ce93dcd67991e6c26`
was rejected before any job started in
[run 41273534125](https://github.com/slackysoba/sobafm/actions/runs/41273534125).
GitHub reported `Unrecognized named-value: 'runner'` for `runner.temp` at line 18,
column 23 and `runner.tool_cache` at line 20, column 29. The same two errors occurred
on the reviewed branch in
[run 41272742489](https://github.com/slackysoba/sobafm/actions/runs/41272742489),
despite ordinary CI passing. Neither failure was a setup dispatch.

[GitHub's context availability table](https://docs.github.com/en/actions/reference/workflows-and-actions/contexts#context-availability)
allows `runner` in step environments, but not job environments. A running step
now resolves the same unique run/attempt paths, checks they are absolute and
contain no control characters, and appends them to `GITHUB_ENV` for later steps.
It does not read that environment file or override reserved runner variables.
Focused offline tests reject unavailable direct job-environment context references
and exercise path exports; they are not a complete GitHub workflow validator.
Review the new head's complete Actions run inventory, including push-triggered
zero-job failures, as well as ordinary CI before any later dispatch release.

The separate `Native arm64 runner preflight` workflow runs only through
`workflow_dispatch`, on the existing `ubuntu-24.04-arm` capability. It uses the
existing commit-pinned checkout and setup-uv actions, read-only repository
permissions and no persisted checkout credentials. It checks out `github.sha`; the
inspection script independently requires that full revision and a clean tracked
tree. No caller-provided command or source expression is evaluated by a shell.

Before one future dispatch, the coordinator reviews the exact source/workflow
and releases that setup plus read-only observation. This change releases neither
setup nor dispatch. There are no automatic PR/push/scheduled triggers or workflow
retries. Concurrency does
not cancel an already running preflight. Keep the existing amd64 Q4 packet
frozen; this observation does not replace its Q3/interpreter-identity gates.

## Missing-interpreter setup

The workflow pins uv **0.13.6** and the exact CPython request
`cpython-3.14.10-linux-aarch64-gnu`, as observed in passing
[main arm64 CI job 124743743364](https://github.com/slackysoba/sobafm/actions/runs/41271056576/job/124743743364).
The existing setup-uv revision is
`07a047e608db84b3d8c06cd9a7c7dcc9c355c18e`. No Python downgrade, application/dev
dependency synchronization, venv, new installer or container operation is added.
The observer's source and the frozen amd64 Q4 packet are unchanged.

Before setup, `arm64_preflight_setup.sh` requires at least **25,000,000,000**
available bytes at the checkout and **25,536,870,912** at the task-directory
parent: the existing free-space floor plus a **512 MiB** temporary setup
reservation. Each `df`/`du` observation has three seconds plus a one-second kill
allowance. The runner's tool-cache filesystem must also have the full free floor
plus setup reservation. These may be the same filesystem; free bytes are not
added. A fresh, exclusively created directory under the runner's temporary
directory contains uv-managed caches, Python installation, Python-install
temporary files and unused tool/bin locations. Its name includes the run ID and
attempt. Existing task directories are rejected; no shared directory is cleaned.

The [supported-path correction](https://github.com/slackysoba/sobafm/issues/7#issuecomment-6553292960)
uses the action's normal **runner-owned bootstrap locations**. The pinned action's
bundled tool-cache reads `RUNNER_TOOL_CACHE` for the uv installation and
`RUNNER_TEMP` for the archive and extracted copy. The
[runner 2.338.2 handler](https://github.com/actions/runner/blob/v2.338.2/src/Runner.Worker/Handlers/NodeScriptActionHandler.cs#L38-L52)
exports runner-context values after step environment values, so reserved-name
overrides would not relocate those files. No such ineffective override is used.
The action's
[documented environment controls](https://github.com/astral-sh/setup-uv/blob/07a047e608db84b3d8c06cd9a7c7dcc9c355c18e/docs/environment-and-tools.md)
keep Python/uv-managed caches/tools inside the task directory. The new uv
`uv/0.13.6` subtree is measured separately under the runner tool cache; if that
version subtree already exists, admission fails before modifying it. The
action's returned executable path must exactly match the expected
`uv/0.13.6/aarch64/uv` path before installation runs. Cache restoration,
saving, pruning, Python caching, environment activation and PATH changes are off.
The action receives no download token, uses quiet mode, and has no mirror fallback.

Setup-uv has a two-minute step timeout. Its finite internal transport retries are
distinct from a workflow retry. At this exact pin, the
[manifest reader](https://github.com/astral-sh/setup-uv/blob/07a047e608db84b3d8c06cd9a7c7dcc9c355c18e/src/download/manifest.ts)
allows three fetch attempts with one- and two-second delays; each fetch has a
[five-second abort signal](https://github.com/astral-sh/setup-uv/blob/07a047e608db84b3d8c06cd9a7c7dcc9c355c18e/src/utils/fetch.ts).
The [bundled tool-cache downloader](https://github.com/astral-sh/setup-uv/blob/07a047e608db84b3d8c06cd9a7c7dcc9c355c18e/dist/setup/index.cjs)
allows three artifact-transfer attempts with two random 10–20 second delays
(at most 40 seconds of backoff); its inner HTTP client disables further retries.
Thus the selected exact-version route permits at most three manifest attempts
plus three uv artifact-transfer attempts, subject to the outer step timeout.
HTTP redirects are part of each transfer, not another workflow invocation.

After uv setup and after Python installation, measure allocated disk bytes with
`du -s -B1` for the task directory and the specific new uv version subtree.
Hard links are counted once within each tree; cross-tree sharing is conservatively
counted in both. Charge those measurements plus a **134,217,728-byte (128 MiB)**
planning reservation for the action's unattributed runner-temp archive/extraction
files against the same **536,870,912-byte** setup limit. This reduces the
remaining allowance for measured tool/Python storage to **384 MiB**. The selected
record explicitly says `bootstrap_temp_usage: unmeasured`; the 128 MiB reservation
is not evidence of actual temporary usage or its peak. No runner-temp inventory,
credential-bearing command/environment file or unrelated cache content is read.
Observe filesystem availability at the task, checkout and tool-cache locations,
retaining the 25 GB free floor and remaining reservation at each admission point.
These are conservative admission/retention checkpoints,
**not hard peak disk or log quotas** around the maintained upstream action.
Unexpected growth, missing measurements or failed setup stop observer admission.
The workflow emits both measured byte counts, the temporary planning charge and
the total accounted amount, including an over-limit count. Unattributed temporary
growth and action peak usage remain explicit gaps for execution review; the free
space checkpoints do not establish a hard aggregate bootstrap bound.
On setup failure it attempts one final storage observation, without claiming
that an interrupted setup process has released its resources.

Python installation runs once under a clean environment with `--no-config`,
`--no-bin`, quiet/no-progress output and `UV_HTTP_RETRIES=0`. Its HTTP timeout is
20 seconds; the command has a 60-second timeout plus a three-second kill allowance
inside a two-minute step. Installer stdout/stderr go to `/dev/null`; only the
exit code, fixed failure classification and selected storage observations remain.
The setup action's own warnings/toolkit messages may still reach the job log;
quiet mode is not a hard upstream log cap. Retain at most **64,000 selected bytes**
across setup and observation evidence, within the existing 25 MB artifact reserve.
The 512 MiB combined runner/task tool allocation is development storage, not retained
artifacts or a change to runtime audio/RAM limits. Do not export tools, raw logs,
archives or caches. No cleanup step removes partially installed files or makes
assumptions about unknown processes; preserve failure/ownership uncertainty and
seek review before another operation. Runner disposal does not prove release.

## Observations

After successful setup, the script uses that exact Python path with isolated
mode and only its standard library. The observer installs nothing and starts no container, image, decoder or media
workload. The three allowed commands are fixed Git revision/status reads and
Docker `info` with an explicit selected-field template. Docker connects only to
the local Unix socket, with an absent dedicated configuration location; the
child environment is a literal small mapping with a nonexistent home, C locale
and system/global Git configuration disabled. No environment dump, real env
file, credential store, Docker context enumeration or full inspect is read.

Selected facts are:

- Actual machine, endianness, page size, kernel and Python version, plus the
  running interpreter's 64-byte ELF header architecture. The initial target
  requires Linux aarch64, little endian, Python 3.14 and 4,096-byte pages.
- Existing core routing, classified only as `core`, `pipe` or `other`; the
  current native harness requires exactly `core`. No raw handler path is emitted.
- Readability of `stat`, `status`, `maps`, `syscall`, `cgroup` and standard
  descriptors for self and each owned CLI. The engine CLI's visibility must be
  observed; an early exit/race or denied read is unqualified. Raw addresses,
  mappings, process text, hostnames and unrelated process inventories are omitted.
- Host cgroup v2/controller visibility and Docker's selected OS, architecture,
  kernel/version, cgroup driver/version, builtin-seccomp flag, rootless flag,
  memory/CPU counts and memory/swap/PID limit capabilities. Host and engine kernel
  and architecture must agree. No emulated target or amd64 image is qualified.
- Available filesystem bytes for the checkout and the engine's reported storage
  directory, limited to `/var/lib/docker` or `/mnt/docker`. Require at least the
  existing 25,000,000,000-byte free-space floor at both locations. These may share
  a filesystem, so their free space is not added. Permission denial or a different
  storage location is unqualified; no directory is created or permission changed.

These checks do not prove container UID, namespaces, effective limits, descendant
containment, in-container proc access, runtime memory fit or resource release.
Docker's capability flags are observations, not enforcement evidence. A target
with different legitimate defaults still needs a concrete reviewed assessment;
the script does not repair the host or silently broaden accepted values.

If admission/setup fails or the pinned interpreter path is absent, the workflow
emits a small `unqualified` record with `python_314_setup_unavailable` and bounded
architecture/kernel/page-size observations from existing
`uname`/`getconf`. Each fallback command has a three-second timeout and one-second
kill allowance. This is a minimal failure record, not a second implementation.
It makes no cleanup or decoder-feasibility claim and never retries setup.
Cancellation may prevent this fallback; a missing terminal record remains incomplete.

## Bounds and ownership

Each command has five seconds within one 30-second operation budget. Each proc
read is at most 65,536 bytes; core/controller reads are at most 4,096 bytes.
Captured command stdout and stderr share a 32,768-byte cap. Raw stderr is never
included in a result. Unexpected fields, duplicate JSON keys, nonmatching source,
missing tools/access, overflow or deadlines make the observation unqualified.

Each started CLI has one owner for its direct child, new process group and
original nonblocking output pipes. The first failure/cancellation or observed
leader exit begins that command's original three-second cleanup deadline.
Repeated cancellation cannot reset it. TERM and the one-second KILL escalation
are sent only while the unreaped direct child still anchors the owned group.
After leader reaping, an unexpected retained group remains debt; the operator
does not signal an unanchored reused group identifier. Failed/unknown waits and
uncertain closes likewise retain ownership. A failed close is not blindly retried.

No next command starts until actual direct-child wait, original pipe closure and
group absence. Late actual release clears debt but leaves the failed deadline
sticky. At 60 seconds the owner emits one bounded debt record and continues
retaining resources. Failed diagnostic writes or unexpected loop exceptions do
not unwind that owner. There is no automatic command replacement/retry.

The observer step's two-minute and workflow's eight-minute timeouts are outer
stops, not evidence of cleanup.
Cancellation, timeout, VM disposal or a missing terminal record never establishes
release. Preserve the failed/incomplete record and unresolved debt for review.

## Reading the result

The script emits one JSON terminal record of at most 32,768 bytes, with optional
one-time bounded cleanup-debt output before it. Retain at most 64,000 selected
bytes from the job logs within the existing artifact reservation; no artifact
upload action, raw PCM or new archive is produced.

`observed_prerequisites_match` means only the enumerated read-only observations
matched. `unqualified` identifies missing/unsuitable facts or failed operations.
Both retain `native_runtime_qualification: not_run`; the full script also states
`container_permissions_and_limits: not_tested`. Tool absence is not decoder
infeasibility. Any future arm64 image acquisition, installation, startup or Q1-Q4
case requires its own concrete binding/review and applicable release.

The focused offline tests cover wrong source/architecture/configuration,
untrusted/oversized output, unavailable permissions/tools, cancellation,
unreaped groups, unknown waits, delayed output completion, failed close and
broken diagnostics. They mock all command/process observations and never call
Docker or dispatch this workflow. Required repository checks remain locked sync,
all-file hooks/strict Pyright and full pytest before push.
Setup tests use local fake installers to cover admission failure, unknown storage,
exclusive directory creation, existing uv-cache refusal, bootstrap accounting,
storage growth, validated action paths, isolated inputs and failed installation
without retries or release claims. They perform no download or real installation.
