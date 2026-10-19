#!/bin/sh
# Python is already inventoried in the pinned builder; this adds no dependency.
set -eu
exec python3 /work/recipe/correspondence.py /work /toolchain /inputs/ffmpeg-9.0.2.tar.xz "$SOBAFM_NATIVE_TARGET"
