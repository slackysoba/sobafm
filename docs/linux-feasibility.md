# Linux resource feasibility proposal

Status: **experimental interpretation and Q1-Q4 scope approved; runtime unqualified**.
This is the bounded feasibility slice of
[issue #7](https://github.com/slackysoba/sobafm/issues/7), under
[Outcome 1](https://github.com/slackysoba/sobafm/issues/1). The
[requirements](requirements.md#approved-memory-definition) remain authoritative.
The associated [accepted experimental decision](decisions/0001-linux-resource-proposal.md)
records the approved interpretation and remaining execution gates.

## Result and evidence boundary

Evaluate one ordinary Docker container with a runtime-installed cgroup v2 memory
limit covering the parent, every producer, every decoder and all descendants.
Treat that entire limit as one shared whole-worker-group cap. Add explicit parent
PCM and payload-copy reservations without subtracting parent memory from the
group cap. This deliberately pays for some memory twice. It avoids an application
cgroup manager, a privileged launcher and a separate worker service.

The proposed controlled-capacity total is **98,979,840 decimal bytes**, below
100,000,000. That is arithmetic, not proof of useful fit. The maintainer
[approved the grouped-cap/runtime-placement interpretation and bounded proof](https://github.com/slackysoba/sobafm/issues/7#issuecomment-6439759990).
The [recorded Q1/Q2 pass](../evaluations/linux_resource/README.md#recorded-amd64-result)
demonstrates runtime placement, effective namespace reachability, inherited
descendant membership and deliberate memory enforcement for its exact amd64
image/source/host. Application cgroup delegation is not required for this
experiment. These results do not qualify the eventual application entrypoint,
decoder image, useful fit or aggregate PCM accounting.

Read-only diagnostics on 2026-10-27 established a Linux/x86_64 Docker engine with
cgroup v2 and the cgroupfs driver, plus an x86_64 Linux contributor environment
with Python 3.14.4. Subsequently Q1 and Q2 each passed once in the pinned image
with Python 3.14.7. Both disposable proof containers were stopped, removed and
verified absent after evidence capture; no cleanup debt remained. No decoder,
audio or provider ran. Native arm64 containment, useful memory fit, decoder and
application cancellation, audio quality and release behavior remain
**unmeasured**. Development-host evidence does not qualify a Linux release host.

## One candidate deployment

Use one image built from a pinned maintained Debian-based Python 3.14 image,
distribution decoder binaries and uv-locked application dependencies. Use one
Compose service, one `/data` volume, an init process, a nonzero UID/GID, the
built-in Docker seccomp profile and no added capabilities. Keep the Docker socket
and host cgroup filesystem out of the container. Do not enable privileged mode,
host PID/cgroup namespaces or custom security profiles.

The following are proposed configuration values, not a runnable deployment:

| Setting | Candidate value | Purpose |
| --- | --- | --- |
| Container memory limit | 90,177,536 bytes | Shared cap over the full service process tree |
| Memory plus swap limit | 90,177,536 bytes | No additional swap allowance |
| PID/task limit | 64 | Finite ceiling including threads; separate from producer admission |
| CPU limit | 2 CPUs | Fixed qualification envelope, not a latency guarantee |
| Application producer slots | 2 globally | Audible production and one replacement; retained work holds a slot |
| PCM banks | 2 globally, 2,304,000 bytes each | Twelve seconds per bank at 192,000 bytes/second |
| Parent audio/IPC copy pool | 4,194,304 bytes globally | All simultaneous payload copies described below |
| Readiness | 1,152,000 bytes, or verified shorter completion | Preserve the required six-second pre-roll |
| Cleanup deadline | 3 seconds from the original cancellation | One owner across all cleanup stages |
| Stop grace | 5 seconds | Allows the three-second cleanup verdict and process exit; does not extend cleanup |
| Container logs | 2 files of at most 1 MB | Safe metadata only; no media or secrets |

The group cap is 86 MiB expressed as 90,177,536 decimal bytes, aligned to 64 KiB
as well as smaller common page sizes. Verify the effective kernel value rather
than assuming no rounding. Explicit byte values are required, not ambiguous
`90m` or `100 MB` shorthand.
Docker documents memory limits and the equal memory/memory-plus-swap setting;
leaving swap unset can create an additional allowance.
[Docker resource limits](https://docs.docker.com/engine/containers/resource_constraints/)
and [Compose service settings](https://docs.docker.com/reference/compose-file/services/).

The qualification image must use the same non-root entrypoint and init path as
the intended application image. `USER` alone does not select a rootless daemon;
these are different properties. Retain the default seccomp profile and verify its
effective state rather than relaxing it to make a probe pass.
[Docker user configuration](https://docs.docker.com/engine/containers/run/#user),
[default seccomp](https://docs.docker.com/engine/security/seccomp/) and
[rootless mode](https://docs.docker.com/engine/security/rootless/).

### Containment before input

The Docker runtime creates and populates the limited cgroup before the
application entrypoint starts. Children inherit membership. No application
process migrates workers after they have parsed input. The startup gate reads
the effective cgroup path and limits before credentials, provider connections or
audio are admitted. Missing or mismatched limits fail closed. A process-wide
outer cap that is merely configured, unreadable or ignored is not evidence.
The OCI lifecycle requires environment creation before the configured program
runs; actual Docker behavior still needs the permission test below.
[OCI lifecycle](https://github.com/opencontainers/runtime-spec/blob/v1.3.0/runtime.md#lifecycle)
and [Linux resources](https://github.com/opencontainers/runtime-spec/blob/v1.3.0/config-linux.md#control-groups).

| Authority question | Proposed owner and required evidence |
| --- | --- |
| Delegation/controller availability | Existing Docker runtime owns the container cgroup. Record its driver and enabled memory/PID controllers. The accepted experimental interpretation does not require delegation to the application. Actual runtime authority remains to be demonstrated. |
| Namespace reachability | Non-root entrypoint resolves its own cgroup through `/proc/self/cgroup` and the mounted cgroup filesystem, reads effective limits, and correlates them with scoped runtime inspection. A private namespace may report `/`; do not infer the host path from it. |
| Placement/migration permission | The runtime must successfully place the entrypoint before start. A controlled child and grandchild must appear in the same limited domain. The app must not need permission to move itself or descendants out. |
| Permission to alter limits | The application has none. It must be unable to raise limits, create an escape subtree or use a runtime socket. Failure to observe this closes the execution gate. |

Linux documents hierarchy, child membership, cgroup write permissions and
delegation separately. `memory.max` can transiently be exceeded; shared pages
can be charged elsewhere, and moving a process does not move old charges. Thus
the configured cap is the accounting value, not a promise about instantaneous
RSS or all physical backing. Do not move live work between groups, reset a group
to hide retained charges, or use `memory.high` as the cap.
[Kernel cgroup v2 documentation](https://docs.kernel.org/admin-guide/cgroup-v2.html).

Per-process `RLIMIT_AS` is not the selected route: it covers address space,
including mappings, and is inherited as a separate limit by each child. It does
not give a process-tree aggregate cap. `RLIMIT_RSS`, sampled RSS, baseline
subtraction and library-specific single-allocation limits do not replace the
required guarantee.
[Linux resource limits](https://man7.org/linux/man-pages/man2/getrlimit.2.html).

## Capacity ownership and admission

Let `G` be the effective, reserved group cap, `P` prepaid parent PCM capacity and
`C` reserved parent audio/IPC payload-copy capacity. There is one global ledger
and one group; every station lease references it. Charge the **entire** `G` to
each playing server when assessing its bound, rather than dividing it by a
current station or worker count. Globally, reserve `G` once and partition the
fixed `P` and `C` pools among owners. Never create per-station copies of those
pools or a second worker group without a new capacity calculation.

```text
G = 90,177,536
P =  4,608,000 = 2 banks * 12 seconds * 192,000 bytes/second
C =  4,194,304
G + P + C = 98,979,840 < 100,000,000
P + C = 8,802,304 < 34,560,000 (conservative parent PCM-copy case)
```

The unused 1,020,160 bytes are not an allocatable residual pool. No worker is
budgeted as its sampled incremental memory. Parent interpreter, SDK and decoder
overhead must also fit inside `G` in this candidate, despite some being excluded
from the product's controlled-capacity definition. That extra constraint may
make this simple candidate impractical; no baseline is subtracted to rescue it.

`C` has explicit owners, not a generic allowance:

| Copy owner | Global reservation in bytes | Admission rule |
| --- | --- | --- |
| Two encoded/network message slots | 2,097,152 | At most 1,048,576 bytes each, reserved before receiving |
| Two IPC read/write slots | 1,048,576 | At most 524,288 bytes each, including retained writes |
| PCM conversion/mixing/callback scratch | 524,288 | Includes input/output copies concurrently retained by transformations |
| Cancellation drain/close payloads | 524,288 | Existing owner retains its reservation until actual completion |
| **Total** | **4,194,304** | No copying between owners without simultaneous credit |

These limits are proposed workload constraints, not assertions about an SDK's
buffering. Enumerate transport buffers, serialized/base64 data, decoded data,
decoder reads, frame slices and callback handoff copies during implementation.
If an SDK cannot bound a message before allocation or retains more copies than
the reservation, it fails qualification; do not move its audio payload into an
excluded-overhead category. Object overhead remains distinguishable from payload.

`P` covers every application-owned PCM bank, including prepared, fading, retired
and worker-side queued PCM if introduced. Workers stream output instead of
keeping an additional PCM reservoir. The calculation above bounds parent PCM and
copies only: decoder-internal PCM buffering still requires a documented bound
and an aggregate PCM reconciliation before acceptance. Charging native memory in
`G` does **not** by itself prove the separate 34,560,000-byte PCM condition.

Allocate banks and copy slots before starting work. The audible and replacement
owners can coexist in the two slots. At six seconds of verified playable PCM,
the replacement can become ready; the old bank remains charged until drained or
discarded and released. A generated crossfade reads both banks using prepaid
scratch. Catalog tracks switch successively, without a fade or overlap. Failed
preparation preserves the playable current bank.

This first useful-workload experiment is one playing server with replacement
overlap. It does not establish a one-server product restriction or multi-server
performance support. A second logical server tests global admission and must be
rejected or delayed before allocation when the two producer/bank leases are
occupied. Any later supported concurrency must reserve enough global banks for
each current track and its replacement; it cannot multiply this budget silently.

Use bounded candidate attempts (3), no more than one retry per candidate, a
15-second preparation deadline and no automatic spare producer. Admit no new
request while its needed slots are in cleanup debt. For this offline experiment,
limit an encoded input to 32,000,000 bytes and 240 seconds, read in at most
65,536-byte chunks, and cap stderr retention at 65,536 bytes per producer. These
are experiment bounds, not new catalog-duration or generated-program limits.
The later 60-minute application run uses successive finite inputs and never
buffers a full program; it is not part of the initial feasibility harness.

### Cancellation, release and failure domains

Each producer owns its process group, in-flight reads/writes, terminal response,
PCM bank and copy reservations. The first cancellation fixes one monotonic
three-second total deadline. Feeding, closing, bounded draining, TERM/KILL,
joining and reaping all share it. Repeated cancellation changes neither owner
nor deadline. Delivery can precede completion; retain the write until it joins.

Expiry records failed cleanup and keeps leases and debt. A signal, cancelled
await, exited leader or low RSS alone cannot release them. Verify descendants
are gone, original I/O has settled and references are released. Do not kill the
shared container to claim successful per-station cleanup. A whole-container OOM
can affect every station and the parent: that is a failure of this configuration
and a service recovery event, not successful isolated recovery. Preserve safe
runtime evidence outside the failed container; restart joins voice idle and
never resumes the previous program.

The product exclusions remain visible: parent allocator/object overhead, native
voice encoding/encryption/packet backing, kernel pipe/socket storage and
uncharged shared/mapped backing. They are not extra payload pools. Total physical
audio memory may exceed 100 MB. This design is resource containment, not a
complete security sandbox. Source storage limits and development disk budgets
are separate from this memory calculation.

## Decoder supply and source obligations

Documentation and package metadata were initially checked on 2026-10-27 without
acquiring packages. The subsequent [signed metadata result](../evaluations/linux_resource/results/2026-10-27-debian-metadata.yaml)
resolves 205 proposed binary additions on the pinned base. See the linked
[startup preparation](../evaluations/linux_resource/decoder-startup.md) for later
acquisition status. No source archive, audio fixture or restricted file was
acquired or hashed.

| Binary distribution path | Evidence and fit | Disposition |
| --- | --- | --- |
| Debian stable `ffmpeg`, `7:7.1.5-0+deb13u1` at review time | Distribution-maintained binaries exist for amd64 and arm64. Broad codec support avoids a custom native build. The published binary file lists contain tools, documentation and presets rather than the restricted source files. Dependencies and installed size are not a memory-fit measurement. | Exact binary set acquired and installed offline; binding reviewed. The fixed supplemental DATA/STACK candidate failed loader startup on amd64. |
| Debian `mpg123` family, all four packages `1.33.7-1` from forky | Upstream fixes relevant decoder paths missing from the older stable family. The authenticated amd64 proposal selects only these four packages from forky, with 23 stable additions and no base/libc change. | [Supply, binding and fixed no-input startup completed](../evaluations/linux_resource/mpg123-proposal.md) under unchanged limits. Useful decode and PCM qualification remain pending; MP3 evidence would not remove FLAC or other qualification requirements. |

Sources: [Debian FFmpeg package](https://packages.debian.org/trixie/ffmpeg),
[amd64 file list](https://packages.debian.org/trixie/amd64/ffmpeg/filelist),
[arm64 file list](https://packages.debian.org/trixie/arm64/ffmpeg/filelist),
[Debian mpg123](https://packages.debian.org/trixie/mpg123) and
[mpg123 upstream release/licensing information](https://www.mpg123.de/).

For the candidate, install only signed-distribution binary packages and required
runtime dependencies in the future image build. Pin the base digest, resolved
binary versions and architectures; record binary checksums, build configuration
and package source identities in the release inventory. Do not use source
packages, debug-source packages, a local FFmpeg build, an opaque wheel that
bundles an unidentified decoder, or a third-party static executable by default.
The executable should receive bounded pipe input, no credential-bearing URL,
allowlisted input formats and only required decoding/output options. Review the
exact command and protocol restrictions before execution.

FFmpeg's upstream license varies with enabled components; do not infer LGPL-only
status from the project name. Preserve applicable copyright/license notices and
identify the complete corresponding source, distribution patches and build
instructions for the exact shipped binaries and libraries. Calling an executable
does not remove obligations for redistributing it inside an image. A package
homepage or moving upstream link is not a completed source-delivery plan.
[FFmpeg licensing guidance](https://ffmpeg.org/legal.html).

The exact Debian copyright record was retrieved in the follow-up inspection:
default Debian binaries are GPL v2 or later; the extra libavcodec/libavfilter
flavors are effectively GPL v3 or later. Debian reports that its packages do not
use the unredistributable nonfree combinations. Exact transitive component
licensing remains part of the supply gate.
[Version-specific copyright](https://metadata.ftp-master.debian.org/changelogs//main/f/ffmpeg/ffmpeg_7.1.5-0%2Bdeb13u1_copyright).
Before redistribution, a maintainer
must approve and evidence a compliant source-delivery method, including who
retains and supplies matching source and notices. The prohibition on reading,
acquiring, hashing, reconstructing or building `fftools/ffmpeg_filter.c` and
`18.raw` remains absolute for this work. Do not download a source archive to
inspect or remove its forbidden members. If compliance requires a prohibited
action, stop and obtain a direct maintainer resolution; documentation approval
does not authorize it. Local binary evaluation and image publication are
separate gates, and neither is performed here.

Private local binary evaluation on owned synthetic inputs does not itself
redistribute an image. Assess the exact binary and any applicable local-use
obligations for that experiment; do not make a publication/source-delivery plan
a prerequisite when the action does not trigger it. The Q1/Q2 process-only
stage below uses no decoder and has no decoder-specific licensing gate.

The [Debian security tracker](https://security-tracker.debian.org/tracker/source-package/ffmpeg)
currently lists unresolved entries for this stable version, including deferred
fixes. Therefore this is a maintained-distribution candidate, **not a cleared
security baseline**. Triage the exact enabled parsers/protocols and transitive
libraries against that tracker and upstream advisories. An unresolved applicable
finding needs a supported fix or explicit maintainer security decision before
untrusted-source testing or release. Update through a reviewed image rebuild;
repeat affected qualification when a native component, runtime or dependency
changes. Python-only scanning is insufficient.

## Finite qualification plan

The experimental interpretation and Q1-Q4 scope are approved. The concrete
harness still needs independent review and its security, source-access and
storage gates cleared before containers, installations, fixture generation or
resource experiments run. Do not ask again for the recorded design approval.
Do not use production credentials, real providers or Discord in
the offline phases. There are two stages: an early decoder/process stop-or-go
proof and later application qualification. The first must not recreate a player,
provider adapter, station/credit model or generic IPC framework. Later schedules
belong at actual integration boundaries when those boundaries exist.

### Entry gates and finite bounds

1. Preserve the recorded maintainer approval of the experimental group
   accounting/runtime-placement interpretation. It does not make an unqualified
   configuration an application-wide dependency.
2. Integrate #8's executable foundation and run its locked sync, pre-commit and
   offline suite on the exact documentation/qualification commit. Independent
   review must be posted on the PR. Missing checks are not passes.
3. Review a small process/decoder harness and exact decoder command,
   dependency/security inventory,
   safe fixture provenance, and binary/source-access plan. Resolve applicable
   security exceptions before input reaches native code.
   Apply decoder-specific gates when the decoder is introduced; they do not
   prevent independent Q1/Q2 placement/enforcement evidence.
4. Record exact image digest, source commit, architecture, native host/kernel,
   engine/runtime versions and resource settings for each tested target. The
   available native amd64 target can run Q1-Q4 independently once its other gates
   pass. Native arm64 hardware and its own results remain a separate
   cross-platform/release gate; their absence does not block amd64 evidence.
   Emulation cannot qualify either native target. Before installation or image builds,
   measure storage and allocate an experiment allowance with the coordinator
   within the remaining approved 10 GB project budget and 500 MB artifact limit.
   Pause heavy work at 8 GB or below 25 GB free on the development drive. Only an
   increase or other material new commitment needs renewed maintainer approval.

Proposed initial execution ceiling: one candidate configuration, one image per
target, one sequential pass per native architecture, at most 30 minutes per
target and 20 MB safe evidence total. Permit one rerun only for a documented
harness error;
configuration or code fixes require a new reviewed evidence identity. Stop at
the first containment, source-access, security, accounting or cleanup-ownership
failure. Do not tune limits repeatedly until a run passes.

### Initial stop-or-go proof

The concrete [Q1/Q2 runner, configuration and results](../evaluations/linux_resource/README.md)
used the already present pinned amd64 Python image without decoder installation
or an image build. Q1/Q2 passed and the finite pass stopped. Q3 remains unresolved
because the focused permitted evidence pass did not establish a native PCM
capacity bound; Q4 remains not run behind the Q3 gate.

Use a small standard-library process runner, permitted local fixtures, pipes,
bounded buffers and a paced PCM discard sink. The runner observes the real
decoder processes and kernel limits; it does not simulate a finished bot.
The full group cap remains charged even when this early harness uses less of it.
An early pass permits consideration of the narrow eligible-recording end-to-end
slice. It does not establish that the eventual SDK, voice stack or station model
fits, and is not permission to depend on an unqualified configuration throughout
the application.

| Phase | Bounded case | Required result/evidence |
| --- | --- | --- |
| Q1: startup and authority | One launch, no media; one controlled child and grandchild | Nonzero UID/GID, default seccomp active, expected namespace and cgroup membership, effective memory/PID/swap settings before the first input gate. Runtime placement works; app cgroup mutation is unnecessary and unavailable. Missing visibility fails. |
| Q2: enforcement | One disposable process-tree allocation probe, up to 120,000,000 requested bytes or 10 seconds | Both descendants remain charged to the original domain; pressure/failure is explained by cgroup events. Verify swap stays disabled and no escape/unlimited subgroup exists. Preserve OOM/error evidence; do not demand a false instantaneous-RSS ceiling. |
| Q3: useful decoder fit | Three owned synthetic compressed fixtures, 120 seconds each: MP3 CBR, MP3 VBR and FLAC; stereo/mono and 44.1/48 kHz represented. One additional 30-second overlap of two decoder processes. | Incremental valid 48 kHz stereo s16 PCM, six-second readiness within 15 seconds, known-fixture completion, both banks/copy reservations honored, no OOM. Reconcile native PCM bounds. Hash only permitted fixtures. No station, speaker or Discord output. |
| Q4: process cancellation and release | Four controlled cases, once each: cancel during feed, cancel while stdout is backpressured, child ignores TERM, and descendant remains after leader exit. Repeat cancellation in each case. | One original three-second deadline; bounded drain, process-tree termination and reaping; release only after actual process and I/O completion. A failed deadline retains a slot/debt and blocks reuse. Use explicit synchronization, not race-inducing sleeps. |

Q3's ordinary owned synthetic fixture generation/encoding belongs to the reviewed
bounded harness scope and needs no separate routine approval once that scope is
authorized. Use cleared binaries within the allocated artifact allowance.
No fixture uses `18.raw`, a source catalog
download or a reconstructed restricted sample. Bound fixtures to 20 MB total;
retain provenance and compact results rather than decoded output.

Before Q3 can pass, reconcile aggregate PCM: application banks, PCM-bearing
copies and bounded native decoder PCM retention must together be at most
34,560,000. Record how decoder buffering is bounded for the selected formats;
peak sampling alone does not establish capacity. If primary documentation and
permitted inspection cannot establish that bound, record the gate as unresolved.

### Later application qualification

The following map the remaining requirements to their real implementation
boundaries. They are not prerequisites to implementing the initial proof or the
narrow eligible-recording end-to-end slice. Scope and review each when that
boundary exists; do not build parallel fake application infrastructure now.
Keep the same candidate and numeric contract unless a new decision changes it.

| Case | Actual boundary and finite workload | Required result |
| --- | --- | --- |
| L1: replacement and skip | Station/player, ten replacements and ten skips at fixed fixture offsets | Current audio survives preparation/failure where viable; successive catalog boundaries; credits follow the actual heard-source switch; overlap remains charged. |
| L2: generated buffering | Actual SDK/transport, ten bounded loopback PCM sessions if a safe local endpoint exists | Serialization/base64/PCM copies and renewal overlap fit. A plain fake cannot qualify SDK buffering; otherwise defer to authorized provider evaluation. |
| L3: malformed or stalled input | Actual acquisition/decoder boundary, once each: truncated, corrupt, oversized, stalled feed, stalled reader, stderr flood | Finite completion policy, bounded queues/copies, one terminal outcome and useful preservation of existing PCM. |
| L4: failure schedules | Actual dispatcher/parent reader/callback, pause at grant, delivery before write completion, read, close, exit and switch; cancel twice at each | One owner/outcome/deadline; no deadlock, stale grant or premature release; deliberately stuck cleanup retains debt. Deterministic synchronization. |
| L5: shared admission | Actual station allocator, two logical servers while all slots are owned and again with cleanup debt | Refuse/delay before allocation; no duplicated pool, spare or debt transfer. This proves accounting, not two-server throughput. |
| L6: continuity and restart | Actual application and PCM sink, one 60-minute catalog run with 20 replacements, then stop/restart | At most two seconds total underrun under the declared workload, normal-stop release, idle restart. Offline evidence does not establish Discord or music quality. |

L1-L6 require a later explicit execution scope, proposed at no more than two
hours per target and 30 MB safe results, within the same one-configuration
evaluation. They remain not-run when their application boundary is absent.

### Evidence to retain

Keep a compact result table keyed by Q1-Q4 or L1-L6 and target, with pass/fail/not-run,
case counts, input provenance, effective cap values, ledger high-water marks,
PCM capacity reconciliation, safe memory/PID/event summaries, readiness and
underrun timings, and cleanup verdict plus actual release time. Include failing
cases and execution limits. Never log audio, credentials, request text,
credential-bearing URLs, environment dumps or personal host paths.

Bind results to the exact commit, image digest, native target and dependency
manifest. A new image or native dependency invalidates affected fit evidence.
Default Docker permissions on one host do not prove another host's permissions.
An offline pass neither grants source rights nor establishes listening quality,
provider reliability, Discord behavior, or BOTH-mode release readiness.

## Decision if this candidate does not fit

Keep the application architecture unqualified until its gates pass. If the 90,177,536-byte
whole-service cap cannot run the useful workload, the one concrete alternative
is to retain the same one-container design and pools but evaluate a
**268,435,456-byte group cap**, yielding **277,237,760 bytes** of controlled
capacity. That explicitly exceeds the approved product bound and requires a
requirements/decision change. It is not an available automatic fallback.

No measurement supports that alternative yet. If authorized after failure, run
the same finite cases once and present both results before seeking adoption;
do not label it a measured feasible alternative in advance. It trades the
100,000,000-byte guarantee for more room without a custom supply system or extra
service. If permissions rather than fit fail, increasing memory is irrelevant:
stop and obtain a supported native Docker target with existing adequate runtime
permissions, not a host security or delegation change made by the agent.

Next action: review and preserve the Q1/Q2 result and determine a supported way
to establish the unresolved Q3 native PCM bound. Q4 remains not run; application
architecture acceptance is still gated. Issue #7 remains open until actual
acceptance evidence exists; the larger-cap alternative is outside the approval.

The [prepared stock-decoder startup check](../evaluations/linux_resource/decoder-startup.md)
tests one supplemental DATA/STACK-limit candidate with `-version` and no input.
Its signed dependency resolution and actual installed-binary review are complete.
The [recorded recovery and startup result](../evaluations/linux_resource/decoder-supply.md)
shows exit 127 at a shared-library mapping failure under the fixed DATA/STACK
candidate, with verified cleanup. That candidate is rejected; no memory tuning
or audio followed. The whole-service ledger is unchanged. This does not establish
overall-budget infeasibility or PCM coverage; Q3/Q4 and alternative assessment
remain outstanding.

The [follow-up mpg123 result](../evaluations/linux_resource/mpg123-proposal.md)
establishes authenticated supply and a no-input startup pass with the unchanged
base and numeric limits. It supplies no useful decode or aggregate PCM evidence.
Its separately prepared hard/soft AS 12,524,288 startup variant retains the
original DATA/STACK and group limits. The source-derived ordinary-mapping scope,
special-map exclusions and conditional PCM arithmetic are recorded there; no
execution or useful-fit result for the AS variant is claimed.
