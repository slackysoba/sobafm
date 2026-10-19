#!/bin/sh
# Linux-hosted packaging orchestration; Windows execution is a separate native job.
set -eu
target=$1
case "$target" in
    linux-amd64|windows-amd64) test "$(uname -m)" = x86_64 ;;
    linux-arm64) test "$(uname -m)" = aarch64 ;;
    *) exit 2 ;;
esac
material="$PWD/.local/native-ci-$target"
mkdir -p "$PWD/.local"
mkdir "$material"
mkdir "$material/inputs" "$material/work" "$material/out" "$material/toolchain"
mkdir -m 700 "$material/gnupg"
pin=native/ffmpeg/source.json
archive_url=$(uv run --frozen python -c 'import json,sys; print(json.load(open(sys.argv[1]))["archive"])' "$pin")
key_url=$(uv run --frozen python -c 'import json,sys; print(json.load(open(sys.argv[1]))["key_url"])' "$pin")
fingerprint=$(uv run --frozen python -c 'import json,sys; print(json.load(open(sys.argv[1]))["signing_fingerprint"])' "$pin")
curl --fail --location --retry 3 --connect-timeout 15 --max-time 180 "$archive_url" -o "$material/inputs/ffmpeg-9.0.2.tar.xz"
curl --fail --location --retry 3 --connect-timeout 15 --max-time 60 "$archive_url.asc" -o "$material/inputs/ffmpeg-9.0.2.tar.xz.asc"
curl --fail --location --retry 3 --connect-timeout 15 --max-time 60 "$key_url" -o "$material/inputs/ffmpeg-devel.asc"
uv run --frozen python -c 'import hashlib,json,pathlib,sys; p=json.load(open(sys.argv[1])); d=pathlib.Path(sys.argv[2]); pairs=[("ffmpeg-9.0.2.tar.xz","archive_sha256"),("ffmpeg-9.0.2.tar.xz.asc","signature_sha256"),("ffmpeg-devel.asc","public_certificate_sha256")]; assert all(hashlib.sha256((d/n).read_bytes()).hexdigest()==p[k] for n,k in pairs)' "$pin" "$material/inputs"
gpg --no-options --homedir "$material/gnupg" --batch --dearmor --output "$material/inputs/release.gpg" "$material/inputs/ffmpeg-devel.asc"
gpgv --homedir "$material/gnupg" --status-fd 1 --keyring "$material/inputs/release.gpg" "$material/inputs/ffmpeg-9.0.2.tar.xz.asc" "$material/inputs/ffmpeg-9.0.2.tar.xz" > "$material/source-authentication.log" 2>&1
grep -F "[GNUPG:] VALIDSIG $fingerprint " "$material/source-authentication.log"
docker build --progress=plain --build-arg "NATIVE_TARGET=$target" -t "sobafm-native-ci-$target" native/ffmpeg > "$material/toolchain-build.log" 2>&1
image=$(docker image inspect "sobafm-native-ci-$target" --format '{{.Id}}')
printf '%s\n' "$image" > "$material/builder-id.txt"
name="sobafm-native-$target-build"
set +e
docker run --name "$name" --network none --read-only --user "$(id -u):$(id -g)" \
    --cap-drop ALL --security-opt no-new-privileges --cpus 2 --memory 1g --pids-limit 128 \
    --tmpfs /tmp:rw,noexec,nosuid,size=67108864 \
    --mount "type=bind,source=$PWD/native/ffmpeg,target=/recipe,readonly" \
    --mount "type=bind,source=$material/inputs/ffmpeg-9.0.2.tar.xz,target=/inputs/ffmpeg-9.0.2.tar.xz,readonly" \
    --mount "type=bind,source=$material/work,target=/work" \
    --mount "type=bind,source=$material/out,target=/out" \
    "$image" timeout --signal=TERM --kill-after=15s 600s sh /recipe/build.sh "$target" > "$material/container-output.log" 2>&1
result=$?
set -e
printf '%s\n' "$result" > "$material/container-exit.txt"
docker inspect "$name" --format '{{json .State}}' > "$material/container-state.json"
test "$result" -eq 0
material_name="sobafm-native-$target-materials"
docker create --name "$material_name" --network none --read-only --user 65532:65532 "$image" /bin/true
for directory in sources apt; do
    docker cp "$material_name:/toolchain/$directory" "$material/toolchain/$directory"
done
# Resolve notice links in Linux, before a Windows Docker client copies material.
# Preserve the export container and tar as evidence; no host symlink policy change.
docker run --name "$material_name-notices-export" --network none --read-only \
    --user "$(id -u):$(id -g)" --cap-drop ALL --security-opt no-new-privileges \
    --cpus 1 --memory 128m --pids-limit 32 \
    --mount "type=bind,source=$material/out,target=/out" \
    "$image" timeout --signal=TERM --kill-after=5s 60s sh -c \
    'umask 022; test ! -e /out/toolchain-notices.tar; tar --dereference --hard-dereference -cf /out/toolchain-notices.tar -C /toolchain notices' \
    > "$material/notice-export.log" 2>&1
tar -xf "$material/out/toolchain-notices.tar" -C "$material/toolchain"
for file in packages.tsv covered-packages.tsv; do
    docker cp "$material_name:/toolchain/$file" "$material/toolchain/$file"
done
uv run --frozen python scripts/package_ffmpeg.py --target "$target" --builder-image-id "$image" \
    --build "$material/work" --toolchain "$material/toolchain" --inputs "$material/inputs" --output native-dist
if [ "$target" != windows-amd64 ]; then
    installed="$PWD/installed native runtime"
    manifest=$(uv run --frozen python scripts/check_native_runtime.py install native-dist "$installed")
    docker build -t "sobafm-native-clean-$target" . > "$material/clean-image-build.log" 2>&1
    set +e
    docker run --name "sobafm-native-$target-clean" --network none --read-only \
        --cap-drop ALL --security-opt no-new-privileges --cpus 2 --memory 256m --pids-limit 64 \
        --tmpfs /tmp:rw,noexec,nosuid,size=16777216 \
        --tmpfs /data:rw,noexec,nosuid,size=1048576 \
        --mount "type=bind,source=$installed,target=/native package,readonly" \
        --mount "type=bind,source=$PWD/scripts/check_native_runtime.py,target=/checks/check_native_runtime.py,readonly" \
        --entrypoint python "sobafm-native-clean-$target" /checks/check_native_runtime.py \
        smoke '/native package' "$manifest" --require-no-compiler --application-check > "$material/clean-smoke.json" 2> "$material/clean-smoke.stderr"
    result=$?
    set -e
    docker inspect "sobafm-native-$target-clean" --format '{{json .State}}' > "$material/clean-container-state.json"
    test "$result" -eq 0
fi
sh native/ffmpeg/qualify-ci.sh "$target" "$material" "$image"
