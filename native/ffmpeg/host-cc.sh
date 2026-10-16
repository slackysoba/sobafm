#!/bin/sh
# Generated resource tools run on the builder even for the Windows cross target.
set -eu
case "$(uname -m)" in
    x86_64) export SOBAFM_NATIVE_TARGET=linux-amd64 ;;
    aarch64) export SOBAFM_NATIVE_TARGET=linux-arm64 ;;
    *) exit 2 ;;
esac
exec /work/recipe/cc.sh "$@"
