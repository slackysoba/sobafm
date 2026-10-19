#!/bin/sh
# Inputs are read-only; /work is fresh scratch, /out retains success AND failure.
set -eu
umask 022
export SOBAFM_NATIVE_TARGET=$1
case "$SOBAFM_NATIVE_TARGET" in
    linux-amd64) test "$(uname -m)" = x86_64; arch=x86_64; target_os=linux ;;
    linux-arm64) test "$(uname -m)" = aarch64; arch=aarch64; target_os=linux ;;
    windows-amd64) arch=x86_64; target_os=mingw32 ;;
    *) exit 2 ;;
esac
test ! -e /work/source
test ! -e /work/tmp
# Configure executes sanity probes. Keep /tmp noexec and use fresh owned scratch.
mkdir -m 700 /work/tmp
export TMPDIR=/work/tmp
mkdir -p /work/source /work/build /work/evidence /work/prefix
mkdir -p /work/recipe
cp /recipe/*.sh /recipe/*.py /recipe/source.json /recipe/Dockerfile /work/recipe/
chmod 755 /work/recipe/*.sh
finish() {
    result=$?
    trap - EXIT
    printf '%s\n' "$result" > /work/evidence/exit-code.txt
    tar -cf /out/build-evidence.tar -C /work .
    exit "$result"
}
trap finish EXIT
uname -a > /work/evidence/kernel.txt
id > /work/evidence/user.txt
gcc --version > /work/evidence/compiler.txt
ld --version > /work/evidence/linker.txt
cp /toolchain/packages.tsv /work/evidence/
cp /toolchain/covered-packages.tsv /work/evidence/
python3 /work/recipe/authenticate.py /toolchain /work/evidence > /work/evidence/source-authentication.log 2>&1
sha256sum /inputs/ffmpeg-9.0.2.tar.xz > /work/evidence/source.sha256
printf '%s  %s\n' 8c3850283eb25fa026482078a04051e0be17347b09ef81a0849bec15a96e002e /inputs/ffmpeg-9.0.2.tar.xz | sha256sum --check --strict
tar -xJf /inputs/ffmpeg-9.0.2.tar.xz --strip-components=1 -C /work/source
cd /work/build
set -- \
    --prefix=/opt/sobafm-native \
    --disable-all --disable-autodetect --disable-network --disable-iconv \
    --disable-gpl --disable-nonfree --disable-version3 \
    --disable-doc --disable-debug --disable-asm \
    --disable-static --enable-shared \
    --enable-avutil --enable-avcodec --enable-avformat \
    --enable-avfilter --enable-swresample --enable-ffmpeg \
    --enable-decoder=mp3float --enable-parser=mpegaudio \
    --enable-demuxer=mp3 --enable-encoder=pcm_s16le \
    --enable-muxer=pcm_s16le --enable-protocol=pipe \
    --enable-filter=aresample \
    "--arch=$arch" "--target-os=$target_os" \
    --cc=/work/recipe/cc.sh --ld=/work/recipe/ld.sh \
    --host-cc=/work/recipe/host-cc.sh \
    --extra-cflags=-ffile-prefix-map=/work=/build
if [ "$target_os" = linux ]; then
    # GNU make and the shell each interpret '$'. Response files preserve the
    # loader token literally; inspect the actual ELF afterwards as well.
    printf '%s\n' '-rpath=$ORIGIN/../lib' > /work/cli-linker.rsp
    printf '%s\n' '-rpath=$ORIGIN' > /work/so-linker.rsp
    set -- "$@" --extra-ldexeflags=-Wl,@/work/cli-linker.rsp --extra-ldsoflags=-Wl,@/work/so-linker.rsp
else
    set -- "$@" --enable-cross-compile --cross-prefix=x86_64-w64-mingw32- --disable-pthreads --enable-w32threads --extra-ldflags=-static-libgcc
fi
printf '%s\n' "$@" > /work/evidence/configure-arguments.txt
/work/source/configure "$@" > /work/evidence/configure.log 2>&1
make -j2 V=1 > /work/evidence/make.log 2>&1
make install DESTDIR=/work/prefix > /work/evidence/install.log 2>&1
sh /work/recipe/correspondence.sh > /work/evidence/correspondence.log 2>&1
python3 /work/recipe/inspect_runtime.py /work/prefix/opt/sobafm-native "$SOBAFM_NATIVE_TARGET" /work/evidence/runtime-closure.json > /work/evidence/runtime-inspection.log 2>&1
find /work/prefix -type f -exec sha256sum {} + | LC_ALL=C sort > /work/evidence/installed-files.sha256
find /work/build -name '*.d' -exec cp --parents {} /work/evidence/ \;
if [ "$target_os" = linux ]; then
    find /work/prefix -type f -name '*.so.*' -exec readelf -d {} + > /work/evidence/libraries.txt
    readelf -d /work/prefix/opt/sobafm-native/bin/ffmpeg > /work/evidence/executable.txt
else
    find /work/prefix -type f \( -name '*.dll' -o -name '*.exe' \) -exec x86_64-w64-mingw32-objdump -p {} + > /work/evidence/imports.txt
fi
