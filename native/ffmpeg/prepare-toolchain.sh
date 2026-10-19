#!/bin/sh
# Run only while constructing an isolated builder, never on an operator's host.
set -eu
target=$1
case "$target" in linux-amd64|linux-arm64|windows-amd64) ;; *) exit 2 ;; esac
mkdir -p /toolchain/sources /toolchain/debs /toolchain/notices /toolchain/apt
chown _apt /toolchain/sources /toolchain/debs
# This immutable snapshot supplies authenticated exact source/package metadata for
# the digest-pinned base. Historical expiry is expected; signature checks remain on.
rm /etc/apt/sources.list.d/debian.sources
cat > /etc/apt/sources.list.d/sobafm.sources <<'EOF'
Types: deb deb-src
URIs: https://snapshot.debian.org/archive/debian/20261007T000000Z/
Suites: trixie trixie-updates
Components: main
Check-Valid-Until: no
Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg

Types: deb deb-src
URIs: https://snapshot.debian.org/archive/debian-security/20261007T000000Z/
Suites: trixie-security
Components: main
Check-Valid-Until: no
Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg
EOF
apt-get update
if [ "$target" = windows-amd64 ]; then
    apt-get install --no-install-recommends -y gcc-mingw-w64-x86-64-win32
fi
dpkg-query -W -f='${binary:Package}\t${Version}\t${source:Package}\t${source:Version}\n' > /toolchain/packages.tsv
cp /etc/apt/sources.list.d/sobafm.sources /toolchain/apt/
cp /usr/share/keyrings/debian-archive-keyring.gpg /toolchain/apt/
cp /var/lib/apt/lists/*InRelease /toolchain/apt/
# Retain authenticated indices for independent binary/source hash correspondence.
find /var/lib/apt/lists -maxdepth 1 -type f \( -name '*Packages*' -o -name '*Sources*' \) -exec cp {} /toolchain/apt/ \;
packages='libc6 libc6-dev libgcc-14-dev libgcc-s1 libatomic1 gcc-14 binutils linux-libc-dev'
compiler_package=$(dpkg-query -S "$(readlink -f /usr/bin/gcc)" | sed 's/: .*//')
linker_package=$(dpkg-query -S "$(readlink -f /usr/bin/ld)" | sed 's/: .*//')
packages="$packages $compiler_package $linker_package"
if [ "$target" = windows-amd64 ]; then
    packages="$packages gcc-mingw-w64-x86-64-win32 gcc-mingw-w64-x86-64-win32-runtime gcc-mingw-w64-base mingw-w64-common mingw-w64-x86-64-dev binutils-mingw-w64-x86-64"
fi
for package in $packages; do
    dpkg-query -W -f='${binary:Package}\t${Version}\t${source:Package}\t${source:Version}\n' "$package" >> /toolchain/covered-packages.tsv
    binary_version=$(dpkg-query -W -f='${Version}' "$package")
    source_package=$(dpkg-query -W -f='${source:Package}' "$package")
    source_version=$(dpkg-query -W -f='${source:Version}' "$package")
    (cd /toolchain/debs && apt-get download "$package=$binary_version")
    (cd /toolchain/sources && apt-get source --download-only "$source_package=$source_version")
    apt-cache show "$package=$binary_version" >> /toolchain/apt/binary-metadata.txt
    apt-cache showsrc "$source_package" >> /toolchain/apt/source-metadata.txt
    mkdir -p "/toolchain/notices/$package"
    cp "/usr/share/doc/$package/copyright" "/toolchain/notices/$package/"
done
# Retain the authenticated binary contents for the build-time byte comparison.
# These compiler binaries are NOT copied into the recipient source/runtime bundle.
mkdir -p /toolchain/package-files
for archive in /toolchain/debs/*.deb; do
    dpkg-deb -x "$archive" /toolchain/package-files
done
cp -R /usr/share/common-licenses /toolchain/notices/
find /toolchain -type f -exec sha256sum {} + > /toolchain-files.sha256
