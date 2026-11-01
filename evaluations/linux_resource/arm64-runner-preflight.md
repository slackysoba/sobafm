# Read-only native arm64 runner preflight

The [Ready scope on issue #7](https://github.com/slackysoba/sobafm/issues/7#issuecomment-6552785594)
authorizes this implementation. The workflow has **not been dispatched** and
the runner remains unqualified for Docker/native decoder work. Existing arm64
offline CI is evidence for the tooling/tests only.

The separate `Read-only arm64 runner preflight` workflow runs only through
`workflow_dispatch`, on the existing `ubuntu-24.04-arm` capability. Its sole
action is the existing commit-pinned checkout with read-only repository
permissions and no persisted credentials. It checks out `github.sha`; the
inspection script independently requires that full revision and a clean tracked
tree. No caller-provided command or source expression is evaluated by a shell.

Before one future dispatch, the coordinator reviews the exact source/workflow
and releases that read-only operation. This change does not release a dispatch.
There are no automatic PR/push/scheduled triggers or retries. Concurrency does
not cancel an already running preflight. Keep the existing amd64 Q4 packet
frozen; this observation does not replace its Q3/interpreter-identity gates.

## Observations

The script requires an already installed Python 3.14 and uses only its standard
library. It installs nothing and starts no container, image, decoder or media
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

If `python3.14` is absent, the workflow emits a small `unqualified` record with
tool absence and bounded architecture/kernel/page-size observations from existing
`uname`/`getconf`. Each fallback command has a three-second timeout and one-second
kill allowance. This is a minimal failure record, not a second implementation.
It makes no cleanup or decoder-feasibility claim and installs no interpreter.

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

The workflow's three-minute timeout is an outer stop, not evidence of cleanup.
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
