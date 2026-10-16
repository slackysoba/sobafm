# syntax=docker/dockerfile:1
# Both native Linux targets use the same multi-platform upstream toolchain index.
FROM buildpack-deps:trixie@sha256:ffbfa3ee92287a5fa41c03118bfe9a75c095838bccbe1173322854b8f2387116
ARG NATIVE_TARGET
COPY prepare-toolchain.sh /usr/local/bin/prepare-sobafm-toolchain
RUN sh /usr/local/bin/prepare-sobafm-toolchain "$NATIVE_TARGET"
ENV LANG=C LC_ALL=C TZ=UTC SOURCE_DATE_EPOCH=1789692057
WORKDIR /work
