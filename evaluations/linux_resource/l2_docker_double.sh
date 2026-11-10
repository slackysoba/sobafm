#!/usr/bin/env bash
# Executable stand-in used only by test_l2_runner.py on a private PATH.
set -eu
state=${L2_DOUBLE:?}
mode=$(cat "$state/mode")
image=sha256:$(printf '%064d' 0)
peer=$(printf '%064d' 1)
client=$(printf '%064d' 2)
lookup() {
    local id
    for id in "$peer" "$client"; do
        [[ -f "$state/$id/name" ]] || continue
        if [[ $1 == "$id" || $1 == "$(cat "$state/$id/name")" ]]; then echo "$id"; return; fi
    done
    return 1
}
case "$1" in
image) [[ $mode != wrong-image ]] || image=no-match; echo "linux/amd64 $image" ;;
info) [[ $mode != wrong-target ]] || { echo 'linux/aarch64 test-kernel'; exit 0; }; echo 'linux/x86_64 test-kernel' ;;
create)
    case " $* " in *" --network none "*) id=$peer; role=peer;; *) id=$client; role=client;; esac
    if [[ $mode == ambiguous && $role == client ]]; then exit 1; fi
    name=$role; label=none
    while (( $# )); do
        case "$1" in --name) shift; name=$1;; --label) shift; label=${1#*=};; esac
        shift
    done
    mkdir "$state/$id"
    echo "$name" > "$state/$id/name"; echo "$label" > "$state/$id/label"
    echo false > "$state/$id/running"
    if [[ $mode == "lost-$role" ]]; then
        echo "$$" > "$state/blocked"
        exec sleep 20
    fi
    echo "$id" ;;
start) id=$(lookup "${!#}"); echo true > "$state/$id/running" ;;
wait)
    if [[ $mode == wait ]]; then
        echo "$$" > "$state/blocked"; exec sleep 20
    fi
    [[ $mode != wait-fails ]] || exit 1
    echo false > "$state/$client/running"
    [[ $mode == client-fails ]] && echo 1 || echo 0 ;;
logs)
    id=$(lookup "${!#}")
    if [[ $id == "$peer" ]]; then echo 'ready 8765'; else cat "$state/output"; fi ;;
ps)
    for id in "$peer" "$client"; do
        [[ -f "$state/$id/name" ]] || continue
        [[ " $* " != *"name=^/$(cat "$state/$id/name")$ "* ]] || echo "$id"
    done ;;
inspect)
    id=$(lookup "${!#}")
    case "$*" in
        *OOMKilled*) [[ $mode == oom ]] && echo true || echo false;;
        *Labels*) echo "$(cat "$state/$id/label") $(cat "$state/$id/running")";;
        *) cat "$state/$id/running";;
    esac ;;
stop) id=$(lookup "${!#}"); echo false > "$state/$id/running" ;;
rm)
    id=$(lookup "${!#}")
    [[ $mode != bad-rm || $id != "$client" ]] || exit 1
    rm -- "$state/$id/name" "$state/$id/label" "$state/$id/running"
    rmdir -- "$state/$id" ;;
*) exit 1 ;;
esac
