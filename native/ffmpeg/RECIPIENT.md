# Recipient rebuild and replacement

These instructions use only the delivered source bundle, paired package files,
Python 3.14 with the supplied lockfile, Docker, and the pinned publicly obtainable
compiler-image inputs. No SobaFM application build or supplier work directory is
needed. All outputs remain unqualified until independently reviewed. Keep every
attempt and its failures.

Unpack the exact paired source archive into a fresh directory after checking its
filename, size and SHA256 against the reviewed package index. Its
`recipient-tools/scripts/qualify_native_runtime.py extract-sources` command also
checks the source archive and delivered tool inventory. Keep the original runtime
archive, source archive and `index.json` together in a separate package directory.
Use a Linux x86-64 build host for `linux-amd64` or `windows-amd64`, and an actual
Linux ARM64 build host for `linux-arm64`. Windows output is cross-compiled here;
its final installation and changed-library checks require an actual Windows x64
host. Emulation is not native qualification.

## Rebuild and compare the complete paired archives

The following is a POSIX shell sequence on the appropriate Linux build host.
Set `packages` to the directory containing the original three package files and
select the target. Start in the extracted source bundle root. Use a fresh attempt
path and container names; existing paths deliberately fail instead of being reset.
Docker must permit an ordinary unprivileged user to run containers.

```sh
set -eu
bundle=$PWD
packages=/absolute/path/to/original-paired-packages
target=linux-amd64
tools="$bundle/recipient-tools"
attempt="$bundle/recipient-$target"
test "$(id -u)" -ne 0
mkdir "$attempt"
mkdir "$attempt/work" "$attempt/out" "$attempt/toolchain"
uv sync --project "$tools" --locked --no-dev --no-install-project
python="$tools/.venv/bin/python"
export PYTHONPATH="$tools/src"
docker build --build-arg "NATIVE_TARGET=$target" \
    -t "sobafm-recipient-$target" "$tools/native/ffmpeg" \
    > "$attempt/compiler-image.log" 2>&1
image=$(docker image inspect "sobafm-recipient-$target" --format '{{.Id}}')
printf '%s\n' "$image" > "$attempt/compiler-image-id.txt"
set +e
docker run --name "sobafm-recipient-$target-build" --network none --read-only \
    --user "$(id -u):$(id -g)" --cap-drop ALL \
    --security-opt no-new-privileges --cpus 2 --memory 1g --pids-limit 128 \
    --tmpfs /tmp:rw,noexec,nosuid,size=67108864 \
    --mount "type=bind,source=$bundle/recipe,target=/recipe,readonly" \
    --mount "type=bind,source=$bundle/upstream,target=/inputs,readonly" \
    --mount "type=bind,source=$attempt/work,target=/work" \
    --mount "type=bind,source=$attempt/out,target=/out" \
    "$image" timeout --signal=TERM --kill-after=15s 600s \
    sh /recipe/build.sh "$target" > "$attempt/build.log" 2>&1
result=$?
set -e
printf '%s\n' "$result" > "$attempt/build-exit.txt"
docker inspect "sobafm-recipient-$target-build" --format '{{json .State}}' \
    > "$attempt/build-state.json"
test "$result" -eq 0
cp -R "$bundle/toolchain-sources" "$attempt/toolchain/sources"
cp -R "$bundle/authenticated-metadata" "$attempt/toolchain/apt"
cp -R "$bundle/notices/toolchain" "$attempt/toolchain/notices"
cp "$bundle/toolchain-packages.tsv" "$attempt/toolchain/packages.tsv"
cp "$bundle/covered-packages.tsv" "$attempt/toolchain/covered-packages.tsv"
"$python" "$tools/scripts/package_ffmpeg.py" --target "$target" \
    --builder-image-id "$image" --build "$attempt/work" \
    --toolchain "$attempt/toolchain" --inputs "$bundle/upstream" \
    --output "$attempt/packages" > "$attempt/packaging.log" 2>&1
"$python" "$tools/scripts/qualify_native_runtime.py" compare-packages \
    "$packages" "$attempt/packages" > "$attempt/archive-comparison.json"
```

The build authenticates selected installed compiler/linker inputs against signed
Debian metadata and retained package/source archives. It generates complete actual
compiler/link/install correspondence and checks the runtime closure. The frozen
Dockerfile and snapshot configuration obtain the compiler inputs; the same exact
immutable compiler image may be reused for a separate recipient attempt. Record
that choice. `/work/tmp` is fresh owned scratch for configure's executable probes;
`/tmp` remains `noexec`. The package comparison checks both complete runtime and
source archive bytes and their paired index, including installed binaries and
loader links. Retain discrepancies; do not relabel unequal outputs as reproduced.
The actual image ID and execution logs are separate provenance, not canonical
archive contents.

## Modify one shared library in separate scratch

Continue with the successful recipient variables above. This clones only that
recipient's successful work and changes the avutil version API and reported micro
version while preserving its shared-library ABI. The original executable remains
unchanged. The unchanged original source archive stays identified as such in the
replacement SBOM; the changed component gains its exact recipient patch relation.

```sh
cp -a "$attempt/work" "$attempt/replacement-work"
mkdir "$attempt/replacement-out"
set +e
docker run --name "sobafm-recipient-$target-replacement" --network none --read-only \
    --user "$(id -u):$(id -g)" --cap-drop ALL \
    --security-opt no-new-privileges --cpus 2 --memory 1g --pids-limit 128 \
    --tmpfs /tmp:rw,noexec,nosuid,size=67108864 \
    --mount "type=bind,source=$tools/native/ffmpeg/replace-library.sh,target=/checks/replace-library.sh,readonly" \
    --mount "type=bind,source=$attempt/replacement-work,target=/work" \
    --mount "type=bind,source=$attempt/replacement-out,target=/out" \
    "$image" timeout --signal=TERM --kill-after=15s 180s \
    sh /checks/replace-library.sh "$target" > "$attempt/replacement-build.log" 2>&1
result=$?
set -e
printf '%s\n' "$result" > "$attempt/replacement-exit.txt"
docker inspect "sobafm-recipient-$target-replacement" --format '{{json .State}}' \
    > "$attempt/replacement-state.json"
test "$result" -eq 0
case "$target" in
    windows-amd64) library=avutil-61.dll ;;
    linux-amd64|linux-arm64) library=libavutil.so.61.1.102 ;;
esac
"$python" "$tools/scripts/qualify_native_runtime.py" stage-replacement \
    "$attempt/packages/runtime" "$packages" \
    "$attempt/replacement-out/$library" \
    "$attempt/replacement-out/recipient-change.patch" \
    "$attempt/replacement-out/version.modified.c" "$attempt/changed-package" \
    > "$attempt/replacement-stage.json"
```

Keep the whole `changed-package` directory. It includes the complete original
sources plus exact changed source/patch and instructions in an ordinary ZIP,
updated manifest/SBOM/notices and an explicitly unqualified replacement identity.
Neither these steps nor an installer/index update application/operator approvals.

## Install and execute on the native machine without a compiler

Copy the original three paired package files, delivered `recipient-tools` and the
whole changed-package directory to an actual supported native target machine.
Linux targets need their OS-provided glibc/libm/loader; Windows needs its normal OS
DLLs. Use Python 3.14 and the delivered lockfile with
`uv sync --locked --no-dev --no-install-project` from `recipient-tools`. Set
`PYTHONPATH` to that directory's `src`. This installs only the existing locked
Python dependencies; it does not compile the native runtime or SobaFM application.
Use a fresh runtime path containing spaces and retain the JSON results.

On Linux, set `packages`, `tools`, `changed` and a fresh absolute `runtime` path:

```sh
set -eu
python="$tools/.venv/bin/python"
export PYTHONPATH="$tools/src"
"$python" "$tools/scripts/check_native_runtime.py" install "$packages" "$runtime"
manifest=$("$python" -c 'import json,sys; print(json.load(open(sys.argv[1]))["manifest_sha256"])' "$packages/index.json")
"$python" "$tools/scripts/check_native_runtime.py" smoke "$runtime" "$manifest" \
    --require-no-compiler > original-native.json
"$python" "$tools/scripts/qualify_native_runtime.py" check-replacement \
    "$changed/runtime" "$changed/replacement.json" > replacement-native.json
```

On Windows x64, create the locked environment before limiting PATH. Set absolute
`$packages`, `$tools`, `$changed` and fresh spaced `$runtime` paths in PowerShell:

```powershell
$ErrorActionPreference = 'Stop'
$nativePython = (Resolve-Path "$tools/.venv/Scripts/python.exe").Path
$env:PYTHONPATH = "$tools/src"
$env:PATH = "$env:SYSTEMROOT\System32;$env:SYSTEMROOT;$(Split-Path $nativePython)"
& $nativePython "$tools/scripts/check_native_runtime.py" install $packages $runtime
if ($LASTEXITCODE -ne 0) { throw 'Native installation failed' }
$manifest = (Get-Content -LiteralPath "$packages/index.json" -Raw | ConvertFrom-Json).manifest_sha256
& $nativePython "$tools/scripts/check_native_runtime.py" smoke $runtime $manifest --require-no-compiler > original-native.json
if ($LASTEXITCODE -ne 0) { throw 'Native smoke failed' }
& $nativePython "$tools/scripts/qualify_native_runtime.py" check-replacement "$changed/runtime" "$changed/replacement.json" > replacement-native.json
if ($LASTEXITCODE -ne 0) { throw 'Native replacement failed' }
```

The synthetic owned mono/stereo MP3 check verifies the selected PCM conversion.
The replacement check additionally proves the unchanged executable loads the new
micro version and that the modified shared library reports its source marker.
The original supply identity must reject the replacement. Compiler-free execution
and these checks establish supply behavior only. Production use still requires an
independently reviewed application/operator identity and the separate execution,
containment, cancellation and audio-accounting qualification. This procedure does
not grant source-publication, release or catalog-readiness approval.
