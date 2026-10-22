# Proposed Google Gen AI distribution

This directory prepares [#228](https://github.com/slackysoba/sobafm/issues/228)
and [Proposed ADR-0010](../../docs/decisions/0010-maintain-a-pinned-bounded-genai-distribution.md).
It contains a reviewed downstream patch, exact upstream provenance, licenses,
Windows recipe and isolated installed-package mocks. The application still uses
its stock dependency and unchanged lockfile. No candidate artifact is published.

## Inputs and changes

`UPSTREAM.toml` records the exact locked Google Gen AI 2.26.0 source archive and
both original reviewed-patch and canonical distribution-patch hashes. The archive
is 705,690 bytes with SHA-256
`77bb207d535fc84934b42f95db35267df40fea7244b824e91daf2e7940358db3`.
Obtain only its recorded source URL and verify the hash before relying on it.
The recipe authenticates before parsing/extraction and refuses an existing output
directory. Failed and partial outputs remain in place.

The patch changes three SDK paths: `live_music.py`, `_bounded_receive.py` and
`json_shape_preflight.py`. It exports optional `live_music.BoundedReceiveOptions`
and `live_music.ShapeLimits`. Public WebSockets streaming/options underlie the
implementation; the SDK extension is downstream, not a stock supported Google API.
Omitted options retain the reviewed legacy receive path and default compression.
No application call site activates the extension.

After verifying the patched files against the previous reviewed source hashes,
the recipe adds prominent modification/addition comments. It separately changes
`version.py` and package metadata to `2.26.0+sobafm.1`, includes both licenses and
NOTICE, and adds the full recipe/patch materials to the patched source archive.
These packaging changes are allowlisted separately from the three semantic SDK
changes. The preserved original prototype patch remains untouched. In this
repository copy only, six single-space unified-diff context lines are encoded as
explicit empty delete/add pairs (`-` then `+`); hunk counts are unchanged. This
mechanical encoding changes 22,967 bytes/SHA-256 `9b9455d0...` into 22,979
bytes/SHA-256 `bddf0429...`; full digests are in the manifest. It avoids whitespace
formatter edits without a hook exception. Application uses command-local
`core.autocrlf=false`, and all three resulting SDK files must be byte-identical
to the reviewed prototype before the separately declared notice/version changes.
Canonical patch bytes are not claimed identical to the original patch bytes.

## Windows candidate recipe

Use existing signed CPython 3.14, uv 0.12.21 and Git. No compiler or global tool
installation is part of this recipe. Set the paths for the already obtained
archive and existing signed interpreter, then select a fresh **short writable
output root**. Setuptools creates a nested source tree during sdist construction;
a deep managed-worktree path can exceed Windows MAX_PATH. The caller can choose
another short owned root through `-Output`; no host long-path setting changes are
needed. These examples use `C:/dev/sobafm` as an existing writable parent:

```powershell
$sdkArchive = '.local/google_genai-2.26.0.tar.gz'
$signedPython = 'C:/Python314/python.exe'
$candidateOutput = 'C:/dev/sobafm/.local/g228-candidate-a1'
& ./third_party/google-genai/build.ps1 -Archive $sdkArchive `
    -Output $candidateOutput -Python $signedPython
```

The recipe creates its own build environment/cache, installs the hash-pinned
`build-requirements.txt`, and calls the upstream implied setuptools legacy
backend. The inspected upstream `pyproject.toml` declares setuptools, wheel,
twine, packaging and pkginfo without an explicit `build-backend`; `setup.cfg`
supplies package discovery and the archive has no `setup.py`. Preparation checks
those declarations before executing the backend. Twine is an upstream build
requirement; the recipe never invokes an upload command.

`candidate/dist/` contains the derived universal wheel and complete patched sdist.
`candidate/prepared.json` records strict before-notice equality and exact
notice-only prefixes/final hashes before backend execution.
`candidate/candidate.json` records actual hashes, Python/build-package versions,
source/material hashes and the changed-path inventory. `candidate/source/` and
`candidate/backend.log` preserve the complete build copy and output. The recipe
checks source/wheel/sdist correspondence, public helper inclusion, local version,
and license/NOTICE inclusion. Hashes identify this build; byte-reproducibility
across environments or subsequent rebuilds is not claimed.

The matching sdist includes complete SDK source and `sobafm-distribution/` with
the patch, provenance, licenses, recipe, pinned requirements and package checks.
Any later distribution must make that source available alongside the wheel.
The source archive, rather than three modified files alone, is the corresponding
source material.

## Installed candidate checks

Use a second fresh task-owned environment. Export the application's unchanged
locked runtime requirements while omitting the project and stock `google-genai`,
then install those exact requirements and the built wheel with uv. Keep the
application environment unchanged. For example, from the repository root:

```powershell
uv export --locked --no-dev --no-emit-project --no-emit-package google-genai `
    --output-file .local/candidate-runtime.txt
$checkEnv = 'C:/dev/sobafm/.local/g228-check-a1'
uv venv $checkEnv --python $signedPython
$checkPython = Join-Path $checkEnv 'Scripts/python.exe'
uv pip sync --python $checkPython `
    --require-hashes .local/candidate-runtime.txt
uv pip install --python $checkPython --no-deps `
    (Join-Path $candidateOutput 'candidate/dist/google_genai-2.26.0+sobafm.1-py3-none-any.whl')
& $checkPython -I -B `
    ./third_party/google-genai/check_candidate.py
uv pip check --python $checkPython
```

The unittest checks import the actual installed candidate and assert its version
and import paths. They use real SDK response conversion and owned fake I/O;
context-scoped unittest replacements are restored in the isolated test process.
They construct no SDK Client, default auth, socket or provider connection.
Coverage includes default omission, setup/later streaming, finite forwarding,
shape rejection before parsing, exact used-prefix handling, tiny decoded audio,
UTF-8 object/args and JSON doc sanitization, byte/fragment/empty excess, busy
borrow, cancellation, poisoned nonreuse and context-owned closure. Synthetic
sizes select no production profile or complete audio envelope.

Normal offline pytest separately checks archive path/link/hash refusal,
preservation of an existing destination, exact patch identity, the unchanged
active dependency, and a standard direct-requirement metadata specimen.

## Proposed adoption and update plan

On explicit adoption authorization, publish a distinctly identified wheel and
matching source archive under a dedicated non-`v*` SDK release tag in the existing
SobaFM repository. Only the maintainer creates tags. Record the actual release
URL and wheel SHA-256 in an ordinary direct requirement in
`[project].dependencies`, then regenerate/review `uv.lock`. This proposal makes
neither change and contains no active placeholder URL. A direct requirement
survives ordinary package metadata consumption; a uv-only source override would
leave other consumers requesting stock metadata.

SobaFM would own patch maintenance, source/artifact availability and security
updates. Each upstream update needs a new verified source, deliberate patch
review, package checks and fresh version/digests. Preserve upstream Apache-2.0
and copyright; SobaFM additions use MIT (`LICENSE.sobafm`). `NOTICE` distinguishes
the modifications and records the verified absence of upstream NOTICE.

The manifest distinguishes upstream advisory identity from derived-artifact
pedigree. A future integration must retain ordinary lock/transitive scanning and
dependency review and establish actual upstream-component coverage for the local
version/URL. An unmatched identity is not a clean result; upstream advisory
coverage does not audit this patch. This proposal changes no scanner policy,
exception, ignore, workflow or security claim.

The repository's `CANDIDATE.md` records actual isolated package verification,
retained artifact identities and an owned SobaFM wheel-metadata specimen. It is
excluded from the source build materials to avoid a hash cycle. The specimen
checks ordinary direct-requirement metadata without changing or executing the
application. All readiness, numeric model/native/transport reservations and
original cleanup/debt obligations remain separate.
