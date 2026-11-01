#!/usr/bin/env bash
# Manual workflow prerequisite setup only; never installs application dependencies.
set -euo pipefail

readonly FREE_FLOOR=25000000000
readonly SETUP_LIMIT=536870912
# Planning charge for the action's unattributable runner-temp archive/extraction.
# This is not a measured bound; no runner-temp files are read or cleaned.
readonly BOOTSTRAP_TEMP_RESERVE=134217728

fail() {
  printf '{"schema":"sobafm-arm64-preflight-setup-1","classification":"unqualified","issue":"%s","native_runtime_qualification":"not_run","cleanup_status":"not_qualified_by_setup"}\n' "$1"
  exit 1
}

available_bytes() {
  local result
  result=$(timeout --signal=TERM --kill-after=1s 3s df -B1 --output=avail -- "$1" 2>/dev/null) || return 1
  result=${result##*$'\n'}
  result=${result// /}
  [[ "$result" =~ ^[1-9][0-9]{0,14}$ ]] || return 1
  printf '%s' "$result"
}

setup_bytes() {
  local result
  result=$(timeout --signal=TERM --kill-after=1s 3s du -s -B1 -- "$1" 2>/dev/null) || return 1
  result=${result%%$'\t'*}
  [[ "$result" =~ ^[0-9]{1,15}$ ]] || return 1
  printf '%s' "$result"
}

check_space() {
  local location=$1 required=$2 available
  available=$(available_bytes "$location") || fail storage_unobservable
  (( available >= required )) || fail free_space_below_reservation
}

check_setup() {
  local size uv_size=0 accounted uv_directory="$PREFLIGHT_TOOL_CACHE/uv/0.13.6"
  [[ -d "$PREFLIGHT_ROOT" && ! -L "$PREFLIGHT_ROOT" ]] || fail setup_directory_unavailable
  size=$(setup_bytes "$PREFLIGHT_ROOT") || fail setup_storage_unobservable
  [[ ! -L "$uv_directory" ]] || fail uv_storage_unobservable
  if [[ -e "$uv_directory" ]]; then
    uv_size=$(setup_bytes "$uv_directory") || fail uv_storage_unobservable
  fi
  accounted=$((size + uv_size + BOOTSTRAP_TEMP_RESERVE))
  printf '{"schema":"sobafm-arm64-preflight-setup-1","setup_bytes":%s,"uv_tool_cache_bytes":%s,"bootstrap_temp_reservation_bytes":%s,"bootstrap_temp_usage":"unmeasured","accounted_bytes":%s,"setup_limit_bytes":%s}\n' \
    "$size" "$uv_size" "$BOOTSTRAP_TEMP_RESERVE" "$accounted" "$SETUP_LIMIT"
  (( accounted <= SETUP_LIMIT )) || fail setup_storage_limit_exceeded
  # Admission checkpoints, not a hard peak quota on the upstream setup action.
  check_space "$PREFLIGHT_ROOT" "$((FREE_FLOOR + SETUP_LIMIT - accounted))"
  check_space "$PREFLIGHT_TOOL_CACHE" "$((FREE_FLOOR + SETUP_LIMIT - accounted))"
  check_space "$PREFLIGHT_WORKSPACE" "$FREE_FLOOR"
  printf '{"schema":"sobafm-arm64-preflight-setup-1","classification":"storage_checkpoint_passed"}\n'
}

prepare() {
  [[ ! -e "$PREFLIGHT_ROOT" && ! -L "$PREFLIGHT_ROOT" ]] || fail setup_directory_already_exists
  [[ ! -e "$PREFLIGHT_TOOL_CACHE/uv/0.13.6" && ! -L "$PREFLIGHT_TOOL_CACHE/uv/0.13.6" ]] \
    || fail uv_tool_cache_already_exists
  check_space "${PREFLIGHT_ROOT%/*}" "$((FREE_FLOOR + SETUP_LIMIT))"
  check_space "$PREFLIGHT_TOOL_CACHE" "$((FREE_FLOOR + SETUP_LIMIT))"
  check_space "$PREFLIGHT_WORKSPACE" "$FREE_FLOOR"
  # Exclusive directory creation; no reuse, replacement, shared cache or cleanup.
  mkdir -m 700 -- "$PREFLIGHT_ROOT" || fail setup_directory_creation_failed
  mkdir -m 700 -- "$PREFLIGHT_ROOT/tmp" \
    "$PREFLIGHT_ROOT/cache" "$PREFLIGHT_ROOT/python-cache" "$PREFLIGHT_ROOT/python" \
    "$PREFLIGHT_ROOT/bin" "$PREFLIGHT_ROOT/tools" || fail setup_directory_creation_failed
  check_setup
}

install_python() {
  local uv_path="$PREFLIGHT_TOOL_CACHE/uv/0.13.6/aarch64/uv" status=0
  check_setup
  [[ "${SETUP_UV_PATH:-}" == "$uv_path" ]] || fail unexpected_uv_path
  [[ -x "$uv_path" && ! -L "$uv_path" ]] || fail pinned_uv_unavailable
  # No config discovery, user home, app sync, shell-profile change or output retention.
  # timeout is a finite outer stop; failure never asserts process/resource release.
  /usr/bin/env -i PATH=/usr/bin:/bin HOME=/nonexistent LANG=C LC_ALL=C \
    TMPDIR="$PREFLIGHT_ROOT/tmp" UV_CACHE_DIR="$PREFLIGHT_ROOT/cache" \
    UV_PYTHON_CACHE_DIR="$PREFLIGHT_ROOT/python-cache" \
    UV_PYTHON_INSTALL_DIR="$PREFLIGHT_ROOT/python" UV_PYTHON_BIN_DIR="$PREFLIGHT_ROOT/bin" \
    UV_HTTP_RETRIES=0 UV_HTTP_TIMEOUT=20 UV_NO_PROGRESS=1 \
    /usr/bin/timeout --signal=TERM --kill-after=3s 60s "$uv_path" \
    --no-config --quiet python install --no-bin cpython-3.14.10-linux-aarch64-gnu \
    >/dev/null 2>&1 || status=$?
  if (( status != 0 )); then
    printf '{"schema":"sobafm-arm64-preflight-setup-1","installer_exit":%s}\n' "$status"
    check_setup
    fail python_314_setup_failed
  fi
  check_setup
  local python_path="$PREFLIGHT_ROOT/python/cpython-3.14.10-linux-aarch64-gnu/bin/python3.14"
  [[ -x "$python_path" ]] || fail python_314_setup_incomplete
}

main() {
  [[ "${PREFLIGHT_ROOT:-}" =~ ^/[^[:cntrl:]]*/sobafm-arm64-preflight-[0-9]+-[0-9]+$ ]] \
    || fail invalid_setup_directory
  [[ "${PREFLIGHT_WORKSPACE:-}" == /* && -d "$PREFLIGHT_WORKSPACE" ]] \
    || fail invalid_workspace
  [[ "${PREFLIGHT_TOOL_CACHE:-}" == /* && -d "$PREFLIGHT_TOOL_CACHE" ]] \
    || fail invalid_tool_cache
  case "${1:-}" in
    prepare) prepare ;;
    check) check_setup ;;
    install) install_python ;;
    *) fail invalid_setup_phase ;;
  esac
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  main "$@"
fi
