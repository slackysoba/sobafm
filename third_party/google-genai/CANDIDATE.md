# Isolated candidate verification

The final unpublished proposal candidate is `google-genai 2.26.0+sobafm.1`, built
and verified on Windows x64 with signed CPython 3.14.7. Application dependencies,
lockfile and production SDK calls remain unchanged. No adoption or readiness
result follows from these component/package checks.

## Final package and corresponding source

| Artifact | Bytes | SHA-256 |
| --- | --- | --- |
| `google_genai-2.26.0+sobafm.1-py3-none-any.whl` | 1,174,597 | `95d2a9077f3c4730f846faa1e572fb52bf511333d4ab03bfd1f518c85af3bf03` |
| `google_genai-2.26.0+sobafm.1.tar.gz` | 748,342 | `701377c987998f4585f4f74e785386792d115cf9479af603eb1ad98ed436637f` |

The paired final build is retained at `C:/dev/sobafm/.local/g228-a5`. Its
`candidate/candidate.json`, `candidate/prepared.json`, `candidate/backend.log`,
`tools.json`, complete source tree and both distributions are preserved. This
sdist includes the complete patched SDK and all declared recipe/patch/license
materials. All wheel SDK files and all prepared SDK source files in the sdist
match the prepared source bytes; license/NOTICE and source-material inclusion
checks passed. The report itself is excluded from build inputs to avoid a hash
cycle. No artifact was published and no tag was created.

Build tools were `uv 0.12.21 (7af826859 2026-09-29 x86_64-pc-windows-msvc)`, `git version 2.53.0.windows.2`, PowerShell
`7.6.5` and signed CPython 3.14.7. The exact 26-package Windows
build closure is hash-pinned in `build-requirements.txt` and recorded in the build
receipt: setuptools 84.2.1, wheel 0.48.0, twine 7.0.0, packaging 26.3 and pkginfo
1.13.3 satisfy the inspected upstream declarations. No upload command was run.
A short caller-selected output root worked without host configuration changes.

## Reviewed code and packaging-only changes

The exact upstream archive was obtained once from the existing lockfile URL and
verified as 705,690 bytes/SHA-256
`77bb207d535fc84934b42f95db35267df40fea7244b824e91daf2e7940358db3`.
Its build declarations and complete license were inspected before backend execution.

The original reviewed patch remains preserved outside this worktree. Six
single-space context lines in the new distribution copy are explicit empty
minus/plus pairs, with unchanged hunk counts. Its 22,979 bytes/SHA-256
`bddf04295e8925cd3877f350d23b09c14d680f87f0cd94827c3787f1138d3a18`
reverse exactly to the 22,967-byte reviewed patch/SHA-256
`9b9455d08e7869ff53a8656ab8cbcc6ef89e6afd5cd7ca8ce99fbfe392ee852a`.
The regression checks that transformation. All 12 hooks and their copied approved
wrapper remain unchanged; no whitespace/scanner exclusion was added.

Command-local Git LF handling yields strict byte identity with all three
independently retained reviewed prototype files **before** license comments.
The manifest digests were generated from those authenticated bytes and checked
against the prior independent review. Final installed hashes differ only by the
exact notice prefixes recorded in `prepared.json`:

| SDK path | Reviewed bytes before notice | Final source/wheel/installed bytes |
| --- | --- | --- |
| `google/genai/live_music.py` | `ba1455d8aa94369508254ed25b23cc2369d5751dcb99b3426999d14ca906257d` | `61836c4952cd7c7d1d2e8591534865041ea5b3ef25c8400a117674cd82206469` |
| `google/genai/_bounded_receive.py` | `648bb14e995cec24a69012ee6ef051e54caeb67820e1774cf8acef7f44107399` | `18c849ee61c91bf062a8e1c597a4fa6994f6e6b8eabd963311571601822a08d2` |
| `google/genai/json_shape_preflight.py` | `295163e901f9e9aee4faf6f288ee89e935aeff2027ebf8afd5c6b6ecc1ad8747` | `9e8f3867e17205c23a85ef0b5f0ed2f1b4e94f905402a3cb163a1c4e05b17849` |

The installed `version.py` is exactly its authenticated upstream bytes plus the
recorded modification comment and single version replacement to `2.26.0+sobafm.1`.
Its original/final hashes are `10b2b4d791f1c90473452079a9f4c4db94348ec9eb1f0b255a95ee5ac8af29c2` and
`d6da25088a0957a46146652586d16d778baed1f913b3c29f829082460410ad8f`. Other declared packaging changes are the
project version/license metadata, MANIFEST source inclusion, MIT license/NOTICE
and the bundled recipe materials. No SDK logic was rewritten after review.

## Actual package and metadata checks

A second fresh short environment, `C:/dev/sobafm/.local/g228-check-a2`, installed
the final a5 wheel with the unchanged locked runtime dependency pins. The checker
bundled in the final source materials ran against its actual installed SDK:
**11 tests passed**; `uv pip check` found all 39 installed packages compatible.
Import-path, local version, license metadata, direct URL and installed file hashes
were verified, including real SDK response/base64 model conversion. The fakes
cover defaults, setup/later streaming, reusable storage and independent model
bytes, finite header fallback, pre-parser shape rejection, UTF-8 object/args and
JSON doc sanitization, byte/fragment/empty limits, busy borrow, cancellation,
source/model failure poison, no partial-read reconnect and context-owned closure.
No SDK Client, default authentication, provider, socket/TLS or native call was made.

An owned export of SobaFM source at `122584721203005a90b2b812d9012ecc38a5dfd7`
changed only the SDK dependency field in that isolated metadata specimen. Actual
uv_build 0.12.21 wheel construction and offline `--no-deps` installation preserved
this ordinary Requires-Dist entry:

```text
google-genai @ https://example.invalid/google_genai-2.26.0%2Bsobafm.1-py3-none-any.whl#sha256=95d2a9077f3c4730f846faa1e572fb52bf511333d4ab03bfd1f518c85af3bf03
```

The digest is the actual final SDK wheel digest. The specimen uses a deliberately
unresolvable owned example URL; no URL was fetched or activated in the application.
Its wheel SHA-256 is `bedde4b146c1bedac16c4cffb345440adc7089a85f962867bdbf3586ff6a3dd2`. Full inputs, wheel and
installed metadata proof remain at `C:/dev/sobafm/.local/g228-meta-a1`. This checks
metadata carriage, not publication or remote dependency download.

Normal offline pytest adds 14 owned archive/provenance regressions, including
path/link/device-name/hash refusal, existing-output preservation, exact canonical
patch identity and unchanged active dependency. Baseline locked setup/all 12 hooks
and full offline pytest passed before edits: 1,900 passed, one expected Windows
skip and two provider evaluations deselected. Final repository checks are recorded
on the PR and in the retained author results.

## Retained earlier results

Author evidence is under `.local/issue228-author-20261022-a1` in the managed
`genai-distribution228/sobafm` worktree. All earlier outputs remain:

- `package-a1` stopped before backend execution when inherited Git CRLF handling
  violated strict source-byte identity.
- `package-a2` stopped before backend execution on an owned 65-digit manifest
  transcription error; actual resulting shape-helper bytes equaled the preserved
  prototype. Digests are now generated and independently compared.
- `package-a3` passed source preparation, then setuptools hit Windows MAX_PATH in
  its nested sdist staging tree. The recipe now explicitly documents short roots;
  host settings were not changed.
- Successful a4 artifacts remain distinct from final a5 source materials. Early
  installed test runs exposed owned fixture errors: the real BaseModule
  constructor uses a positional client input, and real message conversion expects
  `serverContent`. Only the new test fixtures changed; the old prototype and SDK
  patch logic remain intact. The corrected final bundled 11-test run passed.

These results select no receive/shape/PCM/native profile or complete allocation,
cleanup-release, security coverage, readiness, latency/endurance or shipping pass.
Maintainer acceptance remains required before merging the Proposed decision PR.
