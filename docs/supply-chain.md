# Supply chain

What the SobaFM image contains, what its licenses owe and how to keep it current. The requirements' [Deployment, privacy and supply chain](requirements.md#deployment-privacy-and-supply-chain) section asks for this record. **It is a project record, not legal advice.**

The image isn't published, and nothing here publishes it. Whether a release should publish one is [an open question for the maintainer](#publishing-is-not-settled).

## Where this comes from

Everything below was read from one actual image:

| Fact | Value |
| --- | --- |
| Image | Local tag `sobafm-live:ab10a71`, ID `sha256:4a91d588f7d49389e0caa01aafabcdb9d86e4b009e96092fb1115472cdfb1355`, linux/amd64, 336,429,161 bytes |
| Bound to | Commit `ab10a71`. All 13 `.py` files of the installed `sobafm` package match it by SHA-256. `Dockerfile`, `.dockerignore`, `pyproject.toml`, `uv.lock` and `src/` didn't change from that commit to `347870a`, the `main` this record started from |
| Read on | 2026-11-05, US Pacific time |

That image predates the [notices file](#notices-file), which is the only change this record makes to what an image contains. No image was built for it.

Reading it took only `docker image inspect` and `docker run --rm --network none --entrypoint <command>`, with no environment file, no mounts and no bot start. Run these against a new image to refresh the tables:

```sh
image=sobafm-live:ab10a71
docker image inspect "$image"
# Debian packages, their sources and what they embed.
docker run --rm --network none --entrypoint dpkg-query "$image" -W -f \
  '${Package}\t${Version}\t${source:Package}\t${source:Version}\t${Built-Using}\t${Static-Built-Using}\n'
# Every file, then the files dpkg owns. What's left is CPython, pip, uv and the virtual environment.
docker run --rm --network none --entrypoint find "$image" / -xdev -type f
docker run --rm --network none --entrypoint grep "$image" -r --include='*.list' -e '' /var/lib/dpkg/info
# Python packages and their licenses.
docker run --rm --network none --entrypoint find "$image" \
  /app/.venv/lib/python3.14/site-packages -maxdepth 1 -name '*.dist-info'
docker run --rm --network none --entrypoint grep "$image" -r --include=METADATA \
  -E -e '^Version: |^License: |^License-Expression: ' /app/.venv/lib/python3.14/site-packages
# Debian's copyright files.
docker run --rm --network none --entrypoint grep "$image" -R --include=copyright -e '' /usr/share/doc
```

## How the image is put together

- **Base image:** `ghcr.io/astral-sh/uv@sha256:8f4ab6a0f2cc074290ba6a136eb016350b2b6b93a2108e07b72724901ad1914f`. Its labels say `0.12.21-python3.14-trixie-slim`, revision `7af826859382eb191e47467540850caa8f493e5b`, created 2026-09-29. uv's [release workflow](https://github.com/astral-sh/uv/blob/0.12.21/.github/workflows/build-docker.yml) builds that tag from `python:3.14-slim-trixie` plus its `uv` and `uvx` binaries. It supplies 87 Debian packages, CPython 3.14.7, pip 26.2.1 and uv 0.12.21.
- **Build stage only:** `ghcr.io/astral-sh/uv:0.13.6@sha256:af22a52f5eafef3d78e551599d405c02df61919d18028f0cea8eda93bc9986d0` provides the uv that installs the locked wheels. That uv isn't in the final image.
- **Debian pins:** the `Dockerfile` adds 27 packages at exact versions, four from forky and 23 from trixie. All 27 match the image. `--no-upgrade` keeps the base packages at the versions the digest fixed.
- **Python:** the 17 [locked runtime packages](#locked-python-packages), in `/app/.venv`.
- **Not in the image:** FFmpeg, a compiler, music or credentials. `.dockerignore` limits the build context to `pyproject.toml`, `uv.lock`, `README.md`, `LICENSE`, `THIRD-PARTY-NOTICES.txt` and `src/`. CPython's `_tkinter` module names Tcl/Tk libraries that aren't there either.

### Decoder route

SobaFM starts `/usr/bin/mpg123` as a child process and reads raw PCM from its stdout ([`decode.py`](../src/sobafm/decode.py)). SobaFM doesn't link it. The program uses `libmpg123-0t64`, `libout123-0t64` and `libsyn123-0t64`, all from the same forky source, mpg123 1.33.7-1, under LGPL-2.1.

Debian's `mpg123` package also depends on the libraries behind its audio output modules, `/usr/lib/x86_64-linux-gnu/mpg123/output_*.so` (alsa, dummy, jack, openal, oss, portaudio, pulse). SobaFM has no use for them, because it reads stdout. They arrive only as dependencies, but they're in the image and owe the same as anything else:

| Depended on by | Packages |
| --- | --- |
| `mpg123` | libasound2t64, libjack-jackd2-0, libopenal1, libportaudio2, libpulse0 |
| libasound2t64, libopenal1 | libasound2-data, libopenal-data |
| libpulse0 | libasyncns0, libdbus-1-3, libsndfile1, libx11-6, libx11-xcb1, libxcb1 |
| libsndfile1 | libflac14, libmp3lame0, libogg0, libopus0, libvorbis0a, libvorbisenc2 |
| libjack-jackd2-0 | libopus0, libsamplerate0 |
| libx11-6, libxcb1 | libx11-data, libxau6, libxdmcp6 |

disnake finds and loads libopus0 itself for voice encoding, so that's the one library in this list SobaFM uses.

## Locked Python packages

`tests/test_supply_chain.py` fails if this table differs from the runtime packages in `uv.lock`. The image above contains exactly these 17 at these versions. Development tools aren't in the image.

Licenses are from each wheel's metadata, and the texts are in its `.dist-info/licenses/` folder. Each name links to the PyPI release, which offers the source distribution that `uv.lock` names. SobaFM's own source is this repository.

| Package | Version | License | Compiled code |
| --- | --- | --- | --- |
| [aiohappyeyeballs](https://pypi.org/project/aiohappyeyeballs/2.7.1/) | 2.7.1 | PSF-2.0 | No |
| [aiohttp](https://pypi.org/project/aiohttp/3.14.5/) | 3.14.5 | Apache-2.0 AND MIT | Yes |
| [aiosignal](https://pypi.org/project/aiosignal/1.4.0/) | 1.4.0 | Apache-2.0 | No |
| [attrs](https://pypi.org/project/attrs/26.1.0/) | 26.1.0 | MIT | No |
| [audioop-lts](https://pypi.org/project/audioop-lts/0.2.2/) | 0.2.2 | PSF-2.0 | Yes |
| [cffi](https://pypi.org/project/cffi/2.1.1/) | 2.1.1 | MIT-0 | Yes |
| [dave-py](https://pypi.org/project/dave-py/1.1.0/) | 1.1.0 | MIT | Yes |
| [disnake](https://pypi.org/project/disnake/2.12.2/) | 2.12.2 | MIT | No |
| [frozenlist](https://pypi.org/project/frozenlist/1.8.0/) | 1.8.0 | Apache-2.0 | Yes |
| [idna](https://pypi.org/project/idna/3.20/) | 3.20 | BSD-3-Clause | No |
| [multidict](https://pypi.org/project/multidict/7.2.1/) | 7.2.1 | Apache-2.0 | Yes |
| [propcache](https://pypi.org/project/propcache/0.5.4/) | 0.5.4 | Apache-2.0 | Yes |
| [pycparser](https://pypi.org/project/pycparser/3.12/) | 3.12 | BSD-3-Clause | No |
| [pynacl](https://pypi.org/project/pynacl/1.6.2/) | 1.6.2 | Apache-2.0 | Yes |
| sobafm | 0.0.0 | MIT | No |
| [typing-extensions](https://pypi.org/project/typing-extensions/4.16.0/) | 4.16.0 | PSF-2.0 | No |
| [yarl](https://pypi.org/project/yarl/1.25.1/) | 1.25.1 | Apache-2.0 | Yes |

### Libraries compiled into those wheels

Dependency scanners read package names, so they don't see these. Every wheel carries its libraries' license texts in `.dist-info/licenses/`, except that cffi's doesn't cover libffi. The [notices file](#notices-file) points to it.

| Wheel | Library | Version | License | Where the version comes from |
| --- | --- | --- | --- | --- |
| dave-py | [libdave](https://github.com/DisnakeDev/libdave/tree/1e21de0e99466f729380c48451b44ac759f8eae6) | v1.2.2, fork at `1e21de0` | MIT | [ADR 0005](decisions/0005-discord-voice-library.md) |
| dave-py | [mlspp](https://github.com/cisco/mlspp) | Not recorded in the wheel | BSD-2-Clause | Built through libdave's vcpkg manifest |
| dave-py | [nlohmann-json](https://github.com/nlohmann/json) | Not recorded in the wheel | MIT | Built through libdave's vcpkg manifest |
| dave-py | [nanobind](https://github.com/wjakob/nanobind) | `~=3.1.0` | BSD-3-Clause | dave.py's `pyproject.toml` at `v1.1.0` |
| dave-py | [OpenSSL libcrypto](https://github.com/openssl/openssl/tree/openssl-3.0.7) | 3.0.7 | Apache-2.0 | [libdave's manifest](https://github.com/DisnakeDev/libdave/blob/1e21de0e99466f729380c48451b44ac759f8eae6/cpp/vcpkg-alts/openssl_3/vcpkg.json). The binary has no version string to check it against |
| pynacl | [libsodium](https://github.com/jedisct1/libsodium) | 1.0.20 | ISC | `configure.ac` in PyNaCl at `1.6.2` |
| aiohttp | [llhttp](https://github.com/nodejs/llhttp) | 9.4.2 | MIT | aiohttp's submodule at `v3.14.5` |
| cffi | [libffi](https://github.com/libffi/libffi) | Not recorded in the wheel | MIT style | The extension links no system libffi and contains `ffi_call_unix64` |

**OpenSSL 3.0.7 is a known risk.** OpenSSL's [release strategy](https://openssl-library.org/policies/releasestrat/index.html) lists 3.0 as no longer supported, and 3.0.7 dates from 2022. Only a dave.py release can change it, as [ADR 0005](decisions/0005-discord-voice-library.md) records. This record doesn't change any dependency. The system OpenSSL, 3.5.7 from Debian, is separate and is what CPython's `ssl` module uses.

## CPython, pip and uv

These come with the base image and aren't Debian packages. Comparing every file in the image with dpkg's lists leaves only these, the virtual environment and system state such as `/etc` and `/var/lib`.

| Component | Version | Location | License | Source |
| --- | --- | --- | --- | --- |
| CPython | 3.14.7 | `/usr/local` | PSF-2.0, in `/usr/local/lib/python3.14/LICENSE.txt`, plus the incorporated code below | [Release](https://www.python.org/downloads/release/python-3147/), with `PYTHON_SHA256=3b48dac8fb59f62eaa67ac83c1eb12bda1b7a08406dd286e252c11a66be27f81` set in the image. [Build recipe](https://github.com/docker-library/python) |
| pip | 26.2.1 | `site-packages`, and a bundled wheel in `ensurepip` | MIT, with its vendored packages' licenses in its `.dist-info/licenses/` | [PyPI](https://pypi.org/project/pip/26.2.1/) |
| uv, uvx | 0.12.21, static musl binaries | `/usr/local/bin` | MIT or Apache-2.0, plus the crates they link | [Tag](https://github.com/astral-sh/uv/tree/0.12.21) |

SobaFM runs CPython from the virtual environment. It doesn't use pip or the base image's uv.

**Incorporated in CPython.** Its [`Misc/sbom.spdx.json`](https://github.com/python/cpython/blob/v3.14.7/Misc/sbom.spdx.json) names expat 2.8.2, mpdecimal 2.5.1 and HACL\* at `8ba599b`. `pyexpat` and `_decimal` link no system copies, so those are the included ones. mimalloc and other code listed in [`Doc/license.rst`](https://github.com/python/cpython/blob/v3.14.7/Doc/license.rst) is included too. The image doesn't otherwise carry these notices, so they're in the [notices file](#notices-file).

**Linked from Debian.** OpenSSL, libffi, zlib, sqlite, bzip2, xz, zstd, readline, ncurses, gdbm, Berkeley DB and libuuid. CPython's `readline` and `_gdbm` modules link GPL-3+ libraries and `_dbm` links Sleepycat Berkeley DB. Neither SobaFM nor anything in its virtual environment imports them.

**uv's crates.** uv's [`Cargo.lock`](https://github.com/astral-sh/uv/blob/0.12.21/Cargo.lock) at that tag has 737 entries, 664 of them from outside its workspace. They include [astral-pubgrub and astral-version-ranges](https://github.com/astral-sh/pubgrub) under MPL-2.0. This record doesn't list each crate's license. The image has no file carrying their notices, and uv's repository has none at its top level. See [Publishing is not settled](#publishing-is-not-settled).

## Debian packages

All 114 are installed and come from Debian `main`. Each has a copyright file at `/usr/share/doc/<package>/copyright` in the image, which is the authority: the license column is a summary of what the package ships and of that file's main terms. Package pages are at `https://packages.debian.org/trixie/<package>`, or `forky` for the four forky pins.

Each source link was checked on 2026-11-05. Superseded versions leave sources.debian.org and stay on [snapshot.debian.org](https://snapshot.debian.org/), which returned HTTP 504 when checked that day, so its copies of these versions weren't confirmed.

`sqv` is the only package that declares embedded sources. Its `Static-Built-Using` field names 65 more Debian source packages, Rust crates and rustc, so its corresponding source includes those. Read the list from the `dpkg-query` command above.

| Package | Version | Source package | Arrives through | License summary |
| --- | --- | --- | --- | --- |
| adduser | 3.152 | [adduser 3.152](https://sources.debian.org/src/adduser/3.152/) | Base image | GPL-2+ |
| apt | 3.0.3 | [apt 3.0.3](https://sources.debian.org/src/apt/3.0.3/) | Base image | GPL-2+; parts BSD-3-Clause, Expat, curl |
| base-files | 13.8+deb13u7 | [base-files 13.8+deb13u7](https://sources.debian.org/src/base-files/13.8%2Bdeb13u7/) | Base image | GPL-2+ |
| base-passwd | 3.6.7 | [base-passwd 3.6.7](https://sources.debian.org/src/base-passwd/3.6.7/) | Base image | GPL-2; public domain |
| bash | 5.2.37-2+b10 | [bash 5.2.37-2](https://sources.debian.org/src/bash/5.2.37-2/) | Base image | GPL-3+ |
| bsdutils | 1:2.41.5-0+deb13u1 | [util-linux 2.41.5-0+deb13u1](https://sources.debian.org/src/util-linux/2.41.5-0%2Bdeb13u1/) | Base image | GPL-2+ |
| ca-certificates | 20250419 | [ca-certificates 20250419](https://sources.debian.org/src/ca-certificates/20250419/) | Base image | MPL-2.0 (certificates); GPL-2+ (scripts) |
| coreutils | 9.7-3 | [coreutils 9.7-3](https://sources.debian.org/src/coreutils/9.7-3/) | Base image | GPL-3+ |
| dash | 0.5.12-12 | [dash 0.5.12-12](https://sources.debian.org/src/dash/0.5.12-12/) | Base image | BSD-3-Clause; part GPL-2+ |
| debconf | 1.5.91 | [debconf 1.5.91](https://sources.debian.org/src/debconf/1.5.91/) | Base image | BSD-2-Clause |
| debian-archive-keyring | 2025.1 | [debian-archive-keyring 2025.1](https://sources.debian.org/src/debian-archive-keyring/2025.1/) | Base image | GPL (keys carry no copyright) |
| debianutils | 5.23.2 | [debianutils 5.23.2](https://sources.debian.org/src/debianutils/5.23.2/) | Base image | GPL-2+; parts public domain, SMAIL-GPL |
| diffutils | 1:3.10-4 | [diffutils 1:3.10-4](https://sources.debian.org/src/diffutils/1%3A3.10-4/) | Base image | GPL-3+ |
| dpkg | 1.22.22 | [dpkg 1.22.22](https://sources.debian.org/src/dpkg/1.22.22/) | Base image | GPL-2+ |
| findutils | 4.10.0-3 | [findutils 4.10.0-3](https://sources.debian.org/src/findutils/4.10.0-3/) | Base image | GPL-3+ |
| gcc-14-base | 14.2.0-19 | [gcc-14 14.2.0-19](https://sources.debian.org/src/gcc-14/14.2.0-19/) | Base image | GPL-3+ with GCC Runtime Library Exception |
| grep | 3.11-4 | [grep 3.11-4](https://sources.debian.org/src/grep/3.11-4/) | Base image | GPL-3+ |
| gzip | 1.13-1+deb13u1 | [gzip 1.13-1+deb13u1](https://sources.debian.org/src/gzip/1.13-1%2Bdeb13u1/) | Base image | GPL-3+ |
| hostname | 3.25 | [hostname 3.25](https://sources.debian.org/src/hostname/3.25/) | Base image | GPL-2 |
| init-system-helpers | 1.69~deb13u1 | [init-system-helpers 1.69~deb13u1](https://sources.debian.org/src/init-system-helpers/1.69~deb13u1/) | Base image | BSD-3-Clause; scripts GPL-2+ |
| libacl1 | 2.3.2-2+b1 | [acl 2.3.2-2](https://sources.debian.org/src/acl/2.3.2-2/) | Base image | LGPL-2+ |
| libapt-pkg7.0 | 3.0.3 | [apt 3.0.3](https://sources.debian.org/src/apt/3.0.3/) | Base image | GPL-2+; parts BSD-3-Clause, Expat, curl |
| libasound2-data | 1.2.14-1+deb13u1 | [alsa-lib 1.2.14-1+deb13u1](https://sources.debian.org/src/alsa-lib/1.2.14-1%2Bdeb13u1/) | Pin, trixie | LGPL-2.1+ |
| libasound2t64 | 1.2.14-1+deb13u1 | [alsa-lib 1.2.14-1+deb13u1](https://sources.debian.org/src/alsa-lib/1.2.14-1%2Bdeb13u1/) | Pin, trixie | LGPL-2.1+ |
| libasyncns0 | 0.8-6+b5 | [libasyncns 0.8-6](https://sources.debian.org/src/libasyncns/0.8-6/) | Pin, trixie | LGPL-2.1+ |
| libattr1 | 1:2.5.2-3 | [attr 1:2.5.2-3](https://sources.debian.org/src/attr/1%3A2.5.2-3/) | Base image | LGPL-2+ |
| libaudit-common | 1:4.0.2-2+deb13u1 | [audit 1:4.0.2-2+deb13u1](https://sources.debian.org/src/audit/1%3A4.0.2-2%2Bdeb13u1/) | Base image | LGPL-2.1 (library); GPL-2 |
| libaudit1 | 1:4.0.2-2+deb13u1 | [audit 1:4.0.2-2+deb13u1](https://sources.debian.org/src/audit/1%3A4.0.2-2%2Bdeb13u1/) | Base image | LGPL-2.1 (library); GPL-2 |
| libblkid1 | 2.41.5-0+deb13u1 | [util-linux 2.41.5-0+deb13u1](https://sources.debian.org/src/util-linux/2.41.5-0%2Bdeb13u1/) | Base image | LGPL-2.1+ |
| libbsd0 | 0.12.2-2 | [libbsd 0.12.2-2](https://sources.debian.org/src/libbsd/0.12.2-2/) | Base image | BSD family, ISC, Expat, Beerware |
| libbz2-1.0 | 1.0.8-6 | [bzip2 1.0.8-6](https://sources.debian.org/src/bzip2/1.0.8-6/) | Base image | bzip2 BSD variant |
| libc-bin | 2.41-12+deb13u4 | [glibc 2.41-12+deb13u4](https://sources.debian.org/src/glibc/2.41-12%2Bdeb13u4/) | Base image | LGPL-2.1+; parts GPL-2+ and permissive |
| libc6 | 2.41-12+deb13u4 | [glibc 2.41-12+deb13u4](https://sources.debian.org/src/glibc/2.41-12%2Bdeb13u4/) | Base image | LGPL-2.1+; parts GPL-2+ and permissive |
| libcap-ng0 | 0.8.5-4+b1 | [libcap-ng 0.8.5-4](https://sources.debian.org/src/libcap-ng/0.8.5-4/) | Base image | LGPL-2.1+ |
| libcap2 | 1:2.75-10+deb13u1+b3 | [libcap2 1:2.75-10+deb13u1](https://sources.debian.org/src/libcap2/1%3A2.75-10%2Bdeb13u1/) | Base image | BSD-3-Clause or GPL-2 |
| libcrypt1 | 1:4.4.38-1 | [libxcrypt 1:4.4.38-1](https://sources.debian.org/src/libxcrypt/1%3A4.4.38-1/) | Base image | LGPL-2.1+; parts BSD, public domain |
| libdb5.3t64 | 5.3.28+dfsg2-9 | [db5.3 5.3.28+dfsg2-9](https://sources.debian.org/src/db5.3/5.3.28%2Bdfsg2-9/) | Base image | Sleepycat and BSD-3-Clause |
| libdbus-1-3 | 1.16.2-2 | [dbus 1.16.2-2](https://sources.debian.org/src/dbus/1.16.2-2/) | Pin, trixie | AFL-2.1 or GPL-2+; parts Expat |
| libdebconfclient0 | 0.280 | [cdebconf 0.280](https://sources.debian.org/src/cdebconf/0.280/) | Base image | BSD-2-Clause |
| libffi8 | 3.4.8-2 | [libffi 3.4.8-2](https://sources.debian.org/src/libffi/3.4.8-2/) | Base image | Expat |
| libflac14 | 1.5.0+ds-2 | [flac 1.5.0+ds-2](https://sources.debian.org/src/flac/1.5.0%2Bds-2/) | Pin, trixie | BSD-3-Clause (library) |
| libgcc-s1 | 14.2.0-19 | [gcc-14 14.2.0-19](https://sources.debian.org/src/gcc-14/14.2.0-19/) | Base image | GPL-3+ with GCC Runtime Library Exception |
| libgdbm6t64 | 1.24-2 | [gdbm 1.24-2](https://sources.debian.org/src/gdbm/1.24-2/) | Base image | GPL-3+ |
| libgmp10 | 2:6.3.0+dfsg-3 | [gmp 2:6.3.0+dfsg-3](https://sources.debian.org/src/gmp/2%3A6.3.0%2Bdfsg-3/) | Base image | LGPL-3+ or GPL-2+ |
| libhogweed6t64 | 3.10.1-1 | [nettle 3.10.1-1](https://sources.debian.org/src/nettle/3.10.1-1/) | Base image | LGPL-3+ or GPL-2+ |
| libjack-jackd2-0 | 1.9.22~dfsg-4 | [jackd2 1.9.22~dfsg-4](https://sources.debian.org/src/jackd2/1.9.22~dfsg-4/) | Pin, trixie | LGPL-2.1+; some files GPL-2+ |
| liblastlog2-2 | 2.41.5-0+deb13u1 | [util-linux 2.41.5-0+deb13u1](https://sources.debian.org/src/util-linux/2.41.5-0%2Bdeb13u1/) | Base image | GPL-2+; parts BSD-2-Clause |
| liblz4-1 | 1.10.0-4 | [lz4 1.10.0-4](https://sources.debian.org/src/lz4/1.10.0-4/) | Base image | BSD-2-Clause (library) |
| liblzma5 | 5.8.1-1+deb13u1 | [xz-utils 5.8.1-1+deb13u1](https://sources.debian.org/src/xz-utils/5.8.1-1%2Bdeb13u1/) | Base image | 0BSD (library) |
| libmd0 | 1.1.0-2+b1 | [libmd 1.1.0-2](https://sources.debian.org/src/libmd/1.1.0-2/) | Base image | BSD family, ISC, Beerware, public domain |
| libmount1 | 2.41.5-0+deb13u1 | [util-linux 2.41.5-0+deb13u1](https://sources.debian.org/src/util-linux/2.41.5-0%2Bdeb13u1/) | Base image | LGPL-2.1+ |
| libmp3lame0 | 3.100-6+b3 | [lame 3.100-6](https://sources.debian.org/src/lame/3.100-6/) | Pin, trixie | LGPL-2+; fft.c GPL-1+ |
| libmpg123-0t64 | 1.33.7-1 | [mpg123 1.33.7-1](https://sources.debian.org/src/mpg123/1.33.7-1/) | Pin, forky | LGPL-2.1 |
| libncursesw6 | 6.5+20250216-2 | [ncurses 6.5+20250216-2](https://sources.debian.org/src/ncurses/6.5%2B20250216-2/) | Base image | MIT/X11 |
| libnettle8t64 | 3.10.1-1 | [nettle 3.10.1-1](https://sources.debian.org/src/nettle/3.10.1-1/) | Base image | LGPL-3+ or GPL-2+ |
| libogg0 | 1.3.5-3+b2 | [libogg 1.3.5-3](https://sources.debian.org/src/libogg/1.3.5-3/) | Pin, trixie | BSD-3-Clause |
| libopenal-data | 1:1.24.2-1 | [openal-soft 1:1.24.2-1](https://sources.debian.org/src/openal-soft/1%3A1.24.2-1/) | Pin, trixie | LGPL-2+; parts Expat, Apache-2.0 |
| libopenal1 | 1:1.24.2-1 | [openal-soft 1:1.24.2-1](https://sources.debian.org/src/openal-soft/1%3A1.24.2-1/) | Pin, trixie | LGPL-2+; parts Expat, Apache-2.0 |
| libopus0 | 1.5.2-2 | [opus 1.5.2-2](https://sources.debian.org/src/opus/1.5.2-2/) | Pin, trixie | BSD-3-Clause |
| libout123-0t64 | 1.33.7-1 | [mpg123 1.33.7-1](https://sources.debian.org/src/mpg123/1.33.7-1/) | Pin, forky | LGPL-2.1 |
| libpam-modules | 1.7.0-5 | [pam 1.7.0-5](https://sources.debian.org/src/pam/1.7.0-5/) | Base image | BSD-3-Clause or GPL; parts LGPL-2+ |
| libpam-modules-bin | 1.7.0-5 | [pam 1.7.0-5](https://sources.debian.org/src/pam/1.7.0-5/) | Base image | BSD-3-Clause or GPL; parts LGPL-2+ |
| libpam-runtime | 1.7.0-5 | [pam 1.7.0-5](https://sources.debian.org/src/pam/1.7.0-5/) | Base image | BSD-3-Clause or GPL; parts LGPL-2+ |
| libpam0g | 1.7.0-5 | [pam 1.7.0-5](https://sources.debian.org/src/pam/1.7.0-5/) | Base image | BSD-3-Clause or GPL; parts LGPL-2+ |
| libpcre2-8-0 | 10.46-1~deb13u2 | [pcre2 10.46-1~deb13u2](https://sources.debian.org/src/pcre2/10.46-1~deb13u2/) | Base image | BSD-3-Clause with exception |
| libportaudio2 | 19.6.0-1.2+b3 | [portaudio19 19.6.0-1.2](https://sources.debian.org/src/portaudio19/19.6.0-1.2/) | Pin, trixie | MIT style |
| libpulse0 | 17.0+dfsg1-2+b1 | [pulseaudio 17.0+dfsg1-2](https://sources.debian.org/src/pulseaudio/17.0%2Bdfsg1-2/) | Pin, trixie | LGPL-2.1+; parts permissive |
| libreadline8t64 | 8.2-6 | [readline 8.2-6](https://sources.debian.org/src/readline/8.2-6/) | Base image | GPL-3+ |
| libsamplerate0 | 0.2.2-4+b2 | [libsamplerate 0.2.2-4](https://sources.debian.org/src/libsamplerate/0.2.2-4/) | Pin, trixie | BSD-2-Clause |
| libseccomp2 | 2.6.0-2 | [libseccomp 2.6.0-2](https://sources.debian.org/src/libseccomp/2.6.0-2/) | Base image | LGPL-2.1 |
| libselinux1 | 3.8.1-1 | [libselinux 3.8.1-1](https://sources.debian.org/src/libselinux/3.8.1-1/) | Base image | Public domain |
| libsemanage-common | 3.8.1-1 | [libsemanage 3.8.1-1](https://sources.debian.org/src/libsemanage/3.8.1-1/) | Base image | LGPL-2.1+ |
| libsemanage2 | 3.8.1-1 | [libsemanage 3.8.1-1](https://sources.debian.org/src/libsemanage/3.8.1-1/) | Base image | LGPL-2.1+ |
| libsepol2 | 3.8.1-1 | [libsepol 3.8.1-1](https://sources.debian.org/src/libsepol/3.8.1-1/) | Base image | LGPL-2.1+ |
| libsmartcols1 | 2.41.5-0+deb13u1 | [util-linux 2.41.5-0+deb13u1](https://sources.debian.org/src/util-linux/2.41.5-0%2Bdeb13u1/) | Base image | LGPL-2.1+ |
| libsndfile1 | 1.2.2-2+deb13u1 | [libsndfile 1.2.2-2+deb13u1](https://sources.debian.org/src/libsndfile/1.2.2-2%2Bdeb13u1/) | Pin, trixie | LGPL-2.1+; parts Apache-2.0, BSD-3-Clause |
| libsqlite3-0 | 3.46.1-7+deb13u2 | [sqlite3 3.46.1-7+deb13u2](https://sources.debian.org/src/sqlite3/3.46.1-7%2Bdeb13u2/) | Base image | Public domain |
| libssl3t64 | 3.5.7-1~deb13u2 | [openssl 3.5.7-1~deb13u2](https://sources.debian.org/src/openssl/3.5.7-1~deb13u2/) | Base image | Apache-2.0 |
| libstdc++6 | 14.2.0-19 | [gcc-14 14.2.0-19](https://sources.debian.org/src/gcc-14/14.2.0-19/) | Base image | GPL-3+ with GCC Runtime Library Exception |
| libsyn123-0t64 | 1.33.7-1 | [mpg123 1.33.7-1](https://sources.debian.org/src/mpg123/1.33.7-1/) | Pin, forky | LGPL-2.1 |
| libsystemd0 | 257.13-1~deb13u1 | [systemd 257.13-1~deb13u1](https://sources.debian.org/src/systemd/257.13-1~deb13u1/) | Base image | LGPL-2.1+ |
| libtinfo6 | 6.5+20250216-2 | [ncurses 6.5+20250216-2](https://sources.debian.org/src/ncurses/6.5%2B20250216-2/) | Base image | MIT/X11 |
| libudev1 | 257.13-1~deb13u1 | [systemd 257.13-1~deb13u1](https://sources.debian.org/src/systemd/257.13-1~deb13u1/) | Base image | LGPL-2.1+ |
| libuuid1 | 2.41.5-0+deb13u1 | [util-linux 2.41.5-0+deb13u1](https://sources.debian.org/src/util-linux/2.41.5-0%2Bdeb13u1/) | Base image | BSD-3-Clause |
| libvorbis0a | 1.3.7-3 | [libvorbis 1.3.7-3](https://sources.debian.org/src/libvorbis/1.3.7-3/) | Pin, trixie | BSD-3-Clause |
| libvorbisenc2 | 1.3.7-3 | [libvorbis 1.3.7-3](https://sources.debian.org/src/libvorbis/1.3.7-3/) | Pin, trixie | BSD-3-Clause |
| libx11-6 | 2:1.8.12-1 | [libx11 2:1.8.12-1](https://sources.debian.org/src/libx11/2%3A1.8.12-1/) | Pin, trixie | MIT/X11 family |
| libx11-data | 2:1.8.12-1 | [libx11 2:1.8.12-1](https://sources.debian.org/src/libx11/2%3A1.8.12-1/) | Pin, trixie | MIT/X11 family |
| libx11-xcb1 | 2:1.8.12-1 | [libx11 2:1.8.12-1](https://sources.debian.org/src/libx11/2%3A1.8.12-1/) | Pin, trixie | MIT/X11 family |
| libxau6 | 1:1.0.11-1 | [libxau 1:1.0.11-1](https://sources.debian.org/src/libxau/1%3A1.0.11-1/) | Pin, trixie | MIT/X11 |
| libxcb1 | 1.17.0-2+b1 | [libxcb 1.17.0-2](https://sources.debian.org/src/libxcb/1.17.0-2/) | Pin, trixie | MIT/X11 |
| libxdmcp6 | 1:1.1.5-1 | [libxdmcp 1:1.1.5-1](https://sources.debian.org/src/libxdmcp/1%3A1.1.5-1/) | Pin, trixie | MIT/X11 |
| libxxhash0 | 0.8.3-2 | [xxhash 0.8.3-2](https://sources.debian.org/src/xxhash/0.8.3-2/) | Base image | BSD-2-Clause (library) |
| libzstd1 | 1.5.7+dfsg-1 | [libzstd 1.5.7+dfsg-1](https://sources.debian.org/src/libzstd/1.5.7%2Bdfsg-1/) | Base image | BSD-3-Clause or GPL-2 |
| login | 1:4.16.0-2+really2.41.5-0+deb13u1 | [util-linux 2.41.5-0+deb13u1](https://sources.debian.org/src/util-linux/2.41.5-0%2Bdeb13u1/) | Base image | GPL-2+ |
| login.defs | 1:4.17.4-2 | [shadow 1:4.17.4-2](https://sources.debian.org/src/shadow/1%3A4.17.4-2/) | Base image | BSD-3-Clause; parts GPL-2+ |
| mawk | 1.3.4.20250131-1 | [mawk 1.3.4.20250131-1](https://sources.debian.org/src/mawk/1.3.4.20250131-1/) | Base image | GPL-2 |
| mount | 2.41.5-0+deb13u1 | [util-linux 2.41.5-0+deb13u1](https://sources.debian.org/src/util-linux/2.41.5-0%2Bdeb13u1/) | Base image | GPL-2+ |
| mpg123 | 1.33.7-1 | [mpg123 1.33.7-1](https://sources.debian.org/src/mpg123/1.33.7-1/) | Pin, forky | LGPL-2.1 |
| ncurses-base | 6.5+20250216-2 | [ncurses 6.5+20250216-2](https://sources.debian.org/src/ncurses/6.5%2B20250216-2/) | Base image | MIT/X11 |
| ncurses-bin | 6.5+20250216-2 | [ncurses 6.5+20250216-2](https://sources.debian.org/src/ncurses/6.5%2B20250216-2/) | Base image | MIT/X11 |
| netbase | 6.5 | [netbase 6.5](https://sources.debian.org/src/netbase/6.5/) | Base image | GPL-2 |
| openssl | 3.5.7-1~deb13u2 | [openssl 3.5.7-1~deb13u2](https://sources.debian.org/src/openssl/3.5.7-1~deb13u2/) | Base image | Apache-2.0 |
| openssl-provider-legacy | 3.5.7-1~deb13u2 | [openssl 3.5.7-1~deb13u2](https://sources.debian.org/src/openssl/3.5.7-1~deb13u2/) | Base image | Apache-2.0 |
| passwd | 1:4.17.4-2 | [shadow 1:4.17.4-2](https://sources.debian.org/src/shadow/1%3A4.17.4-2/) | Base image | BSD-3-Clause; parts GPL-2+ |
| perl-base | 5.40.1-6+deb13u1 | [perl 5.40.1-6+deb13u1](https://sources.debian.org/src/perl/5.40.1-6%2Bdeb13u1/) | Base image | GPL-1+ or Artistic; parts permissive |
| readline-common | 8.2-6 | [readline 8.2-6](https://sources.debian.org/src/readline/8.2-6/) | Base image | GPL-3+ |
| sed | 4.9-2+deb13u1 | [sed 4.9-2+deb13u1](https://sources.debian.org/src/sed/4.9-2%2Bdeb13u1/) | Base image | GPL-3+ |
| sqv | 1.3.0-3+b2 | [rust-sequoia-sqv 1.3.0-3](https://sources.debian.org/src/rust-sequoia-sqv/1.3.0-3/) | Base image | LGPL-2.0+ |
| sysvinit-utils | 3.14-4 | [sysvinit 3.14-4](https://sources.debian.org/src/sysvinit/3.14-4/) | Base image | GPL-2+ |
| tar | 1.35+dfsg-3.1 | [tar 1.35+dfsg-3.1](https://sources.debian.org/src/tar/1.35%2Bdfsg-3.1/) | Base image | GPL-3+ |
| tzdata | 2026c-0+deb13u1 | [tzdata 2026c-0+deb13u1](https://sources.debian.org/src/tzdata/2026c-0%2Bdeb13u1/) | Base image | Public domain |
| util-linux | 2.41.5-0+deb13u1 | [util-linux 2.41.5-0+deb13u1](https://sources.debian.org/src/util-linux/2.41.5-0%2Bdeb13u1/) | Base image | GPL-2+; parts LGPL, BSD |
| zlib1g | 1:1.3.dfsg+really1.3.1-1+b1 | [zlib 1:1.3.dfsg+really1.3.1-1](https://sources.debian.org/src/zlib/1%3A1.3.dfsg%2Breally1.3.1-1/) | Base image | Zlib |

## What the licenses owe

**Building and running the image yourself owes nothing** under any of these licenses. Their conditions attach to distribution, not use, and no package here is AGPL, so operating a network service adds nothing. The repository itself distributes only source and a `Dockerfile`, so it carries none of these duties either.

**Publishing is different.** Pushing the image to a registry, or giving a saved copy to anyone else, distributes every component in it:

| License family | Examples in the image | What publishing owes |
| --- | --- | --- |
| Permissive with a notice: MIT/Expat/X11, BSD family, ISC, Zlib, bzip2, PSF-2.0, curl | disnake, libopus0, libx11-6, CPython, libsodium | Keep the copyright and license text with the binaries. Debian's copyright files, `.dist-info/licenses/` and the notices file do this, except for uv's crates |
| No notice required: public domain, MIT-0, 0BSD | tzdata, libsqlite3-0, cffi, liblzma5 | Nothing required. Their records stay in place anyway |
| Apache-2.0 | libssl3t64, OpenSSL in dave-py, aiohttp, yarl | Include the license, keep any `NOTICE` file (propcache and yarl have one) and mark changed files. Nothing here is changed |
| MPL-2.0 | ca-certificates' certificate data, astral-pubgrub in uv | Keep the license and tell recipients where to get the source of the covered files |
| LGPL 2, 2.1 and 3 | mpg123, libc6, libpulse0, libsndfile1, libopenal1 | License text and notices, plus the library's complete source offered as for GPL below. Recipients must be able to replace the library, which shared libraries in an unlocked image allow |
| GPL 1, 2 and 3 | bash, coreutils, grep, sed, tar, apt, dpkg, libreadline8t64, libgdbm6t64 | License text and notices, plus complete corresponding source, including Debian's patches and build scripts. Put it beside the image, or make a written offer. GPL-2 sets a three-year offer, and GPL-3 access from the same place for as long as the image is offered. See below on relying on Debian's servers |
| GPL with the GCC Runtime Library Exception | libgcc-s1, libstdc++6 | The exception frees programs linked against them. The libraries' own source duty remains, as for GPL |
| Sleepycat | libdb5.3t64 | Say how to get the source of Berkeley DB and of accompanying software that uses it. Here that's PAM's `pam_userdb`, libjack and CPython's `_dbm`, all with public source |
| A choice of licenses | libdbus-1-3 (AFL-2.1 or GPL-2+), perl-base (GPL-1+ or Artistic), libcap2 and libzstd1 (BSD-3-Clause or GPL-2), libgmp10 and nettle (LGPL-3+ or GPL-2+) | Choose one and follow its row |
| Documentation licenses: GFDL, CC-BY | Named in several copyright files | Nothing, because the slim base removes those documents and keeps only the copyright files |

The copyleft duties cover those components, not SobaFM's own code. SobaFM is MIT and runs mpg123 as a separate program. Its own process loads the C library (LGPL), GCC's runtime libraries (under the exception) and permissively licensed ones such as libopus and OpenSSL, so no GPL terms reach its code.

**Debian's servers aren't a safe substitute for the source.** GPL-3 lets a publisher use another server but keeps the publisher responsible for it staying available, and superseded versions leave the main archive, testing's fastest of all. A published image would need its own copy of the exact sources.

No term found here conflicts with publishing. Everything is from Debian `main` or under the licenses above. The gaps are in meeting the duties, not in being allowed to.

## Publishing is not settled

The image is not ready to publish as it stands:

1. **Source.** A publisher needs the exact source of all 80 Debian source packages, the 65 behind `sqv`, CPython 3.14.7 and its build recipe, kept available for as long as the duties last. None of them is FFmpeg, so collecting them doesn't touch the [source-access constraints](requirements.md#deployment-privacy-and-supply-chain). The total size isn't known.
2. **uv's crates.** The binary statically links crates from that list of 664, and neither the image nor this repository has their notices. A generated list from uv's tag, or a base image without uv, would close this. Deleting the binary in a later layer wouldn't, because the layer below would still be distributed.
3. **Labels.** The image inherits `org.opencontainers.image.title=uv`, `licenses=Apache-2.0` and the rest from its base. That's harmless locally and wrong for a published SobaFM image, which should set its own.

The options and a recommendation are [on issue #132](https://github.com/slackysoba/sobafm/issues/132). Until the maintainer answers, the image stays local-only, as [Operations](operations.md#image-contents) says.

## Notices file

`THIRD-PARTY-NOTICES.txt` carries what the image would otherwise lack: uv's notice and its known gap, a pointer for libffi in cffi, and CPython's notices for incorporated code. It also says where the other notices are in the image. The licenses need this only when publishing, but carrying it costs one small layer and keeps a local image and a published one the same.

The `Dockerfile` copies it to `/app/THIRD-PARTY-NOTICES.txt`, and `.dockerignore` admits it to the build context. `tests/test_supply_chain.py` checks both, because a mistake there would otherwise only show in a build. The CPython sections are copied from `Doc/license.rst` and `Modules/_hacl` at `v3.14.7`, leaving out code this Linux image doesn't contain, such as tests and Windows or BSD-only code. Refresh them whenever the CPython version changes.

**A rebuild still has to confirm:**

- the build succeeds with the new `COPY`;
- the file is in the image at that path, readable by UID 1000 under the read-only root;
- the 114 Debian and 17 Python entries are unchanged.

That rebuild also gets a new image ID, so re-run the listing above and update [Where this comes from](#where-this-comes-from).

## SBOM

Produce an SBOM from the actual release image with [Syft](https://github.com/anchore/syft). Nothing has been produced or published, and these commands haven't been run, because `docker image save` is outside what issue #132 approves on the shared engine.

```sh
version=1.57.0
base=https://github.com/anchore/syft/releases/download/v$version
curl -fsSLO "$base/syft_${version}_linux_amd64.tar.gz"
curl -fsSLO "$base/syft_${version}_checksums.txt"
sha256sum --check --ignore-missing "syft_${version}_checksums.txt"
tar -xzf "syft_${version}_linux_amd64.tar.gz" syft

docker image save sobafm:<commit> -o sobafm-<commit>.tar
SYFT_CHECK_FOR_APP_UPDATE=false ./syft scan docker-archive:sobafm-<commit>.tar \
  --output spdx-json=sobafm-<commit>.spdx.json
```

Check that the result has the Debian and Python counts above, and publish it with the image's ID, not just a tag. It isn't expected to see inside compiled files, so the [wheel libraries](#libraries-compiled-into-those-wheels) and [CPython's incorporated code](#cpython-pip-and-uv) tables travel with it.

| Tool | Maintenance and license | Fit |
| --- | --- | --- |
| **Syft (chosen)** | Apache-2.0. Active: six releases from 2026-09-17 to 2026-11-05, the latest v1.57.0, with checksums and a Sigstore bundle | One binary, kept out of the project and image. Reads Debian and Python packages from a saved image, so it needs no access to a Docker socket. Writes SPDX and CycloneDX |
| Trivy | Apache-2.0. Active, but its release channel [was compromised in March 2026](https://github.com/aquasecurity/trivy/security/advisories/GHSA-69fq-xp46-6x23) | Capable, with nothing here that Syft lacks, so there's no reason to take on that history |
| BuildKit's `--sbom` | Apache-2.0. Active, and part of Docker's tooling | Ties the SBOM to a build attestation, which nothing here requires |

## Updates

This extends the Python process in [Contributing](../CONTRIBUTING.md#tool-choices-and-updates). Each change gets its own PR. `scripts/check-commit.sh` runs on all of them, and any change to the image also needs a rebuild, the listing above and updated tables. Building on a shared Docker engine needs its own approval.

| Change | When | What to do | Checks beyond those |
| --- | --- | --- | --- |
| `uv.lock` | A release, an advisory or a new need | Follow Contributing. Update the [Python table](#locked-python-packages). For dave-py, pynacl, aiohttp or cffi, recheck the compiled library in the [wheel table](#libraries-compiled-into-those-wheels) | The drift test fails until the table matches. Rerun the service fit under [ADR 0004](decisions/0004-amd64-whole-service-adoption.md), because native libraries count towards the limit |
| Debian trixie pins | A security advisory, or a point release that replaces a pinned version | Find the version on its package page and change the exact pin. Keep `--no-remove --no-upgrade`, so a pin can't silently move a base package | The build fails if a version is gone. If it now needs a newer base package, update the base digest in the same PR |
| Debian forky pins | mpg123 moves on in forky, or an advisory | Change all four pins together, plus the `mpg123 --version` line in the `Dockerfile` and the pins in `tests/test_app.py`. Read upstream's release notes and Debian's changelog | A decode fit with an owned fixture, because this replaces the decoder. Forky versions leave the archive quickly, so the build fails rather than installing something else |
| Base digest | A security advisory for a base package, a CPython release or a new uv | Read the new digest without pulling it: `docker buildx imagetools inspect ghcr.io/astral-sh/uv:python3.14-trixie-slim`. Change `ARG BASE`. All 87 base versions may move together. Refresh this record's base, CPython and uv facts, and the notices file | Trixie pins may need to move with it, because `--no-upgrade` refuses an upgrade. A decode and service fit, because libc and CPython change |
| Build-stage uv | `required-version` in `pyproject.toml` changes | Change `ARG UV` to the new tag and digest in the same PR | `uv sync --locked` in the build. It doesn't reach the final image |

## Watching for vulnerabilities

Python dependency scanning doesn't cover native libraries, so each layer has its own watch. No job runs these on a schedule, because scheduled automation needs explicit approval. Check them at every release and whenever a Debian advisory names a listed source package.

| Layer | How it's watched |
| --- | --- |
| Locked Python packages | `uv audit --locked --no-default-groups` before each release and after a lock change. It sends package names to OSV, so it needs its own authorization, and it hasn't been run for this record |
| Debian packages from trixie | The [Debian Security Tracker](https://security-tracker.debian.org/tracker/), at `/tracker/source-package/<source>` for each source package, and the [security announcements list](https://lists.debian.org/debian-security-announce/). Base packages stay frozen by the digest, so a fix arrives only with a new digest |
| mpg123 from forky | Its [tracker page](https://security-tracker.debian.org/tracker/source-package/mpg123) and [upstream's news](https://www.mpg123.de/). Testing has [no timely security updates of its own](https://www.debian.org/security/faq): fixes migrate from unstable, with a delay |
| Libraries in wheels | Scanners don't see them. Watch [OpenSSL 3.0's vulnerability list](https://openssl-library.org/news/vulnerabilities-3.0/index.html) and dave.py's releases, then libsodium, llhttp and libffi through PyNaCl, aiohttp and cffi |
| CPython's incorporated code | Python security releases, which update expat and the others. A fix needs a new base digest |
| uv in the base image | Nothing runs it at runtime, so review it only when the base digest changes |
| The whole image, optionally | `grype sbom:sobafm-<commit>.spdx.json` matches Debian and Python packages from the SBOM against current advisories. [Grype](https://github.com/anchore/grype) is Apache-2.0 and active, with the same blind spots as Syft, and it downloads its vulnerability database, so it needs its own authorization |
