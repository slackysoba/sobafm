# Q4 launcher and descendant cases

The [concrete design accepted on issue #7](https://github.com/slackysoba/sobafm/issues/7#issuecomment-6552315831)
permits this offline implementation of the remaining two Q4 fault fixtures.
It continues the [direct cancellation preparation](mpg123-cancellation-plan.md)
and the accepted [experimental contract](../../docs/decisions/0001-linux-resource-proposal.md).
Both native-amd64 topology cases passed once after Q3 acceptance, exact
source/operator review and separate one-shot releases. The actual results below
retain their process, command, source and image bindings. This is the initial
controlled Q4 proof, not application or native-arm64 qualification.

## Concrete topology and ownership

One controller A owns every process identity, original I/O operation, descriptor,
payload view, deadline and terminal result. A forks launcher L; L establishes a
new session and forks decoder D once. Both forks precede any fixture opening or
parent PCM/copy allocation. L never execs; D stays in L's PGID/session and execs
the unchanged stock mpg123. No later fork occurs.

`topology_protocol.py` contains this narrow non-media launcher and its bounded
protocol. `owned_mp3_topology.py` validates the topology and owns its lifecycle.
It shares the existing actual PCM reader/writer, cancellation clock, close-debt
handling and protected owner loop. Historical Q3 code and the direct decoder's
PGID=PID assumptions are unchanged; the topology has its own child entry and
explicit ancestry/group/session checks.

A owns all seven pipe pairs from each successful acquisition, including partial
setup failure. Fork inheritance and explicit closure transfer their endpoints:

| Channel | Controller A | Launcher L after transfer | Decoder D before exec |
| --- | --- | --- | --- |
| Compressed input | write | none | read, duplicated to 0 |
| PCM output | read | none | write, duplicated to 1 |
| Decoder diagnostics | read | none | write, duplicated to 2 |
| Decoder ready | read | none | write, close-on-exec |
| Decoder exec gate | write | none | read, closed before exec |
| Launcher command | write | read | none |
| Launcher status | read | write | none |

L closes all media/exec endpoints and redirects standard descriptors to
`/dev/null`. Its only remaining nonstandard descriptors are the two control
endpoints. D closes both launcher channels and all unused media endpoints; after
exec it retains only 0/1/2. A independently checks each exact descriptor set,
pipe device/inode and access direction. A leaked writer is a failed gate.

The controller pins PID/start identities and validates PPid, PGID, session,
UID/GID 1000, `NoNewPrivs=1`, default seccomp filtering, no effective capabilities,
the original cgroup and one thread per process. Both native members must be the
entire expected inventory before media. L's CORE limit is zero. D receives the
unchanged hard/soft AS/DATA/STACK/CORE limits before exec.

L reports the actual `waitpid` SIGSTOP result because it is D's parent. A checks
the fixed native executable, stopped PID/start identity, mappings, limits and
descriptors independently before allocating the original banks/copy region and
authenticating the compressed snapshot. A never calls `waitpid` on nondirect D.

## Fixed executables, input and capacity

Reuse image
`sha256:e3ff7b02740b55f52735b17afc9846d151f86bf7395341f5d53f7d3dbb235cfc`
on Linux amd64 with the existing read-only mounts/root, no network, no extra
capabilities or privileges, default seccomp and container init. Retained native
receipts report CPython 3.14.7; a version string is not an executable binding.
The topology entrypoint requires `--controller-elf-sha256`. Freeze its actual
value in the future reviewed operator, then require A's executable hash and
L's inherited resolved path/device/inode/hash to match before media. There is
no invented interpreter digest or permission workaround in this preparation.

D uses the existing seven-file ELF allowlist, fixed C locale and raw-stdout argv.
The mpg123 executable remains
`62ef82208722eda0fedd39de0ced0d8e3a8450d129760152ea16d075651f0a91`.
Reuse the 1,920,768-byte CBR fixture with SHA256
`1a431c03098d8be79a522a3b1f64a8182445a8bf6c009f54c1b3bb1f6dad044d`;
submit at most 65,536 bytes and retain the unfinished snapshot. Neither L nor D
opens the fixture. No complete decode, regenerated fixture or happy-path rerun
is part of these cancellation cases.

The controlled ledger stays
`90,177,536 + 4,608,000 + 4,194,304 = 98,979,840` bytes. The existing conservative
PCM ledger stays `2 * 12,524,288 + 8,802,304 = 33,850,880`, below 34,560,000.
This fixture uses one PCM-bearing native decoder. L's zero-PCM premise is its
pre-payload fork, verified absence of media descriptors and fixed non-media code
path. The same-UID fixture mount is not a filesystem access-control barrier.
L's interpreter and metadata remain charged inside the original group cap; its
extra process is now exercised only by the bounded topology cases below, not a
general playback workload. Kernel pipe storage and
object/allocator overhead retain the documented product exclusions and are not
additional payload pools. No limits or reservations are enlarged.

## Finite control protocol

Commands are single bytes: `S` requests stopped-child observation, `P` requests
the post-TERM witness, and `Q` commands intentional leader exit. The only valid
sequences are `S,P` and `S,Q`, selected by the fixed case; each occurs once.
There is no heartbeat, dynamic payload, replacement child or command retry
after a failed write. A nonblocking EAGAIN retains the same original operation.

Status records use the 17-byte network-order layout `!cIQi`: tag, decoder PID,
start ticks and raw wait status/error code. Only `H` (HELLO), `S` (actual stopped
wait), `P` (PONG), `X` (actual decoder reaping) and `E` (finite local error) are
allowed, once each: at most 85 bytes for the stream. Partial reads, ordering,
identity, status words and bounds are validated before changing topology state.
Unknown, duplicated, truncated or oversized records fail closed.

L retains only one pending record and bounded metadata. A broken control channel
or local failure latches failure and keeps L alive for A's cleanup; it cannot
silently orphan D by exiting. Only valid `Q` takes the intentional exit path.
A retains all original control/media operations until completion. Failed
deadline/backstop diagnostic writes remain inside the original cleanup loop and
are attempted once, as in the direct cancellation owner.

## Ignored TERM in an owned non-media child

The `ignoring_launcher` case requires L's actual `SIG_IGN` TERM bit and an
unblocked TERM mask. The stock decoder is not claimed to ignore TERM. SIGSTOP,
blocked pending signals and delayed handlers cannot satisfy this condition.

After at least four PCM bytes arrive through the original reader, with input
open and unfinished, A records cancellation. TERM is dispatched to the original
group before closing input. A repeats cancellation at that dispatch transition,
then sends `P`. Require PONG and a fresh live L identity/disposition observation
before the original-start-plus-one-second KILL point. Missing PONG fails the
case and never delays KILL. Actual launcher `waitpid` must report `-SIGKILL`.

D may be reaped by L or disappear after init reaping. Preserve a validated `X`
when received. Otherwise report an explicitly unobserved reaper and null decoder
wait status, with the complete release evidence below. Do not require catching
a transient post-KILL reparent state or fabricate an exit code from absence.

## Descendant retained after commanded leader exit

The `descendant_after_leader_exit` case first observes actual PCM, stops further
input admission and keeps stdin open. A sends `Q` while D is running or blocked
in ordinary I/O. Do not use job-control stop to manufacture survival.

Require L's actual wait status zero and expected control EOF, then positively
observe the same still-live D with PPid=1, original PGID/session, fixed executable
and original media endpoints. Only this retained-descendant observation triggers
the first cancellation. A signals the group using D's pinned identity, closes
input and repeats cancellation. An exited leader cannot suppress group cleanup
or release the original payload lease.

## Actual release and evidence

Both cases retain the original three-second cancellation deadline, at-most-one-
second TERM grace and bounded drain. The phase budget is 15 seconds; the native
retention backstop reports failure at 45 seconds while preserving ownership.
The reviewed external operation is bounded at 90 seconds with its own
original three-second cleanup deadline. No container kill counts as successful
producer cleanup, and no automatic workload retry is authorized.

Release requires A's actual reaping of L, pinned `/proc/D/stat` absence, an empty
original process group and an empty whole native inventory. It also requires
all original descriptors and operations settled and all payload views released.
A zombie, missing executable link, `cgroup.procs` omission, `ECHILD`, signal
delivery or leader exit alone cannot establish this. Permission uncertainty and
changed identities retain debt. A missed original deadline remains failed after
late actual release; a stuck control/media operation blocks replacement.

Receipts distinguish launcher wait status from decoder wait status and reaper.
The descendant case records the observed init adoption; otherwise preserve a
launcher-reported decoder status when available and label an unavailable reaper
or status explicitly. Record both identities, signals, control transcript,
partial-frame discard, bounded diagnostics, bank high water, memory events,
swap, deadline and actual release. `complete_decode` remains false.

## Offline checks and remaining gates

`test_topology_contract.py` uses actual local pipes with deterministic kernel
outcomes. It does not fork or execute a decoder. It covers partial acquisition,
both fork boundaries, exclusive descriptor transfer, fixed native child setup,
partial/invalid control records, original delivery-before-completion, failed
control writes, stopped/reaped child reports, lost control channels, zombies,
unknown/reused identities, missing PONG, ECHILD and late cleanup. Both full
controller schedules use the actual shared owner loop and media/control I/O.

Run locked sync, all-file hooks/strict Pyright and full pytest before push.
These are implementation checks, not native Q4 qualification. The actual cases
below additionally required the frozen source/operator and interpreter identity,
reviewed native permissions and useful fit, Q3 acceptance and separate one-shot
releases. Future execution requires its own applicable review and release.
Retain at most 700,000 bytes per native case, 64,000 selected public bytes and
one 32,768-byte Docker local log file. No PCM is exported.

## Recorded topology results

The two one-shot cases ran on 2026-11-01 at source
`e5e2f8cc0f53478a83b6b22b15f20b5f1ec65ed5` and image
`sha256:e3ff7b02740b55f52735b17afc9846d151f86bf7395341f5d53f7d3dbb235cfc`,
on x86_64 kernel `6.18.33.2-microsoft-standard-WSL2`.
The actual controller ELF was
`30a7def0b3e8d24a36d3829202717542bef124c0a40608b2232b7c837b271bc9`.
Each receipt retains its own runtime device/inode and pinned process identities;
those observations are not transferred between containers.
The [independent actual-result review](https://github.com/slackysoba/sobafm/issues/7#issuecomment-6556104431)
verified all 17 files in each manifest and accepted both cases.

| Case and selected receipt | Actual topology witness | Native release after first cancellation |
| --- | --- | --- |
| [Ignoring launcher](results/2026-11-01-q4-ignoring-launcher.yaml) | Same launcher alive with TERM ignored and unblocked at +1.004180360 seconds; KILL dispatched at +1.005211198 seconds and launcher reaped with status -9. Decoder reaped by launcher with status 0; H/S/P/X and EOF observed. | 1.027698228 seconds |
| [Descendant after leader exit](results/2026-11-01-q4-descendant-after-leader-exit.yaml) | Launcher reaped with status 0; same decoder observed running under PID 1 in its original group/session before cancellation. H/S and EOF observed; pinned process, original group and native inventory later absent. | 0.002612263 seconds |

The ignored-TERM witness applies to the owned non-media launcher, not stock
mpg123. The one-second grace is the scheduled KILL threshold; the receipt retains
the actual dispatch timestamp rather than rounding it to one second.
The descendant's decoder exit status remains null: init adoption and actual
release were observed, but the controller did not obtain a decoder wait status.
Leader exit or init adoption alone did not release its resources.

Each case accepted only the 65,536-byte compressed prefix and repeated cancellation
without resetting the original three-second deadline. Both native and external
deadlines passed, all original I/O and payload owners released, and no process,
descriptor, CLI or cleanup debt remained. Memory events and swap were zero;
stopped/no-OOM state, removal and exact container absence were verified.
`complete_decode` remains false. These results complete the controlled initial
Q4 case set with the direct cases, while application behavior, native arm64,
real-source audio, Discord, listening and release readiness remain unqualified.
