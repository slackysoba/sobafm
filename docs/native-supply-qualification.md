# Native supply qualification — 19 October 2026

This is the [#174](https://github.com/slackysoba/sobafm/issues/174) supply handoff
for the minimal shared FFmpeg 9.0.2 recipe. The exact observed candidate pairs
are in [native-supply-identities.json](native-supply-identities.json). They are
verified supply candidates, with catalog and production approval still gated.
The application trust registry remains empty. The [supply contract](native-runtime.md)
continues to govern source access, replacement, maintenance and updates.

## Artifact and native execution scope

| Target | Runtime archive bytes | Source archive bytes | Files / loader links | Selected dependency notices |
| --- | ---: | ---: | --- | ---: |
| Linux x86-64 | 2,135,616 | 341,785,680 | 62 / 10 | 775 |
| Native Linux ARM64 | 2,093,792 | 341,700,708 | 68 / 10 | 782 |
| Windows x86-64 | 3,098,864 | 353,024,756 | 84 / 0 | 858 |

The Linux observations bind artifacts from source `755a1f89` and
[native CI 38861442962](https://github.com/slackysoba/sobafm/actions/runs/38861442962).
Windows content binds the actual `f4c163db` artifact from
[native CI 39236687203](https://github.com/slackysoba/sobafm/actions/runs/39236687203).
That run passed all three supplier/recipient routes and native Windows execution.
The Linux runners are native `ubuntu-24.04` x86-64 and `ubuntu-24.04-arm` AArch64.
Windows compilation is a Linux cross-build; compiler-free installation and both
original/replacement execution run separately on native `windows-2025`.
QEMU application-image checks are separate emulation evidence.

Independent package review binds complete runtime inventories, all six installed
native identities and their compiler/link/member correspondence, closure, selected
notices, component ownership, full SPDX and exact runtime/source pairing. Windows
also binds ASLR/NX and nonempty relocation records to actual installed hashes.
Its saved native original/replacement receipts and advisory projection match the
current manifest, replacement manifest and SBOM digests exactly. Their historical
execution dates are preserved; they are not relabeled as a new local execution.

The four implementation patches and all 47 delivered Git bindings remained
identical through the current-main refresh. These include the recipe, source and
toolchain specifications, package/verifier tools and locked recipient context.
Final-head checks and independent review remain recorded on
[PR180](https://github.com/slackysoba/sobafm/pull/180). Reuse applies to the exact
observed pairs above; it does not assign their identities to every later upload.

## Corresponding source and replacement assurance

The actual fail-fast CI runs the delivered source-only sequence in
`native/ffmpeg/qualify-ci.sh`: verify the paired source digest and delivered-tool
inventory, use a separate locked recipient environment and fresh build paths,
rebuild from delivered recipe/materials, compare every native binary/link, then
reconstruct packaging from delivered inputs and compare **both complete runtime
and source archives**. This is actual recipient execution and byte comparison,
supported by independent source-to-object/notice review.

The shared-library test makes a real ABI-compatible avutil version change,
rebuilds and strips that library, retains the unchanged executable and runs native
synthetic mono/stereo decoding with the changed library loaded. The original
manifest must reject the modified runtime. The reviewed staging constructor
packages the complete original sources alongside the exact changed source,
patch, base record and instructions. Selected replacement witnesses and native
receipts bind that construction to each tested candidate.

Direct inspections additionally covered the ARM 444-file material inventory and
the AMD 427-file source inventory. The AMD reader retains nineteen missing
independent raw-oracle observations and false authentication flags. The full
Windows source and replacement-source ZIP interiors were not independently
rescanned. Those direct-observation limits remain intact; they are not source
authentication failures or a replacement for the executed complete-archive
reproduction. The [authentication disposition](https://github.com/slackysoba/sobafm/issues/174#issuecomment-6265831730)
records reviewed producer signature/index checks, actual fail-fast CI, exact
source/correspondence proof and scoped independent source-byte reuse. It does not
claim an independently observed AMD binary-package chain or authenticate the
general compiler census. No second crypto or source-interior replay is required
merely to change the historical flags.

Complete notices preserve IJG, ISC/MIT/Zlib, glibc startup/header and UAPI terms,
GCC 3.1 and distinct ARM soft-fp 2.0 exceptions, and the selected mixed MinGW terms
and custom stdarg exception. Each applicable custom SPDX license resolves to its
complete retained text. The component policy is scoped to actual selected inputs
and installed ownership; the native exception does not broaden Python policy.

## Advisory, footprint and distribution limits

The 19 October primary-source refresh found no new applicable blocking risk.
The official FFmpeg 9.0 release branch still has the same 28 post-tag commits:
26 touch absent inputs, one is changelog-only, and one selected common-mux change
requires a positive trailer-callback condition excluded by this sole PCM route.
Iconv remains disabled. Selected support dispositions retain actual architecture,
implementation, fixed fopen-mode and loader launch-precondition limits.

OSV 2.6.0 source scans retain 1,124 Linux/ARM and 1,126 Windows matches, with scanner
exit 1. The complete projections match the package SBOMs; coverage success does
not approve findings. Seventy binutils and 1,031 kernel individual triggers remain
unreviewed, along with FFmpeg/custom-component scanner gaps. The current binutils
tracker lists 69 rows; historical CVE-2026-19582 remains unresolved despite its
absence from that list. There is no immutable complete vulnerability-database
snapshot or native call-graph proof. Binutils build-only tools, selected UAPI
contributions and separately maintained OS libc/libm/loader/kernel have distinct
roles; no blanket unaffected or security-clean conclusion follows.

The existing release maintainer owns at-least-monthly and upstream-notice review,
applicable-fix triage within two working days, OS/build-tool maintenance and
suspension for an applicable serious unmitigated defect. This candidate assessment
grants no exception to that policy. Recheck the actual shipping pin and closure
when promotion is proposed.

`footprint.json` records compressed archives and exact summed native-binary and
runtime regular-file bytes using explicit units. Package checks validate those
records. There is no previous qualified native baseline for a size delta. The
table above is archive size, not native audio memory, process RSS or an integrated
image comparison.

Under the [recorded sequencing](https://github.com/slackysoba/sobafm/issues/174#issuecomment-6198800938),
Issue #174 delivers verified paired packages and the access/retention contract without
publishing a release. #132 must publish exact versioned sources/notices before
the matching public runtime/image, check unauthenticated access from each surface,
retain assets while binaries remain obtainable, bind image manifests and measure
integrated closure/footprint. Temporary CI retention is not that durable promise.
Issues #175/#125 retain pre-input containment, audio accounting, completion/cleanup and
integrated supported-installation gates. Catalog remains unavailable until all
applicable prerequisites are qualified; this handoff approves no trust identity,
catalog playback, release tag or production publication.

Earlier build, fixture and transfer failures remain distinct preserved records.
In particular, the Windows full transfer hit its original 900-second bound;
the separately reviewed Range recovery passed the complete server ZIP digest
and final original-prefix preservation. Later successes do not relabel failures.
