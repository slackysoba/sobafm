"""Prepare one exact owned bootstrap check; execution needs separate review/clearance."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.metadata
import json
import os
import sys
from dataclasses import asdict
from pathlib import Path

from sobafm.worker_fit import (
    CLOSE_ATTEMPTS,
    DRAIN_LIMIT,
    MAXIMUM_CANDIDATE,
    OPERATION_LIMIT,
    FitCheck,
    Result,
    retained_wait,
)

ROOT = Path(__file__).resolve().parents[1]
FILES = (
    "src/sobafm/audio_worker.py",
    "src/sobafm/worker_fit.py",
    "src/sobafm/windows_decoder.py",
    "src/sobafm/decoder_process.py",
    "src/sobafm/native_calls.py",
    "src/sobafm/catalog_audio.py",
    "scripts/check_worker_fit.py",
    "uv.lock",
)


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def identity() -> dict[str, object]:
    python = Path(sys.executable).resolve()
    return {
        "schema": 2,
        "case": "242-worker-bootstrap-diagnostic",
        "cap": MAXIMUM_CANDIDATE,
        "python": str(python),
        "python_sha256": digest(python),
        "base_python_sha256": digest(Path(sys.base_prefix) / "python.exe"),
        "python_dll_sha256": digest(Path(sys.base_prefix) / "python314.dll"),
        "venv_config_sha256": digest(python.parent.parent / "pyvenv.cfg"),
        "installed": sorted(
            [
                distribution.metadata["Name"],
                distribution.version,
                hashlib.sha256((distribution.read_text("RECORD") or "").encode()).hexdigest(),
            ]
            for distribution in importlib.metadata.distributions()
        ),
        "files": {name: digest(ROOT / name) for name in FILES},
        "run_seconds": 15,
        "cleanup_seconds": 3,
        "operation_limit": OPERATION_LIMIT,
        "cumulative_read_request_bytes": DRAIN_LIMIT,
        "close_attempts": CLOSE_ATTEMPTS,
        "pcm_retained_for_arithmetic": 34560000,
        "outside_charges_optimistically_omitted": True,
        "qualified_profile": False,
    }


def report(result_path: Path, result: Result) -> None:
    with result_path.open("x", encoding="utf-8") as stream:
        json.dump(asdict(result), stream, indent=2)
        stream.write("\n")


async def execute(python: Path, result_path: Path) -> None:
    owner = FitCheck(python)
    report_failed = False
    try:
        result = await retained_wait(owner.decision)
        try:
            report(result_path, result)
        except Exception:  # noqa: BLE001 - reporting cannot release process ownership
            report_failed = True
    finally:
        # Join the independently retained retirement, even if work was cancelled
        # before starting or the report failed. Repeated caller cancellation is
        # deferred until actual release; a parked owner keeps this runner alive.
        await owner.join()
    if report_failed:
        raise RuntimeError("fixed result report failed after actual retirement")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("prepare", "execute"))
    parser.add_argument("manifest", type=Path)
    args = parser.parse_args()
    manifest = args.manifest.resolve()
    if args.mode == "prepare":
        with manifest.open("x", encoding="utf-8") as stream:
            json.dump(identity(), stream, indent=2)
            stream.write("\n")
        return
    if os.name != "nt" or json.loads(manifest.read_text(encoding="utf-8")) != identity():
        raise SystemExit("fixed check identity mismatch")
    # Exclusive creation consumes this exact case even if launch fails. The file
    # is evidence, not permission: review and root clearance remain external.
    with manifest.with_suffix(".consumed").open("x", encoding="utf-8") as stream:
        stream.write("242-worker-bootstrap-diagnostic\n")
    asyncio.run(execute(Path(sys.executable).resolve(), manifest.with_suffix(".result.json")))


if __name__ == "__main__":
    main()
