# Stock FFmpeg startup check preparation

Issue: [#7](https://github.com/slackysoba/sobafm/issues/7). Status: **prepared;
metadata resolution, binary acquisition, image build and native startup not run**.
This is one supplemental-limit rejection check within ADR 0001, not a new product
cap or a Q3 pass. No fixture, audio, provider, credentials or Discord input exists.

## Question and fixed configuration

Can stock Debian `ffmpeg=7:7.1.5-0+deb13u1` start and print `-version` after
soft **and** hard `RLIMIT_DATA=12000000` and `RLIMIT_STACK=524288` take effect?
Use one amd64 configuration, once; at most one rerun is permitted for a
demonstrated harness error, with the error and correction retained. An actual
decoder startup rejection ends this check. Do not tune limits or try other images.

The existing pinned base is locally identified as `python3.14-trixie-slim`:

```text
ghcr.io/astral-sh/uv@sha256:8f4ab6a0f2cc074290ba6a136eb016350b2b6b93a2108e07b72724901ad1914f
```

Its local architecture is amd64 and logical image size is 258,079,780 bytes.
Before any later use, verify `/etc/os-release`, native architecture, existing
package inventory and archive keyring inside that exact base. Do not infer those
facts solely from a mutable tag. There is no arm64 execution claim.

The eventual startup container keeps the reviewed settings: nonzero UID/GID,
builtin seccomp, no added capabilities, no-new-privileges, private cgroup/PID
namespaces, read-only root, no network, init, no restart, two CPUs, 64 tasks,
memory and memory-plus-swap both 90,177,536 bytes. The existing group/parent/copy
ledger stays **98,979,840 bytes**. The supplemental limits apply only to the
fork child before decoder exec; the observer stays inside the original group.

Conditional arithmetic is `2 * (12000000 + 524288) = 25048576` native bytes and
`25048576 + 8802304 = 33850880` PCM bytes. This is **not yet a PCM bound**:
Linux DATA covers a mapping class, not all retained payload. A future proof must
cover every PCM allocation/transient, all stacks and mappings, process
multiplicity, protection changes and cleanup debt on the actual kernel/libc.
`RLIMIT_STACK` sets the initial default NPTL thread-stack size, not a universal
hard per-thread ceiling. No sampled RSS or startup success fills this gap.
[Linux limits](https://man7.org/linux/man-pages/man2/getrlimit.2.html),
[v6.18 mapping classes](https://raw.githubusercontent.com/torvalds/linux/v6.18/mm/vma.h),
[v6.18 enforcement](https://raw.githubusercontent.com/torvalds/linux/v6.18/mm/mmap.c),
[NPTL stacks](https://man7.org/linux/man-pages/man3/pthread_create.3.html).

## Supply gate before acquisition or build

The exact transitive binary closure is **unresolved**. Preparing commands is not
the same as running APT or qualifying that closure. Independent coordinator review
must first authorize the metadata-only step below. It neither downloads `.deb`
files nor installs or executes a decoder. After it runs, review the exact result
before binary acquisition/build; any unresolved applicable security or
source-access issue stops dependent execution. Do not request a blanket waiver.

Use only Debian's signed binary repositories for trixie, trixie-updates and
trixie-security, with the existing base's Debian archive keyring. No `deb-src`,
source/debug-source package, source archive, third-party executable or new key.
Require APT authentication and validity checks; no trusted/allow-insecure,
allow-unauthenticated, expired-metadata bypass, or changed host configuration.
[APT trust chain](https://manpages.debian.org/trixie/apt/apt-secure.8.en.html).

The metadata step is a disposable container of the same pinned base with the
same resource/security settings. Permit network only for this signed metadata
retrieval, use a private bounded writable directory for APT lists/cache, and
mount one new empty project-owned evidence directory. Run as UID/GID 1000.
Do not reuse a host APT cache or modify its sources. Stop if metadata cannot fit
or authenticate; do not raise the experimental limits implicitly.

Prepare an ordinary deb822 file in that private directory with `Types: deb`,
`Components: main`, `Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg`,
and these two entries: `URIs: https://deb.debian.org/debian` with
`Suites: trixie trixie-updates`; and
`URIs: https://security.debian.org/debian-security` with `Suites: trixie-security`.
Use APT options selecting only this file, no source-parts directory, private
`Dir::State::lists` and `Dir::Cache`, and the base's read-only dpkg status file.
For this isolated, metadata-only nonroot operation, `Debug::NoLocking=true`
avoids attempting to lock the immutable dpkg database; no installer may run.

The concrete APT operations, with these same directory options on each call, are:

```sh
apt-get -o APT::Update::Error-Mode=any update
apt-get --simulate --no-install-recommends --no-remove \
  install ffmpeg=7:7.1.5-0+deb13u1
apt-get --print-uris --download-only --yes --no-install-recommends --no-remove \
  install ffmpeg=7:7.1.5-0+deb13u1
```

`--print-uris` reports acquisition locations instead of fetching package files;
`--simulate` reports changes without applying them. Wrap update in a 45-second
timeout and each subsequent call in 20 seconds, with a 120-second outer bound
including capture/cleanup. Cap total output at 5 MB; stop on overflow. Retain
errors, exact command/options, APT version, signed InRelease identities/digests
and authentication result. [APT command semantics](https://manpages.debian.org/trixie/apt/apt-get.8.en.html).

From the proposed transaction, enumerate **every** new or changed package with
name, exact version including epoch, architecture, binary filename/size/SHA256,
source-package name/version and dependencies. Use the selected signed Packages
records via APT; `--print-uris` alone may report a different hash algorithm and
is not sufficient. Retain the base's full installed-package inventory separately,
bound to its image digest. Include libc/loader, native codec/filter/format/device,
resampling/scaling/postprocessing libraries and all their transitive components,
plus existing Python, APT/keyring and shell/core utilities used in preparation.
Do not claim that the top-level FFmpeg package is the complete native inventory.

The published amd64 `.deb` is 2,001,944 bytes with SHA256
`8669bc77e55393c14e0dd257ba9a916007cb6da0e1074c9b5e1d53812024f2af`.
This is a reference to compare against signed metadata, not a downloaded binary
or executable hash. [Exact binary metadata](https://packages.debian.org/trixie/amd64/ffmpeg/download).

After the complete manifest is reviewed, a later ordinary image build must use
the pinned base and install all approved versions explicitly with
`--no-install-recommends --no-remove`, retaining authentication/hash evidence.
An unavailable or changed dependency stops the build; never silently resolve a
replacement. Install no development/source packages and perform no native build.
Preserve installed copyright notices. Record actual installed package inventory,
ELF executable and runtime-library hashes, source-package identities, build
configuration, source commit, mounted harness hashes and resulting image digest.
Review this completed binding before the startup invocation. No build recipe or
unresolved version list in this change is represented as an approved lock.

## Licensing and no-input security assessment

Debian's exact copyright record says its default binaries are GPL v2 or later;
the extra libavcodec/libavfilter flavor is effectively GPL v3 or later. Debian
does not use the nonfree combinations described there. Select the ordinary
flavors and record every resolved component's applicable notices/license.
This fixes the previous retrieval gap, not all dependency or redistribution
obligations. Private local evaluation and image publication remain distinct.
No publication is proposed. The existing prohibition on `fftools/ffmpeg_filter.c`,
`18.raw` and source archives containing them remains unchanged.
[Exact Debian copyright](https://metadata.ftp-master.debian.org/changelogs//main/f/ffmpeg/ffmpeg_7.1.5-0%2Bdeb13u1_copyright),
[security changelog](https://metadata.ftp-master.debian.org/changelogs//main/f/ffmpeg/ffmpeg_7.1.5-0%2Bdeb13u1_changelog).

The upstream n7.1.5 `version` option is `OPT_EXIT`; its handler prints program
and library versions. Main still initializes logging, devices, networking
libraries and the scheduler before processing it. Thus the claim is no media
demux/decode/filter workload, **not** zero native initialization. The child gets
only fixed arguments, `/dev/null` stdin and a fresh minimal environment; no
FFREPORT, preload, plugin path, inherited user configuration or media mount.
Network is disabled in the startup container. The image/root and notices are
trusted reviewed inputs; transitive loader/constructor/configuration exposure
still requires the exact dependency inventory.
[Main](https://raw.githubusercontent.com/FFmpeg/FFmpeg/n7.1.5/fftools/ffmpeg.c),
[version option](https://raw.githubusercontent.com/FFmpeg/FFmpeg/n7.1.5/fftools/opt_common.h),
[handler](https://raw.githubusercontent.com/FFmpeg/FFmpeg/n7.1.5/fftools/opt_common.c),
[exit handling](https://raw.githubusercontent.com/FFmpeg/FFmpeg/n7.1.5/fftools/cmdutils.c).

The stable security tracker has postponed vulnerabilities. For example,
CVE-2026-66037 requires crafted IAMF demux input, CVE-2026-107678 requires crafted
MOV/MP4 encryption metadata, and CVE-2026-66039 concerns crafted MACE6/CAF audio.
Those trigger paths have no supplied input in this exact `-version` action;
this is exposure reasoning, not package-wide clearance. CVE-2026-30999's tracker
note classifies it as a CLI memory leak with no security impact, despite its
headline. Check loader/initialization and all resolved packages separately;
record any applicable finding, supported fix or required maintainer decision.
Untrusted-audio testing and Q3 remain uncleared.
[Tracker](https://security-tracker.debian.org/tracker/source-package/ffmpeg),
[IAMF](https://security-tracker.debian.org/tracker/CVE-2026-66037),
[MOV](https://security-tracker.debian.org/tracker/CVE-2026-107678),
[MACE6](https://security-tracker.debian.org/tracker/CVE-2026-66039),
[CLI leak classification](https://security-tracker.debian.org/tracker/CVE-2026-30999).

## Startup invocation and release ownership

`decoder_startup.py` is standard-library-only. The parent verifies the approved
ELF SHA256 before launch and checks the original cgroup/security contract. Its
single-threaded fork child installs both hard/soft limits, reports readiness,
and blocks on a pipe. The parent reads that child's real `/proc` limits and
security/membership before opening the exec gate. The child then executes only
`/usr/bin/ffmpeg -nostdin -version`. There is no post-exec input gate to race.

The Python child may already have mappings larger than DATA before exec;
lowering a limit does not reclaim them. A failure before exec is **incomplete**
setup evidence, not a measured FFmpeg rejection. A close-on-exec status pipe
distinguishes an explicit setup/exec error. No custom allocator or native shim
is introduced. Fixed stdout/stderr capacities are 16,384 bytes each, with
bounded reads and base64 receipts. Overflow fails the check. The startup deadline
is ten seconds including setup; cleanup has one original three-second deadline.

After supply review, substitute the actual immutable result image and approved
ELF hash in a private invocation record. Reuse the [Q1/Q2 external ownership
procedure](README.md#reviewed-invocation), with a new container name/case
`sobafm-q7-startup`/`startup`, the unchanged settings above, and a read-only mount
of `evaluations` at `/evaluations`. The fixed container command is:

```sh
python -I -B -c \
  'import sys; sys.path.insert(0, "/"); from evaluations.linux_resource.decoder_startup import main; raise SystemExit(main())' \
  --execute-owned-startup --expected-ffmpeg-sha256 "$approved_elf_sha256"
```

The explicit import root contains only the pinned image and read-only evaluation
mount. Do not mount a repository root, home directory, credentials, runtime socket
or environment file. There are no arbitrary executable/argument overrides.
Capture source, image, manifest and harness hashes outside the container. Keep
the runtime log limit at two files of 1 MB. Use the earlier eight-second bounded
Docker management calls, a 25-second attach bound and a 180-second total host
deadline so inspection and owned cleanup have reserved time.

Always inspect and preserve exit/OOM/state evidence, including when the observer
is killed. Reverify exact ID/labels before stopping; an uncertain create or stop
retains debt. Stopping the client or sending a signal does not release ownership.
Require stopped container state and PID zero; after evidence capture, remove only
that verified stopped ID without force and verify absence. Missing final JSON,
retained descendants, unreaped leader, timeout, output overflow or uncertain
cleanup means incomplete/failure. External cleanup evidence remains mandatory
even if the in-container result reports no debt. No other resource is removed.

Successful zero exit with the expected version/build header is
`startup_only_pass`; an observed post-exec nonzero exit is `startup_rejected`.
Neither outcome qualifies PCM coverage, decoder buffering, useful fit, Q3, Q4,
native arm64, application behavior or release readiness. Do not allocate banks,
generate fixtures or continue to media in this check.

## Storage and verification boundary

Proposed incremental acquisition/build ceiling: 2 GB; selected execution evidence:
10 MB; new selected preparation docs/harness: 5 MB. These fit only after current
headroom and coordinator allocation are confirmed within the existing storage
agreement. Logical shared image/cache totals overlap and must not be added.
Recheck filesystem free space and project growth before heavy work and after it;
retain required notices/manifests and compact results, not decoded output or
intermediate archives. No new local Python environment/cache is required.

Offline regression tests cover altered/missing limits, output overflow, EOF with
a live process and a reaped leader with retained descendants. Standard locked
sync, pre-commit and pytest are required before push. These checks run no decoder,
Docker, provider or network and cannot establish any native startup result.
