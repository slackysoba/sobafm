# Native decoder supply

SobaFM's M5 decoder uses the minimal shared FFmpeg route selected by
[ADR-0007](decisions/0007-bound-catalog-audio-decoding.md). SobaFM application code
remains MIT. The native exception covers the exact inventoried decoder and its
necessary support contributions; it does not change the Python dependency policy.

This document describes the supply contract for [#174](https://github.com/slackysoba/sobafm/issues/174).
No native package is currently approved for production distribution or catalog
readiness. Corrected Linux x86-64 and Windows cross-builds have complete checked
compiler-header, source, linker-member and installed-file mappings. The earlier
candidate passes compiler-free native Windows/Linux decoding and reproduces both
complete Linux archives from a separate recipient build; its real changed-library
replacement also loads and decodes with the unchanged executable. The recipe now
explicitly disables unused iconv support. Its fresh Linux and Windows builds pass,
including Linux CONFIG_ICONV=0 and no iconv imports, and require refreshed paired
packages and recipient/platform qualification. Windows replacement, native ARM64
execution and independent final review remain required. Cross-compilation or
emulation alone does not establish native-platform qualification.

## Exact identity and readiness

The recipe under `native/ffmpeg/` pins FFmpeg source, upstream commit, patches,
signature/key identities, configuration, source timestamp and isolated toolchain.
The current candidate is FFmpeg 9.0.2, upstream commit
`946fcce07b6dcd0331c8cc609192aeff5e1924f8`, with no SobaFM patches. Its source
archive SHA-256 is
`8c3850283eb25fa026482078a04051e0be17347b09ef81a0849bec15a96e002e`.
A valid signature authenticates the source; it does not establish its security or
qualification. Recheck the pin and current advisories when selecting a shipping
build.

Packaging validates generated configuration, actual compiled dependencies,
linker-selected objects/archive members and the installed binary closure. It
rejects GPL, nonfree, version3, network, iconv or static FFmpeg configurations and
unreviewed capabilities. The one selected recipe enables the MP3 decoder/parser/
demuxer, s16le PCM encoder/muxer, pipe and required conversion support; common
library code can compile even when unrelated codecs are disabled.

Every target has a manifest binding exact runtime bytes, architecture, relative
paths/symlinks, source archive, corresponding source/notices bundle, frozen recipe,
toolchain and component ownership by digest. The index binds the complete runtime
archive and its exact source archive. Supply verification uses an independently
reviewed expected manifest digest. A caller-provided or locally edited manifest
does not grant production readiness. There is no arbitrary PATH-binary fallback.

`verify_approved_native_runtime()` reads the application-owned
`src/sobafm/native-trust.json`, currently empty, or an explicitly selected reviewed
operator registry outside the installed package. Approval binds target, exact
manifest and paired source-bundle hashes with a review reference. Revocation
overrides an approval. The installer/index and the low-level explicit-digest
qualification tools establish artifact integrity; their output cannot populate
this registry or approve themselves. A recipient replacement needs its own
operator-reviewed identity and applicable execution qualification.

Supply verification is one prerequisite. [#125](https://github.com/slackysoba/sobafm/issues/125)
and [#175](https://github.com/slackysoba/sobafm/issues/175) must additionally qualify
pre-input child containment, bounded audio accounting, strict completion and
cleanup on the actual supported installations before catalog playback is enabled.

## Package contents and component roles

Each target receives a paired compiler-free runtime archive and exact
source/notices archive. The ordinary installation needs no compiler. The source
archive supplies a frozen build recipe, original source and any patches, exact
Debian source descriptors/original/Debian archives for selected support inputs,
signed metadata used to authenticate them, generated configuration, compiler/link
correspondence, installed-file mapping and recipient build/install/verification
tools with their existing locked dependency context.

Runtime files, incorporated support code, build-only tools and operating-system
requirements have separate inventory entries. Do not infer incorporation from a
library appearing on a linker command line: an as-needed library can be inspected
and then omitted, and an archive can contribute only a few selected members.

| Component | Distribution route and evidence |
| --- | --- |
| FFmpeg executable and five shared libraries | LGPL-2.1-or-later; complete exact source/build/install materials, license text, full selected notices and recipient replacement route accompany the package. |
| IJG DCT source in FFmpeg | Preserve full original notices and upstream modification statements for `jfdctfst.c`, `jfdctint_template.c` and `jrevdct.c`; include the explicit IJG acknowledgement. `jfdctint.c` is an LGPL wrapper. |
| FFmpeg `faandct.c`, `avsscanf.c`, `adler32.c` | ISC, MIT and Zlib terms respectively; preserve exact copyright/permission/disclaimer and modified-source notices. The adler code does not imply a linked external libz. |
| GCC selected headers/startup/libgcc code | GPL-3.0-or-later with GCC Runtime Library Exception 3.1 for the actual selected contributions and eligible compilation process. Retain exact corresponding sources and the exception. Compiler executables stay build-only. |
| glibc selected startup objects and headers | Record exact incorporated inputs and applicable per-file terms with their sources. Distinguish these from the host's dynamically supplied libc, libm and loader. |
| Linux UAPI headers | Use each actual header's SPDX terms and syscall exception where present; no kernel runtime is bundled by the native archive. |
| MinGW-w64 CRT/header contributions | Preserve the actual mixed ZPL, public-domain, Lucent and other selected terms. A top-level ZPL label does not cover all selected CRT code. Distinguish incorporated CRT objects and generated import thunks from Windows-provided DLLs. |
| Binutils and cross-tool packaging | Build-only tools and source materials; not a runtime libbfd distribution. Preserve their own source/documentation terms within supplied archives. |

The final machine-readable SBOM records exact versions, source hashes, installed
file ownership, applicable licenses, source relationships and operating-system
requirements. SPDX custom license references must resolve to preserved complete
text; a label or an archive name is not source-to-object proof. Complete source
archives may contain additional independently licensed build/test/documentation
files, whose original notices remain intact.

The runtime includes a plain `NOTICE.txt`, full license texts and directly indexed
notices. A standard compressed archive of actual selected source/header notices
is acceptable accompanying material when it is included in the runtime package,
listed and hashed by the manifest, readable with normal archive tools, and its
index maps installed contributions to exact entries. Preserve entire notice blocks
without editing or truncation. A link to a remote source archive alone is not the
binary's accompanying notice set.

The IJG acknowledgement reads: “This software is based in part on the work of the
Independent JPEG Group.” Record the absence of SobaFM source changes while
retaining upstream changes already documented in those files.

The inspected Linux x86-64 candidate requires glibc symbol versions through
`GLIBC_2.35`. Its executable uses `$ORIGIN/../lib`; its libraries use `$ORIGIN`.
It needs system libc/libm and the loader; libatomic and libgcc shared libraries are
not in its final dynamic runtime closure. This inspection does not prove an
arbitrary older Linux distribution is supported. Clean installation tests determine
the supported OS baseline. A future SobaFM image adds its own base/Python/Opus/OS
closure, which is not licensed or sourced by the FFmpeg bundle alone.

## Recipient rebuild and library replacement

Keep recipient instructions with the source archive, not only in supplier logs.
They must use only the delivered recipe/tools and exact obtainable build inputs,
state platform prerequisites, authenticate source metadata, build in a fresh
location, create paired packages and install them without a compiler on the
runtime machine. Separate deterministic build/package inputs from execution logs,
timestamps, kernel details and local image IDs. Compare native payload bytes and
report any differences explicitly; packaging determinism alone is not an
independent reproducible-build result.

An independent recipient must execute the published sequence and retain commands,
exact source/runtime identities and results. The replacement qualification must
make a real ABI-compatible shared-library modification, rebuild it, replace it in
an installed package and demonstrate the changed library is loaded and works.
Relocation or loading an identical library does not meet that check. Provide any
applicable relinking or installation materials. Replacement preserves the
recipient's rights; operational qualification and the reviewed trusted identity
must be refreshed before a replacement is eligible for production catalog use.
Neither step requires compiling SobaFM application code.

From a source checkout with the locked Python environment and Docker available,
`sh native/ffmpeg/build-ci.sh linux-amd64` performs source authentication, the
isolated build, pairing, compiler-free native installation, independent recipient
rebuild, complete archive comparison and modified-library qualification. Use
`linux-arm64` on an actual ARM64 host. `windows-amd64` prepares the cross-build;
the Windows workflow separately installs and executes its original and modified
packages on Windows. These commands create fresh paths and retain failures.
They do not publish a release or update trusted identities.

Recipients can use the same sequence from `recipient-tools/` in the delivered
source bundle. `RECIPIENT.md` records the locked import context, fresh mount
layout, limits and replacement commands. `qualify_native_runtime.py extract-sources`
checks the complete source-bundle hash and every delivered tool's byte inventory;
`compare` checks actual rebuilt binaries/links, and `compare-packages` checks both
complete archive identities. `replace-library.sh` modifies the version API and
reported micro version in a separate copy of the recipient build.
`stage-replacement` supplies the original complete sources plus exact changed
source/patch; `check-replacement` proves the unchanged native executable uses the
changed library and still decodes. The original supply identity must reject it.
Its generated identity remains explicitly unqualified.

## Source access and retention

The existing release maintainer owns publication and availability. Before a native
download or image becomes public, publish its exact versioned source/notices
bundle in the same existing GitHub release and verify unauthenticated access.
Use direct accompanying source access, not a written-offer substitute. Runtime
download descriptions and package `source-access.json` identify the exact bundle
and digest; source links cannot mean “latest” or only point upstream.

For [#132](https://github.com/slackysoba/sobafm/issues/132), record a release
manifest mapping every native target/archive/manifest and image platform digest to
the correct source/notices assets. Include source/notices access in image metadata
and documentation for the registry-only pull surface, then check it from that
surface. Complete any image-only OS/Python/Opus source and notice obligations
separately, using the exact shipped closure. Confirm every referenced asset and
hash before promotion; an absent, private or mismatched bundle blocks publication.

Keep required sources/notices available while any corresponding binary remains
obtainable, including old tags/images. There is no routine source-asset deletion.
If a distribution surface becomes inaccessible or an asset is lost, restore the
exact verified asset/access and recheck every reference; pause affected new
publication until the mismatch is resolved. Keep unsuccessful and corrected
qualification records distinct. Historical unrecovered evidence remains marked
unrecovered rather than reconstructed as an original success.

Issue #174 prepares these materials and the handoff; it creates no production release
or tag and does not change [ADR-0005](decisions/0005-release-images-with-github-actions-and-artifact-attestations.md)
publication, attestation or approval requirements.

## Security review and updates

The release maintainer reviews [FFmpeg advisories](https://ffmpeg.org/security.html),
upstream release-branch fixes and advisories for every actually selected native
support component at least monthly and whenever upstream notices arrive. Triage
an applicable fix within two working days. Record the review date, exact source
and platform identities, advisory/fix references, included-code/reachability
reasoning, mitigation and remaining gap. Suspend catalog decoding for an
applicable serious unmitigated defect.

The native review covers custom FFmpeg builds, incorporated startup/archive/header
code and deployed OS runtime requirements. The Python vulnerability/license check
of `uv.lock` does not scan these. An image SBOM scanner may identify distribution
packages while missing custom copied FFmpeg or small statically incorporated
members. Preserve scanner name/version, database date, command, inputs, reports,
coverage and errors when one is run. An unavailable scanner, unmatched custom
component or zero reported CVEs is a coverage statement, not proof of no defects.
Manual source/advisory review remains necessary.

The retained [security review](../native/ffmpeg/security-review.json) records the
exact primary sources and limitations. Its upstream snapshot examines all 28
post-9.0.2 commits: 26 change absent code, one changes only the changelog, and the
selected common mux change requires a positive trailer-callback result excluded
by the sole PCM muxer and its checked return paths. This is a configuration-specific
assessment, not a vulnerability-free release claim. The narrowed Linux build
removes iconv code/imports; earlier candidate results are identified separately.

The historical OSV 2.6.0 run reports 1,124 Debian source-package matches: 70 binutils,
two GCC, 21 glibc and 1,031 kernel. All are retained. The individual binutils and
kernel triggers remain unreviewed; build-only tool roles and UAPI-only inclusion
are not individual call-graph proofs. The primary review dispositions distinguish
incorporated startup/header code from separately supplied OS implementations,
Power8-only functions, privileged loader prerequisites, fixed fopen mode strings
and unused function families. Host OS and build-tool maintenance remain required.
The new CI records fresh per-target SBOM reports with all findings, tool/input/report
hashes, online query time, raw errors and unrecognized components. A successful
coverage-recording step grants no runtime approval and suppresses no finding.

For this candidate, the official FFmpeg download page lists 9.0.2 as the stable
9.0 release, but the release branch includes subsequent fixes. Disposition those
exact fixes against the actual compiled units and enabled paths before shipping;
the stable-version label and valid signature are insufficient. Debian trackers
also contain open entries for selected build/OS packages. Evaluate their exact
code families, architectures and launch conditions rather than treating every
whole-source-package match as either exploitable or harmless.

Each update refreshes source/signatures, patches, configuration/toolchain,
component terms/SBOM/notices, source access, correspondence, independent rebuild/
replacement evidence where affected, compiler-free platform execution and relevant
containment/cancellation checks. Record compressed/uncompressed native size and
final image footprint against the previous qualified build with identical units
and scope. Old M118 research and stable-image checks cannot qualify a new runtime.
`footprint.json` reports archive sizes and exact summed regular-file/native-binary
bytes. There is no previous qualified native baseline. Issue #132 must measure
the integrated images and their complete closure; the clean test image mounts the
native package separately and is not an integrated image footprint measurement.

## Qualification record

Retain a per-target record identifying the exact tested artifacts and native OS,
source authentication, compile/link/install correspondence, full runtime closure,
clean installation/synthetic smoke, reproducibility, independent rebuild and
modified-library replacement. Record advisory/scanner coverage, notices/access
checks, footprint, all commands and failures. Linux x86-64, native Linux ARM64 and
Windows x86-64 are required; explicitly label cross-build and emulated evidence.
Update this document's candidate status only from retained results and obtain an
independent whole-change review on the final exact head before merging.
