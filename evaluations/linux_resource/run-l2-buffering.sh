#!/usr/bin/env bash
# Keep one interruptible owner for the workload, Docker CLI children and cleanup.
set -eu
exec python3 -I "$(dirname -- "$0")/l2_run.py" "$@"
