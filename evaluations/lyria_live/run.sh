#!/usr/bin/env bash
# Invoke with the outer GNU timeout in README.md. Never source this file.
set -euo pipefail

if [ "$#" -eq 0 ]; then
    echo '{"mode":"dry_run","sessions":1,"retries":0,"host_hard_seconds":75}'
    exit 0
fi
if [ "$#" -ne 3 ] || [ "$1" != --send-to-lyria ] || [ "$2" != --confirm-free-tier ]; then
    echo '{"result":"failed","reason":"explicit_opt_in_required"}'
    exit 2
fi
key_file=$3
case "$key_file" in /*) ;; *) exit 2 ;; esac
[ -f "$key_file" ] || exit 2
script_dir=$(cd -- "$(dirname -- "$0")" && pwd)
image=sha256:bd7d1aee495e612a8f7a168b767ce43eb996466d75e743e0f7997807fbb7fe23
source=01e2226adeeb49f676b525b9e903385f4960a1fd
label=org.sobafm.lyria-first
run_id=$(cat /proc/sys/kernel/random/uuid)
name=sobafm-lyria-first-$run_id
record_dir=$(mktemp -d "${TMPDIR:-/tmp}/sobafm-lyria.XXXXXX")
# Record identity BEFORE create: even a lost create response remains owned/recoverable.
printf '%s\n' "$name" "$label=$run_id" "$image" > "$record_dir/recovery.txt"
chmod 700 "$record_dir"
reason=setup
client_exit=unknown
clean=false
cleanup_commands_ok=true
create_pending=false

docker_bounded() {
    timeout --signal=TERM --kill-after=1s 2s docker "$@" 2>/dev/null
}

cleanup() {
    trap - EXIT
    trap '' INT TERM
    set +e
    # Resolve the exact name through a successful list, not a failed inspect.
    found=$(docker_bounded container ls -a --filter "name=^/$name$" --format '{{.Names}}')
    if [ "$?" -eq 0 ] && [ -z "$found" ]; then
        # A timed-out create might still be executing in the daemon.
        [ "$create_pending" = false ] && clean=true
    elif [ "$found" = "$name" ]; then
        owner=$(docker_bounded inspect --format "{{index .Config.Labels \"$label\"}}" "$name")
        if [ "$?" -eq 0 ] && [ "$owner" = "$run_id" ]; then
            # stop is idempotent for an exited container; never force-remove.
            docker_bounded stop --time 0 "$name" >/dev/null || cleanup_commands_ok=false
            docker_bounded rm "$name" >/dev/null || cleanup_commands_ok=false
            found=$(docker_bounded container ls -a --filter "name=^/$name$" --format '{{.Names}}')
            if [ "$?" -eq 0 ] && [ -z "$found" ]; then clean=true; fi
        fi
    fi
    if [ "$reason" = complete ]; then
        if [ "$cleanup_commands_ok" = false ]; then reason=cleanup_command; fi
        if [ "$clean" = false ]; then reason=cleanup_unconfirmed; fi
    fi
    verdict=failed
    if [ "$reason" = complete ] && [ "$client_exit" = 0 ] && [ "$clean" = true ] && \
        [ "$cleanup_commands_ok" = true ]; then
        verdict=pass
    fi
    printf '{"wrapper":"%s","reason":"%s","client_exit":"%s","cleanup_verified":%s,"cleanup_commands_ok":%s,"run":"%s"}\n' \
        "$verdict" "$reason" "$client_exit" "$clean" "$cleanup_commands_ok" "$run_id"
    [ "$verdict" = pass ] && exit 0
    exit 1
}
trap cleanup EXIT
trap 'reason=interrupted; exit 1' INT TERM

identity=$(docker_bounded image inspect --format '{{.Id}} {{.Os}} {{.Architecture}}' "$image")
[ "$identity" = "$image linux amd64" ] || exit 1
harness=$(sha256sum "$script_dir/probe.py" | cut -d ' ' -f 1)
printf '{"image":"%s","source":"%s","harness_sha256":"%s","run":"%s"}\n' \
    "$image" "$source" "$harness" "$run_id"
create_pending=true
docker_bounded create --name "$name" --label "$label=$run_id" \
    --pull never --restart no --init --user 1000:1000 --read-only --cap-drop ALL \
    --security-opt no-new-privileges --ulimit core=0:0 \
    --memory 90177536 --memory-swap 90177536 --pids-limit 64 --cpus 2 \
    --log-driver local --log-opt max-size=64k --log-opt max-file=1 --log-opt compress=false \
    --mount "type=bind,src=$script_dir/probe.py,dst=/eval/probe.py,readonly" \
    --mount "type=bind,src=$key_file,dst=/run/lyria-key,readonly" \
    --entrypoint /app/.venv/bin/python "$image" -I /eval/probe.py --send-to-lyria --confirm-free-tier >/dev/null
create_pending=false
reason=start
docker_bounded start "$name" >/dev/null
reason=client_wait
client_exit=$(timeout --signal=TERM --kill-after=1s 40s docker wait "$name" 2>/dev/null)
case "$client_exit" in 0|1|2|124|137|143) ;; *) client_exit=unknown; exit 1 ;; esac
state=$(docker_bounded inspect --format '{{.State.Running}} {{.State.OOMKilled}}' "$name")
[ "$state" = 'false false' ] || exit 1
reason=log_retrieval
# The sole process prints fixed-schema bounded records; no other entrypoint runs.
# Keep stderr discarded: it can contain raw startup/provider tracebacks.
docker_bounded logs "$name" > "$record_dir/result.jsonl"
reason=log_size
[ "$(wc -c < "$record_dir/result.jsonl")" -le 65536 ] || exit 1
reason=evaluator_output_missing
grep -Eq '"result"[[:space:]]*:[[:space:]]*"(pass|failed)"' "$record_dir/result.jsonl" || exit 1
reason=client_result
grep -Eq '"result"[[:space:]]*:[[:space:]]*"pass"' "$record_dir/result.jsonl" || {
    cat "$record_dir/result.jsonl"
    exit 1
}
cat "$record_dir/result.jsonl"
[ "$client_exit" = 0 ] || exit 1
reason=complete
