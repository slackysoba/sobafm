#!/usr/bin/env bash
# Run the required checks on a pushed commit, from a clean Linux clone.
#
# Usage: scripts/check-commit.sh <branch-or-sha> [slot]
#
# Tests run on Linux. On a Windows host, run this inside WSL. Use a different
# slot for each concurrent check: slots share uv's package cache, not an
# environment. Set SOBAFM_CHECK_ROOT to change where clones and uv are kept.
set -euo pipefail

ref=${1:?usage: check-commit.sh <branch-or-sha> [slot]}
slot=${2:-default}
root=${SOBAFM_CHECK_ROOT:-$HOME/sobafm-check}
uv_version=0.13.6
archive=uv-x86_64-unknown-linux-gnu.tar.gz
tools=$root/tools/uv-$uv_version

if [ ! -x "$tools/uv" ]; then
  download=$(mktemp -d)
  trap 'rm -rf "$download"' EXIT
  base=https://github.com/astral-sh/uv/releases/download/$uv_version/$archive
  curl -fsSL "$base" -o "$download/$archive"
  curl -fsSL "$base.sha256" -o "$download/$archive.sha256"
  (cd "$download" && sha256sum -c "$archive.sha256")
  mkdir -p "$tools"
  tar -xzf "$download/$archive" --strip-components=1 -C "$tools"
fi
export PATH="$tools:$PATH"

clone=$root/$slot
if [ ! -d "$clone/.git" ]; then
  git clone -q https://github.com/slackysoba/sobafm.git "$clone"
fi
cd "$clone"
git fetch -q origin "$ref"
git checkout -q --force --detach FETCH_HEAD
git clean -fdxq -e .venv

uv sync --locked
uv run pre-commit run --all-files
uv run pytest -q
echo "checks passed at $(git rev-parse HEAD)"
