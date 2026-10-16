#!/bin/sh
# Native supply tests use delivered source tools, separate work and unchanged compilers.
set -eu
target=$1
material=$2
image=$3
supplier="$PWD/native-dist"
recipient="$material/recipient"
uv run --frozen python scripts/qualify_native_runtime.py extract-sources "$supplier" "$recipient" > "$material/recipient-extraction.json"
tools="$recipient/recipient-tools"
recipient_python="$PWD/.venv/bin/python"
export PYTHONPATH="$tools/src"
mkdir "$material/recipient-work" "$material/recipient-out"
set +e
docker run --name "sobafm-native-$target-recipient" --network none --read-only --user "$(id -u):$(id -g)" \
    --cap-drop ALL --security-opt no-new-privileges --cpus 2 --memory 1g --pids-limit 128 \
    --tmpfs /tmp:rw,noexec,nosuid,size=67108864 \
    --mount "type=bind,source=$recipient/recipe,target=/recipe,readonly" \
    --mount "type=bind,source=$recipient/upstream,target=/inputs,readonly" \
    --mount "type=bind,source=$material/recipient-work,target=/work" \
    --mount "type=bind,source=$material/recipient-out,target=/out" \
    "$image" timeout --signal=TERM --kill-after=15s 600s sh /recipe/build.sh "$target" > "$material/recipient-build.log" 2>&1
result=$?
set -e
docker inspect "sobafm-native-$target-recipient" --format '{{json .State}}' > "$material/recipient-container-state.json"
test "$result" -eq 0
"$recipient_python" "$tools/scripts/qualify_native_runtime.py" compare "$supplier/runtime/manifest.json" \
    "$material/recipient-work/prefix/opt/sobafm-native" > "$material/recipient-payload.json"
# Reconstruct the packaging input layout entirely from delivered materials.
mkdir "$material/recipient-toolchain"
cp -R "$recipient/toolchain-sources" "$material/recipient-toolchain/sources"
cp -R "$recipient/authenticated-metadata" "$material/recipient-toolchain/apt"
cp -R "$recipient/notices/toolchain" "$material/recipient-toolchain/notices"
cp "$recipient/toolchain-packages.tsv" "$material/recipient-toolchain/packages.tsv"
cp "$recipient/covered-packages.tsv" "$material/recipient-toolchain/covered-packages.tsv"
"$recipient_python" "$tools/scripts/package_ffmpeg.py" --target "$target" --builder-image-id "$image" \
    --build "$material/recipient-work" --toolchain "$material/recipient-toolchain" \
    --inputs "$recipient/upstream" --output "$material/recipient-dist" > "$material/recipient-packaging.log" 2>&1
"$recipient_python" "$tools/scripts/qualify_native_runtime.py" compare-packages "$supplier" \
    "$material/recipient-dist" > "$material/recipient-packages.json"
cp -a "$material/recipient-work" "$material/replacement-work"
mkdir "$material/replacement-out"
set +e
docker run --name "sobafm-native-$target-replacement-build" --network none --read-only --user "$(id -u):$(id -g)" \
    --cap-drop ALL --security-opt no-new-privileges --cpus 2 --memory 1g --pids-limit 128 \
    --tmpfs /tmp:rw,noexec,nosuid,size=67108864 \
    --mount "type=bind,source=$tools/native/ffmpeg/replace-library.sh,target=/checks/replace-library.sh,readonly" \
    --mount "type=bind,source=$material/replacement-work,target=/work" \
    --mount "type=bind,source=$material/replacement-out,target=/out" \
    "$image" timeout --signal=TERM --kill-after=15s 180s sh /checks/replace-library.sh "$target" > "$material/replacement-build.log" 2>&1
result=$?
set -e
docker inspect "sobafm-native-$target-replacement-build" --format '{{json .State}}' > "$material/replacement-container-state.json"
test "$result" -eq 0
if [ "$target" = windows-amd64 ]; then library=avutil-61.dll; else library=libavutil.so.61.1.102; fi
"$recipient_python" "$tools/scripts/qualify_native_runtime.py" stage-replacement "$supplier/runtime" "$supplier" \
    "$material/replacement-out/$library" "$material/replacement-out/recipient-change.patch" \
    "$material/replacement-out/version.modified.c" "$PWD/native-replacement" > "$material/replacement-stage.json"
if [ "$target" != windows-amd64 ]; then
    set +e
    docker run --name "sobafm-native-$target-replacement-clean" --network none --read-only \
        --cap-drop ALL --security-opt no-new-privileges --cpus 2 --memory 256m --pids-limit 64 \
        --tmpfs /tmp:rw,noexec,nosuid,size=16777216 --tmpfs /data:rw,noexec,nosuid,size=1048576 \
        --env PYTHONPATH=/recipient/src:/recipient/scripts \
        --mount "type=bind,source=$tools,target=/recipient,readonly" \
        --mount "type=bind,source=$PWD/native-replacement,target=/changed native package,readonly" \
        --entrypoint python "sobafm-native-clean-$target" /recipient/scripts/qualify_native_runtime.py \
        check-replacement '/changed native package/runtime' '/changed native package/replacement.json' \
        > "$material/replacement-native.json" 2> "$material/replacement-native.stderr"
    result=$?
    set -e
    docker inspect "sobafm-native-$target-replacement-clean" --format '{{json .State}}' > "$material/replacement-native-state.json"
    test "$result" -eq 0
fi
