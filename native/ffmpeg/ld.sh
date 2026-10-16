#!/bin/sh
set -eu
case "$SOBAFM_NATIVE_TARGET" in
    windows-amd64) compiler=x86_64-w64-mingw32-gcc-win32 ;;
    linux-amd64|linux-arm64) compiler=gcc ;;
    *) exit 2 ;;
esac
output=
previous=
for argument in "$@"; do
    if [ "$previous" = -o ]; then output=$argument; fi
    previous=$argument
done
test -n "$output"
name=$(basename "$output")
case "$output" in
    /work/tmp/ffconf.*/test*) records=/work/evidence/configure-links ;;
    *) records=/work/evidence/links ;;
esac
mkdir -p "$records"
printf '%s\n' "$@" > "$records/$name.arguments"
# Actual driver trace + link map retain startup objects and selected archive
# members; a separate dry-run command cannot establish that correspondence.
exec "$compiler" "$@" -v "-Wl,-Map=$records/$name.map,--cref,-t"
