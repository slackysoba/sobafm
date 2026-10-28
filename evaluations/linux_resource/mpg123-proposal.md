# Pinned mpg123 supply and startup

Issue [#7](https://github.com/slackysoba/sobafm/issues/7). Status: **authenticated
metadata, binary binding and no-input startup complete; useful decode and Q3/Q4 pending**.
The [complete proposal and selected operation evidence](results/2026-10-27-mpg123-proposal.yaml)
record all 27 proposed additions and the unchanged 87-package base. This follows
the [fixed FFmpeg startup rejection](decoder-supply.md). The completed mpg123 run
changes no numeric limit, PCM requirement or supported-input requirement. A
distinct, stricter address-space startup variant is prepared below, not executed.

## Resolved transaction

The reviewed metadata-only operation completed in **41.140 seconds** at source
`882ae080abe10eef02a2966e66388ac4043c2a4b`, on the original pinned Linux amd64 base
`ghcr.io/astral-sh/uv@sha256:8f4ab6a0f2cc074290ba6a136eb016350b2b6b93a2108e07b72724901ad1914f`.
Execution occurred on October 27 in America/Los_Angeles, October 28 UTC.
APT reported **0 upgraded, 27 newly installed, 0 removed and 7 not upgraded**.
Every original package/source/version record and the existing signing keyring
matched the previous base evidence. There was no libc or other base replacement.

| Selected binary package | Exact version | Source package/version | Archive bytes |
| --- | --- | --- | ---: |
| mpg123 | 1.33.7-1 | mpg123 1.33.7-1 | 210,920 |
| libmpg123-0t64 | 1.33.7-1 | mpg123 1.33.7-1 | 154,156 |
| libout123-0t64 | 1.33.7-1 | mpg123 1.33.7-1 | 32,016 |
| libsyn123-0t64 | 1.33.7-1 | mpg123 1.33.7-1 | 97,888 |
| libportaudio2 | 19.6.0-1.2+b3 | portaudio19 19.6.0-1.2 | 64,768 |

Only the four mpg123-family packages come from **forky**. The other 23 additions
come from stable. Twenty-two have exactly the same version, architecture, source,
filename, size and SHA256 as the previously verified cache manifest, totaling
8,319,864 bytes. The five entries above are new or changed and total 559,748 bytes.
No cached payload was read or copied in this metadata slice. Later reuse still
requires authorization and rechecking each complete file's size and hash.

The complete proposal totals **8,879,612 archive bytes** and **19,378,176 bytes of
declared Installed-Size**. The latter is a packaging estimate, not an image size
or runtime memory measurement. Declared dependency compatibility with the pinned
base is established; actual loader/runtime compatibility remains untested.

## Isolation and authentication

The operation reused the nonroot [metadata procedure](decoder-startup.md), with
private source, preference, list, cache and temporary directories. Stable,
stable-updates and stable-security remained available for dependencies. One
additional official `forky main` binary index was enabled using the existing
Debian archive keyring. No source index, new key or authentication/expiry exception
was introduced. APT's error-on-any-update-failure and validity checks remained on.

The isolated preferences were:

```text
Package: mpg123 libmpg123-0t64 libout123-0t64 libsyn123-0t64
Pin: release n=forky
Pin-Priority: 500

Package: *
Pin: release n=forky
Pin-Priority: -1
```

All four requested versions were explicitly `1.33.7-1`. The simulation used
`--no-install-recommends --no-remove --no-upgrade`; additional checks rejected
every proposed change to an installed package, any libc change, or a nonzero
upgrade/removal count. The full package policies confirm that all other selected
versions had eligible stable origins. APT prints the generic forky index priority
of -1 beneath the four packages' effective version priority of 500: the specific
package pin takes precedence. [APT preference semantics](https://manpages.debian.org/trixie/apt/apt_preferences.5.en.html).

APT authenticated four InRelease records. The proposal preserves each identity,
SHA256, date and published validity: forky and trixie-updates expire November 4
at 02:08:46 UTC; trixie-security expires November 3 at 21:13:57 UTC. The stable
trixie Release has no Valid-Until field. No missing or expired record was accepted
by disabling validation. Selected package records include exact binary and source
identities, architecture, filename, size, SHA256, dependencies and official URI.

The container retained UID/GID 1000, default seccomp, no capabilities,
no-new-privileges, a read-only root, private cgroups and a 256 MiB noexec tmpfs.
The development memory cap was 512 MiB with no swap, two CPUs and 64 tasks.
Bounds were 210 seconds in the container, 240 seconds for the host operation plus
60 seconds for ownership cleanup, 3.9 MB stdout, 4 MB operator output, 5 MB selected
evidence and 500 MB incremental development storage. These are metadata-preparation
resources. Runtime group 90,177,536, DATA 12,000,000, STACK 524,288 and the existing
PCM accounting remain unchanged.

The container exited zero without an OOM kill, was observed stopped with PID zero,
removed normally and verified absent. Cleanup debt is zero. Only metadata was
retrieved: no binary acquisition, installation, decoder, audio or provider activity
occurred. No host APT or security setting changed.

## License, security and source impact

The upstream August 2026 release fixes relevant decoder memory-safety and error
handling issues, including layer III uninitialized values and double-free paths.
The four explicit pins prevent the new CLI from retaining older libraries that
would still satisfy its declared minimum versions. Debian's current tracker
lists 1.33.7-1 in forky; an assigned-CVE list alone is not a complete review of
upstream fixes. [Upstream release notes](https://www.mpg123.de/),
[Debian tracker](https://security-tracker.debian.org/tracker/source-package/mpg123),
[exact Debian changelog](https://metadata.ftp-master.debian.org/changelogs/main/m/mpg123/mpg123_1.33.7-1_changelog).

The exact mpg123 source and Debian packaging copyright record specifies LGPL 2.1.
The selected PortAudio source record contains its permissive MIT-style license
and notice-retention requirement. Their retrieved document hashes are in the
proposal. The PortAudio tracker showed no issue entries at this review; that is
not an independent assurance of safety. Its selected stable version is 19.6.0-1.2,
while forky offers a newer version that was deliberately ineligible for this
transaction. [mpg123 copyright](https://metadata.ftp-master.debian.org/changelogs/main/m/mpg123/mpg123_1.33.7-1_copyright),
[PortAudio copyright](https://metadata.ftp-master.debian.org/changelogs/main/p/portaudio19/portaudio19_19.6.0-1.2_copyright),
[PortAudio tracker](https://security-tracker.debian.org/tracker/source-package/portaudio19).

The remaining selected package identities are unchanged from the previous
proposal; this permits evidence reuse without transferring the previous FFmpeg
invocation's exposure assessment to a different program. The pinned base's known
security findings remain recorded in the [earlier narrow assessment](decoder-startup.md#licensing-and-no-input-security-assessment).
Actual executable/library identities, constructors, output modules, notices and
the precise future command require review before execution. The official newer
package lists `/usr/bin/mpg123` and output modules; a stable wrapper name must not
be assumed. [Published file list](https://packages.debian.org/forky/amd64/mpg123/filelist).

This proposal uses maintained distribution binaries with a narrowly selected
testing-suite family. That mixed-suite maintenance obligation remains explicit;
it is not production adoption or a security exception. Source/version identities
and exact notices must survive any later image assembly. No source archive or
restricted source file was acquired, read, hashed or reconstructed. Image
redistribution and complete corresponding-source obligations remain a separate
gate; no image publication is proposed.

## Acquisition and startup result

The subsequent released acquisition verified all 27 archives: 22 existing files
were rehashed and hardlinked, and the five new files all completed on their first
HTTPS HTTP/1.1 attempt. Offline assembly completed in 12.672 seconds at source
`f293738d9d5f42b861e3f4c0e8ae38b6c4848476`, with no active build debt. The actual image
is `sha256:e3ff7b02740b55f52735b17afc9846d151f86bf7395341f5d53f7d3dbb235cfc`,
Linux amd64, final user 1000:1000, logical size 287,470,219 bytes. Its 114-package
inventory exactly matched the proposal plus base; the binding contains 691 ELF
path records and all 114 copyright notices. A never-started receipt container
was removed and verified absent. The image remains private evaluation material;
its inherited upstream OCI license label is not a combined-license claim.

After independent review of the actual image, binding and harness, the single
fixed `/usr/bin/mpg123 --version` check **passed** at source
`4071d187b5f21abe4c62ccb24e8453ae8f28c600`. The executable SHA256 was
`62ef82208722eda0fedd39de0ced0d8e3a8450d129760152ea16d075651f0a91`.
The parent verified the child's actual hard/soft DATA 12,000,000 and STACK 524,288
limits before releasing the exec gate. Group memory remained 90,177,536 with no
swap; nonroot/default-seccomp, no capabilities, no-new-privileges, read-only root,
private cgroups and network none remained in force.

The child exited zero with `mpg123 1.33.7` and a trailing newline on stdout and
empty stderr. The leader was reaped, no descendants remained and the original
three-second cleanup deadline was met. The container was observed stopped with
PID zero, was not OOM-killed, and was removed and verified absent. Internal and
external cleanup debt are zero. The outer operation took 7.125 seconds, including
container setup and observation; this is not a decoder latency measurement.
The [selected result](results/2026-10-27-mpg123-startup.yaml) preserves the actual
inventory, native hashes, preflight, gate, result and separate build/startup source
bindings. No second startup run, limit tuning or audio followed.

This is a startup-only pass. Metadata preparation, assembly, startup and later
documentation checks have distinct source bindings. None is silently rebound to
another commit or architecture. The existing failed FFmpeg candidate remains
valid historical evidence.

## Stricter address-space startup preparation

The next reviewed preparation adds hard and soft **RLIMIT_AS 12,524,288 bytes**
to the existing DATA 12,000,000 and STACK 524,288 limits. This is a distinct
candidate: the passing DATA/STACK receipt above did not set or test AS. The
fixed `--mpg123-address-space-limit` flag selects it; there is no configurable
numeric limit or alternate command. It is restricted to the same pinned
`/usr/bin/mpg123 --version`, executable hash and immutable image. No audio input,
new package, resampler execution or target change is part of this startup check.

The single-threaded fork child sets both AS limits before reporting ready. The
parent verifies the actual child identity, group, security settings and all three
soft/hard limits through `/proc` before releasing exec. Missing, changed,
unlimited or duplicate AS evidence prevents release. Gate and result records
identify `data-stack-as`; the original mode remains `data-stack`. Setting a limit
below the Python launcher's existing mappings does not reclaim them. Setup or
exec failure remains incomplete evidence, distinct from native loader rejection.
The child receives `/dev/null` as stdin and inherits no PCM in this no-input run.

The container retains memory 90,177,536, zero swap, two CPUs, 64 tasks,
UID/GID 1000, default seccomp, no capabilities, no-new-privileges, private cgroups,
read-only root and network none. Startup remains ten seconds, output 16,384 bytes
per stream and cleanup one original three-second deadline. The external operator
retains its 90-second operation plus 90-second ownership-cleanup bounds, 250 KB
output and 500 KB selected evidence. It must verify source/harness hashes and
exact image/ELF identities, preserve the separate receipt, and stop after this
one result. Neither another run nor limit tuning follows automatically.

### Source-derived scope of the candidate

The exact running-family Microsoft WSL kernel tag `linux-msft-wsl-6.18.33.2`
checks ordinary virtual-memory growth against AS before DATA classification or
the `ignore_rlimit_data` exception. `mmap`, `brk`, stack growth, expanding `mremap`
and SysV attachment use that accounting. Private/shared, file/anonymous,
read-only and PROT_NONE mappings and all thread stacks consume the same process
address space; changing protection does not erase their extent. This closes the
ordinary mapping/allocator-class gap left by DATA alone.
[Kernel accounting](https://github.com/microsoft/WSL2-Linux-Kernel/blob/linux-msft-wsl-6.18.33.2/mm/mmap.c),
[mapping and stack growth](https://github.com/microsoft/WSL2-Linux-Kernel/blob/linux-msft-wsl-6.18.33.2/mm/vma.c),
[remapping](https://github.com/microsoft/WSL2-Linux-Kernel/blob/linux-msft-wsl-6.18.33.2/mm/mremap.c),
[SysV attachment](https://github.com/microsoft/WSL2-Linux-Kernel/blob/linux-msft-wsl-6.18.33.2/ipc/shm.c).

This is not a universal assertion that every kernel-inserted VMA is enforced by
AS. `__install_special_mapping` accounts special mappings without the ordinary
growth check. x86 vDSO/vvar contain kernel code/time data, not PCM in the inspected
path; they are an explicit scope exclusion. No numeric special-map allowance has
been established or spent. The ELF loader installs vDSO after replacing the old
image, so `/proc/exe` changing or a close-on-exec pipe reaching EOF alone does not
prove all exec setup finished. The completed native `--version` output and
termination can establish startup; future input admission needs a source-backed
native user-code boundary plus actual mapping/limit identity evidence.
[Special mapping setup](https://github.com/microsoft/WSL2-Linux-Kernel/blob/linux-msft-wsl-6.18.33.2/arch/x86/entry/vdso/vma.c),
[exec lifecycle](https://github.com/microsoft/WSL2-Linux-Kernel/blob/linux-msft-wsl-6.18.33.2/fs/exec.c),
[ELF loading](https://github.com/microsoft/WSL2-Linux-Kernel/blob/linux-msft-wsl-6.18.33.2/fs/binfmt_elf.c).

AS bounds mapped extents, not entire backing objects or kernel I/O storage.
Kernel pipes, parent copies, unmapped retained file/memfd/SysV pages and other
processes still require explicit reservations or exclusion. For a future fixed
raw-stdout path, source review must exclude PCM files/shared backing and output
helpers, account pipes/copies, and retain at most **two decoder address spaces**,
including forked descendants and cleanup debt. A 64-task limit is not that
two-address-space ownership rule. Pre-exec inherited PCM remains charged until
the old image is actually released; launching a replacement must not hide a copy.

Conditional on those premises, the conservative PCM capacity arithmetic is
`2 * 12,524,288 + 8,802,304 = 33,850,880` bytes, leaving 709,120 below 34,560,000.
AS already includes stacks: STACK must not be added again. On 4 KiB pages the
ordinary-mapping ceiling rounds down to 12,521,472 bytes, so this arithmetic
does not spend rounding slack. The whole-service controlled ledger remains
98,979,840. This calculation is not yet useful-fit or Q3 qualification.

## Next bounded gate

Independently review the exact committed variant and operator invocation, then
release one no-input AS startup check. No AS result exists yet. If it starts,
prepare the bounded owned-fixture proof with the remaining input, mapping,
completion and release conditions explicitly enforced.

The source audit identifies the supported NtoM resampling mode as a possible
resource-probe path that avoids the fine resampler's extra buffers and an inferred
allocation-failure cleanup defect. It does not approve NtoM for music fidelity or
authorize a resampler run. A successful process exit alone must not be treated
as complete audio: the inspected CLI can exit zero after a decoding error.
[Version-specific options](https://scm.orgis.org/mpg123/tags/1.33.7/man1/mpg123.1),
[audio path](https://scm.orgis.org/mpg123/tags/1.33.7/src/audio.c),
[resampler allocation and cleanup](https://scm.orgis.org/mpg123/tags/1.33.7/src/libsyn123/resample.c),
[CLI completion](https://scm.orgis.org/mpg123/tags/1.33.7/src/mpg123.c).

No useful mpg123 workload has run. Any MP3 result would remain partial: FLAC,
aggregate PCM coverage, Q4 cleanup, native arm64 and real source/Discord acceptance
remain outstanding. The existing product guarantees still apply.
