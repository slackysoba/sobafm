# 0010: Control crash dumps in SobaFM's processes on amd64

Status: **Accepted** (2026-11-06). [Maintainer acceptance on #133](https://github.com/slackysoba/sobafm/issues/133#issuecomment-6683100286).
Issue: [#133](https://github.com/slackysoba/sobafm/issues/133), under [Outcome 4](https://github.com/slackysoba/sobafm/issues/4).
Related: [ADR 0004](0004-amd64-whole-service-adoption.md), [#60](https://github.com/slackysoba/sobafm/issues/60), [#138](https://github.com/slackysoba/sobafm/issues/138).

The maintainer accepted option 3 for amd64. SobaFM relies on its own process controls and records the host's `kernel.core_pattern` during qualification. The host need not use a particular value. The existing credential, audio-storage and controlled-capacity requirements apply to crash dumps.

## Problem

When a process crashes, Linux can write a core dump: a copy of its memory. The host-wide setting `kernel.core_pattern` says where it goes, and this record calls that the route:

- **File route:** a file, as with the kernel's default value `core`, which writes to the crashing process's working directory.
- **Pipe route:** the value starts with `|`, and the kernel runs that program on the host and streams the dump to it.
- **Socket route:** the value starts with `@`, and the kernel sends the dump to a socket on the host (since Linux 6.16).

The harness required the value to be exactly `core`, and only a WSL2 host has passed. [ADR 0004](0004-amd64-whole-service-adoption.md#consequences) retained that gate while recording that no requirement or decision states its reason. Outcome 4 asks for the reason to be recorded, or the gate changed, before a real host is chosen.

## Decision and scope

The exact value `core` came from a harness assumption. It excluded ordinary hosts that pipe dumps and could still permit the main process to write credentials into a file. For amd64, this record supersedes only ADR 0004's exact-route qualification premise. Qualification must exercise the application on the target kernel, including the three [piped-host cases](#verification-needed).

The main process sets its dump filter to zero, disables its own dumps and verifies both settings before reading configuration. The stock decoder inherits the filter and retains its zero core limit. These controls landed separately in [PR #140 for #138](https://github.com/slackysoba/sobafm/pull/140).

The acceptance covers design and requirements interpretation only. Choosing a host, running native or Docker tests, changing host settings, contacting providers, Discord or music sources, and spending all require separate authorization. Native arm64 stays on hold under [#60](https://github.com/slackysoba/sobafm/issues/60).

## Where the gate lives

| Place | What it does | State |
| --- | --- | --- |
| Harness at `81a7519`: [decode](https://github.com/slackysoba/sobafm/blob/81a7519/evaluations/linux_resource/owned_mp3_decode.py#L445), [overlap](https://github.com/slackysoba/sobafm/blob/81a7519/evaluations/linux_resource/owned_mp3_overlap.py#L457), [cancel](https://github.com/slackysoba/sobafm/blob/81a7519/evaluations/linux_resource/owned_mp3_cancel.py#L311), [topology](https://github.com/slackysoba/sobafm/blob/81a7519/evaluations/linux_resource/owned_mp3_topology.py#L328) | Refuses to run unless `/proc/sys/kernel/core_pattern`, stripped, equals `core`. | Removed from `main` in [#104](https://github.com/slackysoba/sobafm/issues/104). |
| [#44 evaluation](https://github.com/slackysoba/sobafm/blob/81a7519/evaluations/fma_validation/observation.py#L730) at `81a7519` | Fails with `core_route_changed` unless the bytes are `core` and a newline. | Removed from `main`. |
| [arm64 preflight](../../evaluations/linux_resource/arm64_runner_preflight.py#L452) ([classification](../../evaluations/linux_resource/arm64_runner_preflight.py#L481), [issue](../../evaluations/linux_resource/arm64_runner_preflight.py#L493), [description](../../evaluations/linux_resource/arm64-runner-preflight.md)) | Reads the value, classifies it as `core`, `pipe` or `other`, and lists `core_routing` as an issue unless it is `core`. | On `main`. The only code left that reads it. On hold and unchanged under #60. |
| [ADR 0004](0004-amd64-whole-service-adoption.md), [engineering notes](../engineering-notes.md), [feasibility record](../linux-feasibility.md) | State it in words, as "the core-routing gate" and "core-routing premises". | Historical premise for amd64, superseded here. |
| `src/sobafm/` | The [entrypoint](../../src/sobafm/__main__.py) verifies [process dump controls](../../src/sobafm/crash_dumps.py) before reading configuration. The [decoder](../../src/sobafm/decode.py#L102) keeps `RLIMIT_CORE` 0. | The service does not inspect the host route. |

The gate's only stated purpose is one sentence in the [harness plan](https://github.com/slackysoba/sobafm/blob/81a7519/evaluations/linux_resource/mpg123-fixture-plan.md#L91), first committed in [#22](https://github.com/slackysoba/sobafm/commit/6eb4abb): "Also set hard/soft CORE to zero and require the observed core routing to be the plain `core` file pattern; a piped handler would need separate assessment." So it meant "pipes aren't assessed", not "pipes are forbidden". This record is that assessment.

## What it protects

### What the kernel does

The controls below behave differently on each route, in [`fs/coredump.c` at v7.0](https://github.com/torvalds/linux/blob/v7.0/fs/coredump.c). The same checks are at v6.18, the kernel line of the recorded WSL2 host.

| Control on the crashing process | File route | Pipe route | Socket route |
| --- | --- | --- | --- |
| `RLIMIT_CORE` 0, as the decoder has now | [No dump](https://github.com/torvalds/linux/blob/v7.0/fs/coredump.c#L884) | [Ignored](https://man7.org/linux/man-pages/man5/core.5.html): the dump is sent | [Ignored](https://github.com/torvalds/linux/blob/v7.0/fs/coredump.c#L726): the dump is sent |
| `RLIMIT_CORE` 1 | No dump | [No dump, no handler](https://github.com/torvalds/linux/blob/v7.0/fs/coredump.c#L979) | Ignored: the dump is sent |
| Not dumpable (`PR_SET_DUMPABLE` 0) | [No dump](https://github.com/torvalds/linux/blob/v7.0/fs/coredump.c#L1085) | No dump, no handler | No dump, no handler |
| `coredump_filter` 0 | No memory mappings | Handler runs; [no memory mappings](https://github.com/torvalds/linux/blob/v7.0/fs/coredump.c#L1594) | Handler runs; no memory mappings |

With `coredump_filter` 0 the dump still carries [ELF headers and notes](https://github.com/torvalds/linux/blob/v7.0/fs/binfmt_elf.c#L1594) (registers, auxiliary vector, signal details, mapped file names, argv) and the vDSO. On the socket route a handler could ask to write the dump itself, which would bypass the filter. systemd's handler [asks the kernel to write it](https://github.com/systemd/systemd/blob/v262/src/coredump/coredump-worker.c#L117).

### The reachable code paths

A dump needs a [core-dump signal](https://man7.org/linux/man-pages/man7/signal.7.html) such as a segmentation fault or abort. SIGTERM, SIGKILL and an OOM kill don't produce one, so ordinary cancellation, shutdown and OOM paths never reach this code.

- **Decoder.** `DecodeProducer.start` [launches mpg123](../../src/sobafm/decode.py#L290) and then applies the limits. A fault on bad input, or a [stack overflow](https://man7.org/linux/man-pages/man2/getrlimit.2.html) under its 524,288-byte stack limit, enters the kernel's dump path. On the file route the limit stops the dump and the decoder exits at once. Before #138, on the pipe or socket route the kernel could stream the decoder's written memory, including decoded PCM and input bytes, up to its 12,524,288-byte address space. Its [fixed environment](../../src/sobafm/decode.py#L107) holds no secrets, and it takes input on stdin, so no recording's file name appears in its mapped files.
- **Parent.** The Python parent holds the Discord token, the Gemini key, both PCM banks and the copy pool, and it loads native code such as dave.py, PyNaCl and aiohttp's extensions. Before #138 it had no dump controls. It [removes the credentials from `os.environ`](../../src/sobafm/discord_adapter.py#L287), but [`/proc/pid/environ` keeps showing the initial environment](https://man7.org/linux/man-pages/man5/proc_pid_environ.5.html). The harness ran without credentials, so the gate's purpose never covered this.
- **Parent crash on the file route.** The container's working directory is the [writable `/data` volume](0009-writable-data-volume.md). On one WSL2 Docker host with `core_pattern=core`, a SIGSEGV with an unlimited core limit wrote a 46,567,424-byte dump containing the placeholder token before the fix. The fixed image left no dump under the same limit. The host's default limit was zero, and neither image dumped with that default. [Image identities, positive control and results](https://github.com/slackysoba/sobafm/pull/140#issuecomment-6680009310).

### Risks on a piped host

The following analysis explains the original risk without the process controls and the remaining question about a slow handler. The pipe behavior comes from source and documentation. It still needs observation on the target.

| Question | What happens | Requirement it meets |
| --- | --- | --- |
| Who pays for the memory | The dying process's pages stay charged to the group until it exits. The handler [runs in the host's initial namespaces](https://man7.org/linux/man-pages/man5/core.5.html), [started from a kernel workqueue](https://github.com/torvalds/linux/blob/v7.0/kernel/umh.c#L433), so neither its memory nor its copy is in the 90,177,536-byte group. Kernel pipe storage is a named exclusion. The handler's copy isn't. | [ADR 0004](0004-amd64-whole-service-adoption.md#decision): "At most two charged PCM-bearing native address spaces". The [named exclusions](../requirements.md#approved-memory-definition) don't include it. |
| Where the data goes | **systemd-coredump** [stores the dump on the host](https://github.com/systemd/systemd/blob/v262/man/systemd-coredump.xml) under `/var/lib/systemd/coredump/` for a few days, logs to the journal and records `/proc/pid/environ` as `COREDUMP_ENVIRON`. It [grants read access to the crashing numeric UID](https://github.com/systemd/systemd/blob/ec1065495802/src/coredump/coredump-submit.c), which is 1000 here unless the daemon remaps users. A container only gets its own dumps if its unit enables `CoredumpReceive=`, which that manual aims at full-OS containers. **Apport** [logs one host line and exits](https://github.com/canonical/apport/blob/2.28.1/data/apport#L537) for a container without its socket, and keeps no report. | [Storage](../requirements.md#source-specific-audio-storage): persist audio only where permitted, with a configured limit and removal policy. [Credentials](../requirements.md#deployment-privacy-and-supply-chain): not in logs or published diagnostics. `AGENTS.md` says the same of diagnostics. |
| The cleanup clock | The decoder stays alive while dumping, so stdout stays open, no end of output arrives and the clock hasn't started. Buffered audio runs out and the listener hears silence, then the track fails as `decoder_exit`. With `core_pipe_limit` nonzero ([16 from systemd](https://github.com/systemd/systemd/blob/v262/sysctl.d/50-coredump.conf.in), [10 from apport](https://github.com/canonical/apport/blob/2.28.1/data/apport#L652)) the kernel also waits for the handler to exit, and [a misbehaving handler can block reaping](https://github.com/torvalds/linux/blob/v7.0/Documentation/admin-guide/sysctl/kernel.rst#L189). Once cancelled, [only SIGKILL gets through](https://github.com/torvalds/linux/blob/v7.0/kernel/signal.c#L878), and `finish` [sends it at two seconds](../../src/sobafm/decode.py#L377). It [interrupts the write](https://github.com/torvalds/linux/blob/v7.0/fs/coredump.c#L576) and [the wait](https://github.com/torvalds/linux/blob/v7.0/fs/coredump.c#L602), leaving one second. | [Cleanup](../requirements.md#cleanup). The source shows an interruptible write and wait. Release within the deadline has not been measured on a piped host. The kernel's [wait for the handler's exec](https://github.com/torvalds/linux/blob/v7.0/kernel/umh.c#L457) isn't interruptible, and the socket route's wait wasn't traced this far. |
| The host | Satisfying the exact value on a default Ubuntu, RHEL, Fedora or SLES host means changing a host-wide setting that affects every other workload. | [Requirements](../requirements.md#approved-memory-definition): no silent host security changes. |

The operator's host normally holds these credentials already, in `.env`. What a dump adds is a second unmanaged copy, readable by a host user with the same numeric UID, on a retention schedule SobaFM doesn't control.

### The rule for gates

`docs/agent-briefs.md` asks every new gate for three things. The exact value has none on record, so here is what a replacement must name:

- **Requirement:** the storage and credential rules above, and ADR 0004's bound on PCM-bearing address spaces. The deadline is unchanged either way.
- **Reachable code path:** a core-dump signal in the decoder or the parent, as above.
- **Missing observable fact:** what such a signal actually does in the real image on a host that pipes. Is the handler started, how many bytes does it get, do they include a known marker from the process's memory, and how long is it until release, against the original clock? No recorded case answers this. The Q4 cases use TERM and KILL, and the [retained harness](https://github.com/slackysoba/sobafm/tree/81a7519/evaluations/linux_resource) never sends a core-dump signal. The host's `core_pattern` value can't answer it either, which is why it is the wrong thing to observe.

## What real hosts do

| Host | Default route | Source and limits |
| --- | --- | --- |
| Linux kernel | File, `core` | [Sysctl documentation](https://github.com/torvalds/linux/blob/v7.0/Documentation/admin-guide/sysctl/kernel.rst#L144). |
| Debian 13 | File, unless `systemd-coredump` is installed | [`systemd`](https://packages.debian.org/trixie/amd64/systemd/filelist) [sets `core`](https://salsa.debian.org/systemd-team/systemd/-/blob/debian/trixie/debian/extra/sysctl.d/10-coredump-debian.conf). The pipe setting, `50-coredump.conf`, ships only in [`systemd-coredump`](https://packages.debian.org/trixie/amd64/systemd-coredump/filelist), which `systemd` [neither depends on nor recommends](https://salsa.debian.org/systemd-team/systemd/-/blob/debian/master/debian/control). Inferred from packaging; not checked on each Debian image. |
| Ubuntu | Pipe to apport | [Canonical](https://ubuntu.com/blog/apport-local-information-disclosure-vulnerability-fixes-available) calls apport the Ubuntu default handler, installed in all Ubuntu Server images. Apport [2.28.1](https://packages.ubuntu.com/noble/apport), as in 24.04, [writes the pipe at start](https://github.com/canonical/apport/blob/2.28.1/data/apport#L649) and is [enabled by default](https://github.com/canonical/apport/blob/2.28.1/etc/default/apport). |
| RHEL 9 | Pipe to systemd-coredump | [Qualys](https://www.qualys.com/2025/05/29/apport-coredump/apport-coredump.txt) and Canonical state it as the default. [Red Hat's guide](https://docs.redhat.com/en/documentation/red_hat_enterprise_linux/9/html/developing_c_and_cpp_applications_in_rhel_9/debugging-applications_developing-applications) gives it as the configuration to verify, without the word "default" in the text read. That site refused a direct fetch, so its text was read through search results. |
| Fedora | Pipe to systemd-coredump | [Fedora documentation](https://docs.fedoraproject.org/en-US/qa-docs/guides/coredumpctl/), Qualys. |
| SLES 15 | systemd-coredump | [Enabled by default](https://documentation.suse.com/sles/15-SP7/html/SLES-all/cha-tuning-systemd-coredump.html), except [Minimal VM images](https://documentation.suse.com/sles/15-SP4/html/SLES-all/article-minimal-vm.html). |
| Amazon Linux 2023 | Pipe to systemd-coredump | [AWS advisory](https://explore.alas.aws.amazon.com/CVE-2021-3864.html). |
| Amazon Linux 2 | File, `core` | The same advisory. |
| systemd 262 on a kernel with the required support | Socket, `@@`, replacing the pipe when its service starts | [Its unit](https://github.com/systemd/systemd/blob/v262/units/systemd-coredump-register.service.in#L27) and [manual](https://github.com/systemd/systemd/blob/v262/man/systemd-coredump.xml), generally kernel 6.19 or newer. No distribution default is claimed for this route yet. |
| GitHub-hosted arm64 Ubuntu runner | Pipe, observed | Preflight [on November 1](https://github.com/slackysoba/sobafm/issues/7#issuecomment-6553570914) and [November 4](https://github.com/slackysoba/sobafm/issues/7#issuecomment-6634028903). |
| Recorded amd64 WSL2 host | File, `core`, observed | [Recorded result](https://github.com/slackysoba/sobafm/blob/81a7519/evaluations/linux_resource/results/2026-11-01-owned-flac-decode.yaml#L83). |

CentOS Stream, Rocky, AlmaLinux, Flatcar and Container-Optimized OS weren't checked, and this record claims nothing about them.

[Docker Engine's install page](https://docs.docker.com/engine/install/) lists CentOS, Debian, Fedora, Raspberry Pi OS (32-bit), RHEL and Ubuntu. Of those with a sourced amd64 default above, only Debian keeps the file route. So the exact gate turns away the default configuration of most of the distributions an operator would pick.

**A container can't change the setting.** `core_pattern` is [one global kernel variable](https://github.com/torvalds/linux/blob/v7.0/fs/coredump.c#L82), [registered as a plain sysctl](https://github.com/torvalds/linux/blob/v7.0/fs/coredump.c#L1552). [Docker supports only namespaced sysctls](https://docs.docker.com/reference/cli/docker/container/run/#sysctl), and this isn't on its list. [Docker mounts `/proc/sys` read-only](https://github.com/moby/moby/blob/402d0a16f131/daemon/pkg/oci/defaults.go#L110), and the service runs non-root with all capabilities dropped. A container can read the value, as the harness did. This record doesn't assume any host setting can be changed.

## Options

Each option keeps the 98,979,840 controlled bytes and the cleanup deadline, and option 6 also amends the approved memory definition. None chooses a host.

| Option | Default piped hosts | Socket route | Parent's credentials | Changes the reviewed decoder limits |
| --- | --- | --- | --- | --- |
| 1. Keep exact `core` | Refused | Refused | Not covered: its dump lands in `/data` | No |
| 2. Any file route | Refused | Refused | Not covered: its dump lands in `/data` | No |
| 3. Controls on own processes (**accepted**) | Accepted | Accepted | Covered | No |
| 4. Soft limit of one | Accepted | Not covered | Only if also applied to the parent | Yes |
| 5. Non-dumpable decoder | Needs a changed decoder | Would be covered | Not covered | Replaces the stock decoder |
| 6. Drop the gate, accept handlers | Accepted | Accepted | Not covered | No |

The table compares the options as originally proposed. The host rule and the process controls were separate decisions. PR #140 implemented the parent control and the filter the decoder [inherits through fork and exec](https://man7.org/linux/man-pages/man5/core.5.html). “Not covered” describes each alternative on its own.

### Option 1: keep exact `core`

- **Evidence needed:** none new. To take effect it would also need a check in `__main__.py`, because the service doesn't have one.
- **What it changes:** no limits. It makes the host a product constraint, in practice Debian, or requires operators to change a host-wide setting. That needs an explicit exception to the rule against silent host security changes. It does nothing about the parent's dump in `/data`, which #138 handles.

### Option 2: narrow it to any file route

Accept any value not starting with `|` or `@`, including an empty one, and keep `RLIMIT_CORE` 0. The limit stops file dumps wherever the pattern points, so `core.%p` and absolute paths meet the same property.

- **Evidence needed:** the same read-only observation, and one native crash case on a host using the file route.
- **What it changes:** no limits or requirements. It corrects the test but admits no mainstream default, so it doesn't solve the problem on its own. The parent's dump in `/data` is left to #138.

### Option 3: controls on SobaFM's own processes (accepted)

Stop requiring a host value, and record the route as a fact in qualification evidence.

- **Parent:** before reading configuration, the entrypoint writes 0 to `/proc/self/coredump_filter`, sets `PR_SET_DUMPABLE` to 0 and reads both back. It exits 5 if either control fails. The filter is written first because [non-dumpability makes the process's `/proc` files root-owned](https://man7.org/linux/man-pages/man5/proc_pid.5.html). Once non-dumpable, the parent produces no dump and starts no handler on any route.
- **Decoder:** `RLIMIT_CORE` stays 0. Stock mpg123 inherits the filter across exec, which resets dumpability. On a file route the limit prevents a decoder dump. On a pipe route the kernel can send a filtered dump to a handler. The filter omits ordinary memory mappings but leaves the ELF notes, registers and vDSO described above. The socket route also depends on whether the handler asks the kernel to write the dump.
- **Evidence needed:** three native crash cases per target kernel, on a host that pipes. See [Verification needed](#verification-needed). No amd64 host that pipes is on record.
- **Debugging cost:** after startup a main-process crash leaves its exit status and logs, with no core dump to debug. On a piped host a decoder crash can still leave a small diagnostic record. The operator guide will need to explain what that record can contain.
- **Limits:** a compromised decoder could raise its own filter, which the requirements don't claim to prevent. The `docker-init` process and the moment between exec and the first lines of `__main__.py` aren't covered.

### Option 4: soft limit of one

Set the decoder's hard and soft `RLIMIT_CORE` to 1. The kernel treats that value as a recursion guard and aborts a pipe dump before starting the handler. [Assessed on 2026-11-01](https://github.com/slackysoba/sobafm/issues/7#issuecomment-6553650111) and not accepted, because the process could lower the value to zero, which isn't equal to an immutable zero.

- **What's different now, for it:** that comparison was with a plain-`core` host. On the same piped host, one is never worse than zero. It stops an ordinary crash, and lowering it only gets back what zero gives there today.
- **What's different now, against it:** the socket route ignores it, and systemd 262 moves to that route on new kernels. A host upgrade would silently remove the protection, so the gate would still have to read and refuse host values. The value is explained in a kernel source comment and not, in what was read, in user documentation.
- **Evidence needed:** the native cases on each target kernel, plus that permanent route check.
- **What it changes:** the reviewed decoder core limit. It doesn't cover the parent unless applied there too.

Changing this reviewed limit would require a separate decision if qualification finds a handler delay.

### Option 5: non-dumpable decoder

[Assessed on 2026-11-01](https://github.com/slackysoba/sobafm/issues/7#issuecomment-6556964526) and not accepted, for two reasons: exec resets the attribute, and setting it after exec would block the parent's required inspection of the decoder.

- **What's different now:** `src/sobafm/` no longer inspects the decoder through `/proc`, so the second reason no longer applies on `main`. The first still does. [`PR_SET_DUMPABLE` acts only on the calling process](https://man7.org/linux/man-pages/man2/PR_SET_DUMPABLE.2const.html), and stock mpg123 doesn't call it.
- **A trap:** an unreadable decoder binary makes [exec set the attribute from `fs.suid_dumpable`](https://github.com/torvalds/linux/blob/v7.0/fs/exec.c#L1210) instead. systemd and apport both set that to 2, [which still dumps to a handler](https://github.com/torvalds/linux/blob/v7.0/Documentation/admin-guide/sysctl/fs.rst#L249). So this fails on exactly the hosts in question.
- **Evidence needed:** a new decoder supply and startup review for any shim or changed decoder.
- **What it changes:** the stock-decoder premise and the "no custom native build" constraint.

The earlier assessment didn't consider the parent, which has no such problem. Option 3 and #138 use non-dumpability there.

### Option 6: drop the gate and accept handlers

- **Evidence needed:** none beyond ordinary qualification.
- **What it changes:** the requirements. It keeps the 98,979,840 bytes but amends the approved memory definition, which would have to name the handler's copy as an exclusion. The requirements would also have to allow audio in host crash stores, and credentials too unless the parent control is added, or tell operators to set `Storage=none`.

Option 6 would waive the native cases and add a host-handler exclusion. The maintainer accepted option 3 with the existing memory definition.

## Consequences

- The full 90,177,536-byte group, 4,608,000 bytes of PCM and 4,194,304 bytes of copies still total 98,979,840 controlled bytes. No host-handler copy gains an exclusion.
- The original three-second cleanup clock still governs cancellation. A missed deadline remains a failed verdict. Owners, slots and debt stay charged until actual release, including when a handler delays reaping.
- The previous amd64 results remain bound to their original sources, images and kernels. The old harness did not exercise a core-dump signal. The native arm64 preflight and its CI job stay unchanged.
- Follow-up work under Outcomes 4 and 5 must apply the decision to the remaining qualification records and operator guidance and obtain the target evidence below.

## Verification needed

A separate issue must identify an amd64 host that already pipes dumps and obtain approval for Docker and native decoder use. Bind all three cases to the actual source, image and target kernel. The offline startup-refusal tests from #138 cover failure to apply the controls.

1. **Decoder crash:** on a host that pipes, in the real image, non-root and read-only, with an owned fixture: send a core-dump signal to a running decoder. Record the route class, the exit status, the time to release against the original clock, and whether the handler's bytes contain a known marker from decoder memory.
2. **Parent crash:** the same with placeholder credentials, expecting no handler at all.
3. **Stalled handler:** a handler that never exits, then cancellation, expecting SIGKILL at two seconds and release before three.

Counting the decoder handler's bytes in case 1 and stalling it in case 3 require a capturing handler and a change to `core_pattern` on a disposable runner. That needs separate explicit authorization. Inspection of a default handler's retained records can cover cases 1 and 2 only in part. Neither the decoder's output contents nor timely release against a stalled handler has been established on a piped amd64 host.

## What is established and what isn't

**Established:**

- The gate's locations, its one stated purpose, and that `src/sobafm/` doesn't check it.
- The maintainer confirmed that the existing storage, credential and capacity requirements cover dumps. No product requirement specifies the exact host value `core`.
- Kernel routing and control behavior, as read from documentation and from source at v7.0 and v6.18.
- The sourced distribution defaults above, within the limits each row states.
- A container can't change the setting through Docker.
- In the #140 file-route image check, the main process reported dumpable 0. The stock decoder inherited filter 0, retained `RLIMIT_CORE` 0 and completed and cancelled cleanly in the two observed runs. Cancellation there did not exercise the signal path. [Recorded results](https://github.com/slackysoba/sobafm/pull/140#issuecomment-6680009310).

**Not established:**

- On a piped amd64 host, the actual handler's invocation, output contents and release timing for the three cases above. The observed piped host in the table was an arm64 preflight runner.
- Behavior on kernels older than v6.18, such as those long-term distributions ship. `coredump_filter` has been documented since Linux 2.6.23, but the source was read only at those two versions.
- Whether malformed input can crash stock mpg123 at all. An outside signal exercises the same kernel path, so the case doesn't depend on it.
