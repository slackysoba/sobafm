#!/bin/sh
set -eu
case "$SOBAFM_NATIVE_TARGET" in
    windows-amd64) compiler=x86_64-w64-mingw32-gcc-win32 ;;
    linux-amd64|linux-arm64) compiler=gcc ;;
    *) exit 2 ;;
esac
# Remove FFmpeg's user-header-only mode before requesting complete dependencies.
# GCC can retain -MMD's omission even when -MD is appended to the driver command.
for argument in "$@"; do
    if [ "$argument" = -c ]; then
        for dependency_argument in "$@"; do
            shift
            if [ "$dependency_argument" != -MMD ]; then
                set -- "$@" "$dependency_argument"
            fi
        done
        exec "$compiler" "$@" -MD
    fi
done
exec "$compiler" "$@"
