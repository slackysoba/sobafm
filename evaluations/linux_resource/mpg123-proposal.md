# Pinned mpg123 metadata proposal

Issue [#7](https://github.com/slackysoba/sobafm/issues/7). Status: **authenticated
metadata complete; acquisition, installed binding and native execution pending**.
The [complete proposal and selected operation evidence](results/2026-10-27-mpg123-proposal.yaml)
record all 27 proposed additions and the unchanged 87-package base. This follows
the [fixed FFmpeg startup rejection](decoder-supply.md); it changes no numeric
limit, PCM requirement or supported-input requirement.

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

## Next bounded gate

Review the complete proposal and its source/license/security impact before any
binary acquisition. A later concrete acquisition/install procedure must preserve
every exact hash/version and the unchanged base, then bind actual installed files
and notices before a separately reviewed native check. Stop on an unavailable
version or an unexpected transaction rather than substituting another package.

No mpg123 startup or useful workload has run. Any MP3 result would remain partial:
FLAC, aggregate PCM coverage, Q4 cleanup, native arm64 and real source/Discord
acceptance remain outstanding. The existing product guarantees still apply.
