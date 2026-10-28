# Q1-Q2 runtime placement and enforcement proof

This is the small first part of [issue #7](https://github.com/slackysoba/sobafm/issues/7).
The [experimental interpretation](../../docs/decisions/0001-linux-resource-proposal.md)
is approved. The runner/configuration received
[independent review](https://github.com/slackysoba/sobafm/pull/10#pullrequestreview-5666254143)
before the recorded execution. It tests ordinary Docker placement and enforcement, not the eventual
application entrypoint/image or decoder fit. It adds no application modules,
dependencies, provider adapter or station framework.

## Current boundary

- Q1 and Q2 passed once each on native amd64 on 2026-10-27, as recorded below.
- Q3 is partially exercised. The pinned mpg123 path has a
  [source-derived address-space scope and PCM reconciliation](mpg123-proposal.md#source-derived-scope-of-the-candidate),
  verified native input gates and one passing
  [owned MP3 confirmation](mpg123-fixture-plan.md#corrected-confirmation) on amd64.
  This covers the exact command and mapped/backing scope, with AS 12,524,288
  per admitted native address space and the full parent PCM/copy reservation.
  Other formats/rates/channels and actual two-decoder overlap remain unqualified.
  The [overlap preparation](mpg123-overlap-plan.md) reuses the retained fixture.
- Q4 is not executed or waived. Decoder cancellation follows the unresolved Q3
  gate. Helper-process reaping here does not establish decoder cleanup behavior.
- Every concrete native case needs independent review. Stop after its first
  failure and preserve the observation before preparing a correction; no case
  authorizes an automatic retry or the larger-cap fallback.

The current conditional PCM arithmetic is
`2 * 12,524,288 + 8,802,304 = 33,850,880` bytes. The first owned case exercised
one decoder, while reserving two address spaces. Remaining Q3 work must exercise
both admitted processes and required formats without violating the mapped/backing
scope, payload ownership or useful-fit criteria. The full 90,177,536-byte group
cap remains a separate controlled-capacity boundary; it does not itself establish
the smaller PCM bound.

The original Q1/Q2 stage below could not establish native decoder PCM capacity.
The later fixed FFmpeg DATA/STACK startup candidate was
[rejected at loader startup](decoder-supply.md); `-max_alloc` and sampled peaks
were never aggregate PCM proofs. Those historical results remain unchanged.
The subsequent mpg123 evidence does not waive source-access restrictions or
remaining qualification cases and introduces no custom decoder distribution.

## Exact first-stage candidate

Use the already present amd64 image, without a pull or build:

```text
ghcr.io/astral-sh/uv@sha256:8f4ab6a0f2cc074290ba6a136eb016350b2b6b93a2108e07b72724901ad1914f
```

Read-only image inspection identifies `amd64` and that digest. The probe uses
only its Python standard library. It acquires and runs no decoder, media,
restricted source, or external network input. No image is published. Therefore
decoder redistribution/source-delivery arrangements do not block this first
stage. Exact later binary licensing/security assessment remains necessary for
local decoder testing; publication and untrusted-input clearance remain distinct.
The retained `ffmpeg` candidate is Debian `7:7.1.5-0+deb13u1`, not a qualified
integration and not part of this first-stage image.

## Recorded amd64 result

The [selected machine-readable evidence](results/2026-10-27-amd64.yaml) binds this
pass to source `a708fa370e68cefeb5de22ce4c0e54e77673ee56`, the image digest below,
and mounted probe SHA256
`8fb662183acae70b4decaa5478c40e58c407a7a6c9c60c09e5c24d9e407963de`.
The hash covers actual mounted bytes, including checkout line endings.

| Case | Attempts | Result | Evidence |
| --- | --- | --- | --- |
| Q1 | 1 | Pass for runtime placement | Parent, child and grandchild had UID/GID 1000, seccomp mode 2, zero effective capabilities and the same private cgroup; exact limits were visible before release. |
| Q2 | 1 | Pass for deliberate enforcement | `memory.events` changed from zero to `max=162`, `oom=7`, `oom_kill=1`; zero swap and successful final release were observed. |
| Q3 | 0 | Not run; unresolved | No established native decoder PCM capacity bound. |
| Q4 | 0 | Not run | The Q3 gate precedes decoder cancellation; helper cleanup is not a substitute. |

The target was a Linux amd64 Docker engine 29.8.0, kernel
`6.18.33.2-microsoft-standard-WSL2`, cgroup v2/cgroupfs and builtin seccomp;
the image ran Python 3.14.7. The editor-host supervisor used the existing desktop
Docker client with eight-second management calls, a 40-second attached-call
bound and a 180-second total case deadline. It preserved the reviewed controls
without changing host settings. No pull, build or fixture generation occurred.

Both probes exited zero, reported the leader reaped with no retained descendants,
and had stopped container state with PID zero. Q2's runtime OOM flag was true
while its observer survived to report the expected enforcement result. Ownership
was reverified, evidence captured, and only the two stopped proof containers
were removed normally; their absence was verified. Cleanup debt was zero.
Whole host cases took 3.021 and 2.756 seconds, including management and removal;
these are not isolated cleanup-latency measurements. Private receipts occupied
28,080 bytes before retaining the small host supervisor; Docker storage totals
were unchanged at reported precision. No fixture or proof container remains.

This pass establishes neither eventual application entrypoint/image behavior,
useful decoder fit, aggregate native PCM, Q4 cancellation, native arm64 containment
nor release readiness. The 98,979,840-byte ledger is unchanged and remains
arithmetic until useful workloads honor all reservations. No rerun was needed.

## Reviewed invocation

From the repository root on a native amd64 Docker host, the intended invocation
for Q1 is below. This text is not an execution receipt. Confirm the exact source
commit, a clean tree, image digest, native host/engine architecture and builtin
seccomp state immediately before use. Record those facts beside the probe's own
source hash. No private host paths belong in public evidence.

```sh
revision=$(git rev-parse HEAD)
test -z "$(git status --porcelain)"
timeout --signal=TERM --kill-after=2s 8s docker container inspect \
  --format '{{.Id}}' sobafm-q7-q1
# Continue only when this reports that the name does not exist, while the
# Docker connection itself has already passed a bounded read-only check.
id=$(timeout --signal=TERM --kill-after=2s 8s \
  docker create --name sobafm-q7-q1 --pull=never --init --restart=no \
  --label org.sobafm.project=sobafm --label org.sobafm.issue=7 \
  --label "org.sobafm.source=$revision" --label org.sobafm.case=q1 \
  --user 1000:1000 --cap-drop=ALL --security-opt=no-new-privileges=true \
  --network=none --read-only --cgroupns=private \
  --memory=90177536 --memory-swap=90177536 --pids-limit=64 --cpus=2 \
  --log-driver=local --log-opt=max-size=1m --log-opt=max-file=2 \
  --mount "type=bind,source=$PWD/evaluations/linux_resource,target=/probe,readonly" \
  --workdir=/probe \
  ghcr.io/astral-sh/uv@sha256:8f4ab6a0f2cc074290ba6a136eb016350b2b6b93a2108e07b72724901ad1914f \
  python -I -B /probe/probe.py q1 --execute-owned-probe)
```

Q2 uses the identical configuration with container name `sobafm-q7-q2` and case
`q2`. Do not run Q2 unless Q1 passes. Do not add a seccomp override, host mount,
runtime socket, privilege, environment file, extra capabilities or writable root.
The default seccomp selection must be corroborated by daemon security information
and container configuration; `Seccomp: 2` alone only establishes filtering.

Before start, save the returned full container ID and inspect only its ID,
name, image and four ownership labels. Require exact agreement with this case,
project, issue and source revision. An existing name is never reused or removed.
If creation times out, inspect the expected name read-only to recover its ID
only when all ownership fields match; record the uncertain outcome and stop the
pass. Never infer ownership from the name alone.

The host supervisor then follows this finite procedure for each case. These are
explicit operational steps, not an invitation to run the probe unsupervised:

1. Create a new project-owned evidence directory under `.local/`, preserving the
   source/host/image receipt and verified container ID. Set a 180-second outer
   case deadline, including creation, evidence capture and cleanup. Wrap every
   Docker management/read call in `timeout --signal=TERM --kill-after=2s 8s`
   (at most ten seconds); use the shorter remaining case budget if necessary.
   The longer attach/start call below leaves time for the fixed evidence and
   cleanup steps. A timeout ends testing and enters owned cleanup, never Q2.
2. Attach/start that verified ID with `timeout --signal=TERM --kill-after=3s 40s
   docker start --attach "$id"`, recording the exit status and bounded stdout.
   The timeout bounds the client, not proof that the container stopped.
3. Capture safe state/exit/OOM fields and at most the configured two 1 MB log
   files before cleanup, using that ID. Retain failure, timeout and partial
   output. Missing final result or a nonzero probe exit fails/incompletes the
   case; never continue Q2 after Q1 failure.
4. Reverify ownership. If still running, use a bounded `docker stop --time=3
   "$id"`; inspect again for stopped state and PID zero. A failed stop or
   uncertain state is retained cleanup debt and ends the pass. Do not claim
   process release from the stop request alone.
5. After evidence is captured and stopped state is confirmed, reverify ownership
   and remove only that new ID with `docker rm "$id"` (no force). Verify the ID
   is absent with a bounded container listing. If evidence capture failed, stop
   the positively owned container but retain it for inspection rather than
   removing it. Failed removal remains documented cleanup debt.

Do not use `--rm`: Q2 may kill the observer, so external evidence must survive.
Coordinator authorization covers stopping/removing these newly created,
positively identified proof containers; no separate routine cleanup approval is
needed. No other container, image, volume or cache is touched. Limit the entire
pass to 30 minutes and results to 20 MB. Do not retry or tune after a failure.

## Observations and verdict

Q1 checks the exact effective memory, swap, PID and CPU settings, a read-only
reachable cgroup namespace, non-root UID/GID and no effective capabilities. A child
and grandchild report their own membership/security state and wait on an explicit
pipe gate. They receive the release byte only after those identities validate.
Their normal exit and reaping must complete before the result is accepted.

Q2 uses the same handshake, then each helper attempts at most 60,000,000 bytes of
non-audio allocation, touching each page, with a ten-second pressure deadline.
An increase in the cgroup OOM-kill counter is expected enforcement evidence.
The owner terminates remaining helpers, retains process identities until actual
release, and reports any cleanup debt. A failed release fails the pass even if
enforcement was observed. Bounded event/reaping polling is not a sleep-based
attempt to induce a race. This is not Q4's deterministic decoder cancellation
schedule and makes no station cleanup claim.

If the observer is OOM-killed, preserve its preflight/handshake log plus the
runtime's stopped-container state, OOM flag, exit code, exact limits and image.
That can show enforcement, but absence of the final in-container event/release
receipt makes the automated pass **incomplete**, not a silent pass. Do not infer
reaping from a kill signal or timeout. Q2's deliberate OOM is not a Q3 useful
workload OOM, which would be a failure.

The ledger remains 90,177,536 group + 4,608,000 PCM + 4,194,304 copies =
98,979,840 controlled bytes. Q1/Q2 allocate no PCM bank or audio payload; their
success establishes neither useful fit under all prepaid reservations nor the
separate aggregate native PCM condition. Reports keep Q3/Q4 `not_run` explicit.

Capture only these external fields: source commit/probe hash, image digest,
native architecture/kernel/runtime versions, builtin seccomp evidence,
memory/swap/CPU/PID settings, state/exit/OOM information and bounded probe JSON.
Never export full inspect/environment output, credentials or arbitrary host data.

## Offline validation

The contract tests exercise fail-closed parsing and descendant security checks
without spawning processes or using Docker. Ordinary pytest collection names
this one offline test file explicitly; it does not collect native evaluations.
They can also be run independently:

```sh
uv run python -m unittest evaluations.linux_resource.test_contract
```

Strict Pyright includes this directory. The required locked sync, all-file
pre-commit and ordinary pytest suite still run before every push. Passing these
checks cannot establish kernel containment or any Q1-Q4 execution result.
