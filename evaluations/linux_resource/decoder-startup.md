# Stock FFmpeg startup check preparation

Issue: [#7](https://github.com/slackysoba/sobafm/issues/7). Current status: **binary
supply recovered; exact image/binding reviewed; fixed no-input startup rejected**.
See [recovery and actual startup evidence](decoder-supply.md). The initial supply
attempts below remain historical evidence; no limits were increased.
This is one supplemental-limit rejection check within ADR 0001, not a new product
cap or a Q3 pass. No fixture, audio, provider, credentials or Discord input exists.

The harness also supports the separately reviewed [mpg123 follow-up](mpg123-proposal.md):
`--expected-mpg123-sha256` selects only `/usr/bin/mpg123 --version`, while the
existing FFmpeg flag retains its original fixed command. Exactly one flag is
required. Both paths keep the same exec gate, limits, stream bounds and cleanup
ownership; results identify the decoder, fixed arguments and executable hash.
The current harness does not accept an arbitrary command or media arguments.
Historical result schemas and source hashes remain tied to their original runs.

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

The exact proposed transitive binary closure is **resolved and reviewed for the
recorded bounded assembly attempts**. The [selected result](results/2026-10-27-debian-metadata.yaml) records
authenticated metadata, 205 additions, zero upgrades/removals and the retained
manifest hash. During that metadata-only phase, no `.deb` files were downloaded,
installed or executed. The initial assembly attempts below acquired some
binaries but did not produce a complete verified archive set or result image.
The later [retained-cache recovery](decoder-supply.md) verified the unchanged full
set, installed it offline, and passed actual binary-binding review before the
recorded startup rejection. Metadata alone does not qualify startup or audio.

The first metadata attempt failed closed when APT could not create its signature
verification temporary file on the read-only root. The reviewed correction put
`TMPDIR` and empty APT configuration directories inside the existing bounded
private tmpfs; authentication, resource and time limits were unchanged. The second
attempt authenticated all three release records and completed in 25.954 seconds.
Both containers were observed stopped with PID zero, removed normally and verified
absent. Both attempts' exact scripts and receipts are retained; cleanup debt is zero.

The base contains 87 packages. The 205 proposed additions total 133,190,124 binary
download bytes and an estimated 466,388,992 installed bytes. These are APT planning
figures, not measured runtime memory or installed disk usage. All selected binary
records are amd64/all from Debian main; no development/debug/source package was
selected. Five print-URI rows omit a legacy digest, while every selected Packages
record supplies SHA256. The full hash manifest, not URI text alone, binds the set.
The [complete public proposal](results/2026-10-27-debian-package-proposal.yaml)
is a faithful YAML representation of the retained JSON: decoded data equality
was checked, and the result record distinguishes both serialization hashes.
Its historical pending-review fields describe the metadata step, not the later
coordinator release. It contains public package/source/image identities only.

### Acquisition outcome

The [assembly attempts](results/2026-10-27-debian-build.yaml) used the pinned base,
all 205 exact versions/hashes and ordinary APT. The first stopped before binary
download because a generated package-pin text file had CRLF endings, which the
Linux shell passed as part of each version. Its exact inputs were preserved. The
reviewed correction wrote LF explicitly and checked the raw bytes before APT;
it changed no package, base, authentication rule or resource limit.

The corrected attempt passed the base and exact transaction checks, then acquired
some binaries. Repeated HTTPS connection failures, including TLS unexpected EOF,
prevented completion; the 240-second download deadline returned exit 124. The
outer build returned failure normally after 269.328 seconds. The full archive
verification and network-disabled installation stages did not run. No output
image/tag resulted. One final, explicitly released transport retry used the
identical corrected context and limits. It also stopped at download exit 124
after 275.515 seconds overall; no direct TLS error was reported in that attempt
before the deadline. Acquisition then stopped with no further retry.

No decoder was invoked, and neither a startup rejection nor a resource-feasibility
result was measured. All three attempts have explicit completed BuildKit
solve/step failure evidence, so no uncertain active-build ownership remains;
client exit alone would not establish that. Selected receipts and scoped cache
observations are retained. No cleanup or pruning was performed.

That supply gate was subsequently cleared by the [recovery procedure](decoder-supply.md).
The transport outcome did not invalidate the signed metadata evidence or justify
changing the runtime cap or source-access restriction. The fixed supplemental
DATA/STACK candidate then failed its no-input startup check; no audio followed.

Use only Debian's signed binary repositories for trixie, trixie-updates and
trixie-security, with the existing base's Debian archive keyring. No `deb-src`,
source/debug-source package, source archive, third-party executable or new key.
Require APT authentication and validity checks; no trusted/allow-insecure,
allow-unauthenticated, expired-metadata bypass, or changed host configuration.
[APT trust chain](https://manpages.debian.org/trixie/apt/apt-secure.8.en.html).

The metadata step is development tooling in a disposable container of the same
pinned base. Its memory and memory-plus-swap limits are **536,870,912 bytes**,
with two CPUs, 64 tasks and the same nonroot/default-seccomp/no-capability
security settings. This separate preparation budget avoids confusing APT cache
generation with decoder startup fit. It changes no product cap and supplies no
runtime evidence: the actual decoder startup remains exactly 90,177,536 group
bytes plus DATA 12,000,000 and STACK 524,288, with no tuning.
Permit network only for signed metadata retrieval, use private bounded writable
APT lists/cache storage, and capture bounded output outside the container.
Run as UID/GID 1000.
Do not reuse a host APT cache or modify its sources. Stop if metadata cannot fit
its preparation allocation or authenticate; do not raise either limit implicitly.

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
`--simulate` reports changes without applying them. The executed procedure bounded
update to 35 seconds and other calls to 20 seconds, inside a 55-second container
budget and a 120-second host budget including capture/cleanup. The child stdout
cap was 4 MB, host selected output cap 5 MB and retained evidence allocation 10 MB.
Stop on overflow. Retain
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

The resolved base includes glibc `2.41-12+deb13u4` and OpenSSL
`3.5.7-1~deb13u2`; the added GnuTLS package is `3.8.9-3+deb13u4`.
For the fixed private no-input action, the reviewed glibc loader findings
CVE-2026-95818/86805 require privilege-transition conditions excluded by nonroot,
no-new-privileges and verification of the actual executable's mode/capabilities.
The resolver finding CVE-2026-8674 has no supplied hostname/search-domain workload;
the fresh environment omits LOCALDOMAIN and startup has no network.
[Loader finding](https://security-tracker.debian.org/tracker/CVE-2026-95818),
[second loader finding](https://security-tracker.debian.org/tracker/CVE-2026-86805),
[resolver finding](https://security-tracker.debian.org/tracker/CVE-2026-8674).

OpenSSL's selected base version is still affected by CVE-2026-84784 and
CVE-2026-84782; Debian's `u3` fixes them. Their QUIC peer-frame and DTLS
retransmission triggers are absent from this version-only action. This is a
specific exposure assessment, not a claim those vulnerabilities are fixed or
permission to change the pinned base. Normal APT HTTPS acquisition is separate
from startup and does not use QUIC/DTLS. Native initialization, actual ELF binding
and trusted image configuration still require review before startup; no audio
or general release clearance follows.
[QUIC finding](https://security-tracker.debian.org/tracker/CVE-2026-84784),
[DTLS finding](https://security-tracker.debian.org/tracker/CVE-2026-84782).

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
