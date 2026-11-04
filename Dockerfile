# SobaFM first amd64 slice. Not yet built or qualified; see docs/operations.md.
#
# Base: the reviewed Python 3.14 trixie-slim uv image recorded in
# evaluations/linux_resource/decoder-startup.md, pinned by digest.
# Decoder: the reviewed mpg123 1.33.7 route recorded in
# evaluations/linux_resource/mpg123-proposal.md: the four mpg123 packages from
# Debian forky, every other package from stable, exact versions, no recommends.
# The image contains no FFmpeg and needs no compiler: every Python dependency
# in uv.lock installs from a wheel.

# pragma: allowlist nextline secret - public image digest
ARG BASE=ghcr.io/astral-sh/uv@sha256:8f4ab6a0f2cc074290ba6a136eb016350b2b6b93a2108e07b72724901ad1914f
# uv 0.13.6, the version pyproject.toml requires.
# pragma: allowlist nextline secret - public image digest
ARG UV=ghcr.io/astral-sh/uv:0.13.6@sha256:af22a52f5eafef3d78e551599d405c02df61919d18028f0cea8eda93bc9986d0

FROM --platform=linux/amd64 ${UV} AS uv

FROM --platform=linux/amd64 ${BASE} AS build
COPY --from=uv /uv /opt/uv/uv
ENV UV_PYTHON=/usr/local/bin/python3.14 UV_PYTHON_DOWNLOADS=never UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /build
COPY pyproject.toml uv.lock README.md LICENSE ./
RUN /opt/uv/uv sync --locked --no-default-groups --no-install-project --no-editable
COPY src ./src
RUN /opt/uv/uv sync --locked --no-default-groups --no-editable

FROM --platform=linux/amd64 ${BASE}
USER 0:0
RUN set -eu; \
    printf '%s\n' \
      'Types: deb' \
      'URIs: https://deb.debian.org/debian' \
      'Suites: forky' \
      'Components: main' \
      'Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg' \
      > /etc/apt/sources.list.d/forky.sources; \
    printf '%s\n' \
      'Package: mpg123 libmpg123-0t64 libout123-0t64 libsyn123-0t64' \
      'Pin: release n=forky' \
      'Pin-Priority: 500' \
      '' \
      'Package: *' \
      'Pin: release n=forky' \
      'Pin-Priority: -1' \
      > /etc/apt/preferences.d/mpg123-forky; \
    apt-get update; \
    apt-get install --yes --no-install-recommends --no-remove --no-upgrade \
      mpg123=1.33.7-1 libmpg123-0t64=1.33.7-1 libout123-0t64=1.33.7-1 \
      libsyn123-0t64=1.33.7-1 \
      libasound2-data=1.2.14-1+deb13u1 libasound2t64=1.2.14-1+deb13u1 \
      libasyncns0=0.8-6+b5 libdbus-1-3=1.16.2-2 libflac14=1.5.0+ds-2 \
      libjack-jackd2-0=1.9.22~dfsg-4 libmp3lame0=3.100-6+b3 libogg0=1.3.5-3+b2 \
      libopenal-data=1:1.24.2-1 libopenal1=1:1.24.2-1 libopus0=1.5.2-2 \
      libportaudio2=19.6.0-1.2+b3 libpulse0=17.0+dfsg1-2+b1 \
      libsamplerate0=0.2.2-4+b2 libsndfile1=1.2.2-2+deb13u1 \
      libvorbis0a=1.3.7-3 libvorbisenc2=1.3.7-3 \
      libx11-6=2:1.8.12-1 libx11-data=2:1.8.12-1 libx11-xcb1=2:1.8.12-1 \
      libxau6=1:1.0.11-1 libxcb1=1.17.0-2+b1 libxdmcp6=1:1.1.5-1; \
    rm -rf /var/lib/apt/lists/*; \
    test "$(mpg123 --version)" = "mpg123 1.33.7"; \
    mkdir -p /data; \
    chown 1000:1000 /data
COPY --from=build /app/.venv /app/.venv
USER 1000:1000
WORKDIR /data
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 LANG=C.UTF-8
ENTRYPOINT ["/app/.venv/bin/python", "-I", "-m", "sobafm"]
