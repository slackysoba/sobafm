#!/bin/sh
# Run in a separate copy of a successful recipient build, never the supplier build.
set -eu
umask 022
export SOBAFM_NATIVE_TARGET=$1 TMPDIR=/work/tmp
test ! -e /out/recipient-change.patch
cp /work/source/libavutil/version.c /out/version.original.c
python3 -c 'from pathlib import Path; p=Path("/work/source/libavutil/version.c"); s=p.read_text(); old="return FFMPEG_VERSION;"; micro="return LIBAVUTIL_VERSION_INT;"; assert s.count(old)==s.count(micro)==1; p.write_text(s.replace(old,"return FFMPEG_VERSION \"-sobafm-recipient-test\";").replace(micro,"return LIBAVUTIL_VERSION_INT + 1;"))'
cp /work/source/libavutil/version.c /out/version.modified.c
set +e
diff -u --label a/libavutil/version.c --label b/libavutil/version.c /out/version.original.c /out/version.modified.c > /out/recipient-change.patch
result=$?
set -e
test "$result" -eq 1
case "$SOBAFM_NATIVE_TARGET" in
    linux-amd64|linux-arm64)
        built=libavutil/libavutil.so.61
        installed=libavutil.so.61.1.102
        strip_tool=strip
        ;;
    windows-amd64)
        built=libavutil/avutil-61.dll
        installed=avutil-61.dll
        strip_tool=x86_64-w64-mingw32-strip
        ;;
    *) exit 2 ;;
esac
cd /work/build
make -j2 V=1 "$built" > /out/replacement-make.log 2>&1
"$strip_tool" --strip-unneeded -o "/out/$installed" "$built"
sha256sum /out/version.original.c /out/version.modified.c /out/recipient-change.patch "/out/$installed" /work/build/libavutil/version.o > /out/replacement.sha256
