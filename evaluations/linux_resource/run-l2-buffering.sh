#!/usr/bin/env bash
# After explicit target-run authorization only. No pulls, builds or service configuration.
set -euo pipefail
harness=${1:?reviewed harness commit}
source=${2:?source commit of approved image}
image=${3:?immutable local image ID}
kernel=${4:?approved native target kernel release}
[[ $# == 4 && $harness =~ ^[0-9a-f]{40}$ && $source =~ ^[0-9a-f]{40}$ ]]
[[ $image =~ ^sha256:[0-9a-f]{64}$ && $kernel =~ ^[a-zA-Z0-9._+-]{1,128}$ ]]

dir=evaluations/linux_resource
cd "$(git rev-parse --show-toplevel)"
git diff --quiet "$harness" -- "$dir/run-l2-buffering.sh"
[[ $(git rev-parse "$source^{commit}") == "$source" ]]
[[ $(docker image inspect --format '{{.Os}}/{{.Architecture}} {{.Id}}' "$image") == "linux/amd64 $image" ]]
[[ $(docker info --format '{{.OSType}}/{{.Architecture}} {{.KernelVersion}}') == "linux/x86_64 $kernel" ]]
lock=$(git show "$source:uv.lock" | sha256sum)
lock=${lock%% *}
# Bind partial output even if the client is killed before it can write its first row.
printf '{"case":"L2","harness":"%s","source":"%s","image":"%s","lock_sha256":"%s","kernel":"%s"}\n' \
    "$harness" "$source" "$image" "$lock" "$kernel"
tmp=$(mktemp -d)
peer=
client=
cleanup() {
    local rc=$? id
    trap - EXIT
    for id in "$client" "$peer"; do
        [[ -n $id ]] || continue
        if [[ $(docker inspect --format '{{.State.Running}}' "$id") == true ]]; then
            docker stop --time 1 "$id" >/dev/null || rc=1
        fi
        docker rm "$id" >/dev/null || rc=1
    done
    rm -f -- "$tmp/fixture.key" "$tmp/fixture.crt" "$tmp/l2_peer.py" "$tmp/l2_buffering.py"
    rmdir -- "$tmp" || rc=1
    exit "$rc"
}
trap cleanup EXIT
trap 'exit 124' TERM INT
for file in l2_peer.py l2_buffering.py; do
    git show "$harness:$dir/$file" >"$tmp/$file"
done
# One short-lived test identity. Only its public certificate reaches the measured client.
openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes -days 1 \
    -subj /CN=loopback -addext subjectAltName=IP:127.0.0.1 \
    -out "$tmp/fixture.crt" -keyout "$tmp/fixture.key" >/dev/null 2>&1
chmod 0444 "$tmp"/{fixture.key,fixture.crt,l2_peer.py,l2_buffering.py}
common=(--pull never --platform linux/amd64 --user 1000:1000 --read-only --init
        --cap-drop ALL --security-opt no-new-privileges --cgroupns private
        --pids-limit 64 --cpus 2 --ulimit core=0:0 --restart no
        --log-driver local --log-opt max-size=64k --log-opt max-file=1
        --mount "type=bind,src=$tmp/fixture.crt,dst=/fixture.crt,readonly"
        --entrypoint /app/.venv/bin/python)
peer=$(docker create "${common[@]}" --network none --memory 134217728 --memory-swap 134217728 \
    --mount "type=bind,src=$tmp/fixture.key,dst=/fixture.key,readonly" \
    --mount "type=bind,src=$tmp/l2_peer.py,dst=/l2_peer.py,readonly" \
    "$image" -I /l2_peer.py /fixture.crt /fixture.key)
docker start "$peer" >/dev/null
ready=false
for _ in {1..50}; do
    if [[ $(docker logs "$peer" 2>/dev/null) == 'ready 8765' ]]; then ready=true; break; fi
    sleep 0.1
done
[[ $ready == true ]] || { echo '{"L2":"failed","reason":"peer_start"}'; exit 1; }
client=$(docker create "${common[@]}" --network "container:$peer" \
    --memory 90177536 --memory-swap 90177536 \
    --mount "type=bind,src=$tmp/l2_buffering.py,dst=/l2_buffering.py,readonly" \
    "$image" -I /l2_buffering.py)
docker start "$client" >/dev/null
if ! status=$(timeout 90 docker wait "$client"); then
    echo '{"L2":"failed","reason":"wait_or_timeout","settlement":"unconfirmed"}'
    docker logs "$client" 2>/dev/null
    exit 1
fi
output=$(docker logs "$client" 2>/dev/null)
printf '%s\n' "$output"
oom=$(docker inspect --format '{{.State.OOMKilled}}' "$client")
printf '{"client_exit":%d,"oom_killed":%s}\n' "$status" "$oom"
[[ $status == 0 && $oom == false ]]
[[ ${output##*$'\n'} == '{"L2":"pass","sessions":10,"renewals":5}' ]]
