# Decoder supply recovery and fixed startup result

Issue [#7](https://github.com/slackysoba/sobafm/issues/7). The approved Debian binary
set is now acquired and installed in a local Linux amd64 image. The fixed no-input
startup check **rejected the supplemental-limit candidate**. No audio, Q3/Q4 or
native arm64 qualification follows. The [selected result](results/2026-10-27-decoder-recovery.yaml)
contains the actual 292-package inventory, component hashes and execution evidence.
Execution occurred on October 27 in America/Los_Angeles, October 28 UTC.

## Transport findings and recovery

The earlier [assembly attempts](results/2026-10-27-debian-build.yaml) encountered
actual HTTPS errors and transaction deadlines. Small direct transfers showed
that the official package URLs were usable with normal certificate verification:

| Probe | Bytes | Seconds | Result |
| --- | ---: | ---: | --- |
| Windows curl, FFmpeg | 2,001,944 | 10.598 | Exact signed SHA256 matched |
| WSL curl, FFmpeg | 2,001,944 | 5.571 | Exact signed SHA256 matched |
| WSL curl, libpciaccess0 | 51,920 | 1.883 | Exact signed SHA256 matched |
| WSL curl, libllvm19 | 25,978,316 | 9.281 | Exact signed SHA256 matched |

These measurements do not isolate a CDN, address or protocol as the cause of every
earlier failure. They justify retaining successful transfers instead of restarting
the entire 205-package transaction inside a disposable build step.

An initial retained-cache acquisition verified 122 files, then encountered a
specific curl HTTP/2 protocol error: the peer returned unexpected data where a
SETTINGS frame was expected. A per-command HTTP/1.1 continuation reused those
verified files and acquired the remainder from the same official HTTPS URLs.
Certificate verification and the approved hashes were unchanged. This changes
the transfer invocation, not host configuration or the application target.

One later transfer demonstrated recovery from a stalled body: `libsoxr0` retained
12,402 bytes when the low-throughput deadline fired. A range request returned
HTTP 206 with the remaining 68,606 bytes. The complete 81,008-byte file then matched
its approved SHA256. All **205 archives, 133,190,124 bytes**, ultimately verified.
No package version, source, signature policy or hash changed. The retained signed
security/updates records were still within their November 3 validity window.

## Ordinary package procedure

This is a finite development proof, not an application package manager or new
decoder distribution. Use the existing [complete proposal](results/2026-10-27-debian-package-proposal.yaml)
and preserve its source/image binding. The operator procedure is:

1. Validate each proposed binary filename, architecture, exact version, official
   HTTPS URI, expected size and SHA256 against the reviewed manifest. Only the
   recorded Debian `main` binary archives are eligible. Do not fetch source,
   development/debug-source packages or another archive as a substitute.
2. Keep verified archives in one project-owned directory outside transient build
   layers. Verify existing files before reuse. Download missing files to separate
   partial names with ordinary curl; rename only after the complete size and
   SHA256 match. Partial or mismatched data must never become installation input.
3. Use finite per-file bounds and at most three concurrent transfers. This run
   used 10-second connection, 180-second request, and 30-second low-throughput
   bounds at 1,024 bytes/second; at most two attempts per missing file and a
   900-second invocation bound. Resume retained bytes with HTTP range support.
   Reject unsupported resume, integrity failures and nonretryable responses.
4. Supply exactly the 205 verified `.deb` files and small reviewed plan files to
   the pinned base through an allowlisted, read-only build-context mount. Run the
   installation step with **network none**. Recheck the base's 87-package inventory,
   archive keyring and every archive hash before invoking APT.
5. Have ordinary APT simulate installation of all local archive paths with
   `--no-download --no-install-recommends --no-remove`. Require exact equality
   between the proposed transaction and the approved 205 additions, with no base
   upgrade, removal or extra dependency. Then install those same local paths,
   still without network or downloading. No fresh index resolution was needed.
6. Compare the actual installed inventory against the 87 base packages plus the
   205 additions. Record package/source identities, executable/shared-library
   hashes and notices. Keep the actual binary binding for independent review
   before any decoder invocation. Preserve the verified host archive cache.

APT simulation had a 60-second bound and installation a 240-second bound. The
outer build/observation budget was 600 seconds, with at most 8 MB captured output
inside the 10 MB selected-evidence allocation. The incremental development-storage
allocation remained 2 GB, with existing project budget/free-space stops. Build
resources do not establish runtime memory fit. A client exit alone cannot release
an uncertain daemon operation; actual completion evidence is required.

Normal package maintainer scripts ran during the approved offline build. No
FFmpeg, ffprobe, media or provider command was invoked by the build procedure.
Restricted source filenames are rejected before opening/hashing candidate files.
No source archive or restricted file was acquired, read or reconstructed.

## Actual image and startup result

The build completed successfully in **86.172 seconds**, with no active build debt.
Its immutable local image is:

```text
sha256:f214f61096a8823a587859e4ce87616f25a63f77b37e659c79ef8757a6cef76c
```

The image is Linux amd64, final user `1000:1000`, logical size 885,884,364 bytes.
The actual inventory exactly matched 292 packages. The retained binding records
906 executable/shared-library paths and all 292 copyright notices. FFmpeg is
root-owned mode 0755 with no file capability, and its ELF SHA256 is:

```text
e8a8d46f5225f3062cec7c07fb145d58ae73c603cb740dcd5bad34bfb54e455a
```

The image is private evaluation material, not a published release. Its inherited
upstream OCI license label describes the base and is not a combined-license claim.
The recorded narrow no-input assessment does not qualify untrusted audio or
redistribution. Exact notices remain in the image.

The reviewed [startup harness](decoder_startup.py) ran at clean source
`9748b281794e9be2c3883261308a2471f775d4e0`. One operator setup attempt stopped before
container start because Docker omitted the optional `Tmpfs` inspection key.
The original receipt was preserved; the owned container was observed created
with PID zero, removed normally and verified absent. The reviewed correction
used optional-map indexing and a unique attempt name/output. No decoder or limit
changed in that correction.

The corrected attempt retained the original settings: nonroot, default seccomp,
no capabilities, no-new-privileges, private cgroups, read-only root, no network,
two CPUs, 64 tasks, group memory 90,177,536 bytes and no swap. The parent verified
the child's actual hard/soft DATA 12,000,000 and STACK 524,288 limits before exec.
The only command was `/usr/bin/ffmpeg -nostdin -version`, with no input.

Exec succeeded, but the loader exited **127** with:

```text
/usr/bin/ffmpeg: error while loading shared libraries: libm.so.6: failed to map segment from shared object
```

The result is `startup_rejected`, not a setup failure. The leader was reaped,
no descendant remained, the original cleanup deadline was met, and internal and
external cleanup debt were zero. The container was observed stopped with PID zero,
removed normally and verified absent. It was not reported OOM-killed. No limit
tuning or audio run followed.

This result rejects this exact stock-image/supplemental-limit configuration. It
does not establish that the overall group budget is infeasible, isolate which
limit caused the mapping failure, or qualify aggregate PCM capacity. Q3/Q4 and
native arm64 runtime evidence remain outstanding. The next bounded alternative
assessment belongs to issue #7; the numeric guarantees and source restrictions
remain unchanged.

## Retention and checks

The verified 133,190,124-byte archive set, one result image and compact receipts
remain available for reuse. Build cache/image totals share layers and must not be
added as physical growth. At the post-build checkpoint, shared Docker image totals
changed from 9.939 GB to 10.57 GB and build-cache totals from 5.553 GB to 6.314 GB;
the build's observed host free-space decline was about 952 MB. These are different
measurements, not additive project usage. No global cache or Docker pruning occurred.

The existing offline startup regressions still cover bounds, retained processes
and late cleanup. Required locked synchronization, all-file checks and the current
offline suite apply before push. Native observations above are tied to their
recorded execution source/image; later documentation integration does not silently
rebind them to a new source or to arm64.
