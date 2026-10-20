"""Install and exercise a paired native package with owned, synthetic MP3 frames."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import shutil
import stat
import subprocess
import tarfile
import tempfile
import zipfile
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from sobafm.native import (
    SourceArchive,
    file_sha256,
    package_path,
    verify_approved_native_runtime,
    verify_native_runtime,
)


class PackageIndex(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    schema_version: int
    target: str
    status: str
    catalog_ready: bool
    manifest_sha256: str
    runtime: SourceArchive
    sources: SourceArchive


def install_package(directory: Path, destination: Path) -> str:
    """Verify the paired artifact hashes before extracting into a fresh directory."""
    index = PackageIndex.model_validate_json((directory / "index.json").read_bytes())
    for artifact in (index.runtime, index.sources):
        path = directory / artifact.name
        if path.stat().st_size != artifact.size or file_sha256(path) != artifact.sha256:
            raise ValueError("paired native artifact digest mismatch")
    if destination.exists():
        raise ValueError("installation requires a fresh destination")
    destination.mkdir(parents=True)
    path = directory / index.runtime.name
    if path.suffix == ".zip":
        with zipfile.ZipFile(path) as archive:
            for entry in archive.infolist():
                package_path(entry.orig_filename.rstrip("/"))
                package_path(entry.filename.rstrip("/"))
                mode = entry.external_attr >> 16
                if stat.S_ISLNK(mode) or (mode and not (stat.S_ISREG(mode) or stat.S_ISDIR(mode))):
                    raise ValueError("native ZIP contains a non-file entry")
                extracted = destination / entry.filename
                if entry.is_dir():
                    extracted.mkdir(parents=True, exist_ok=True)
                else:
                    extracted.parent.mkdir(parents=True, exist_ok=True)
                    with archive.open(entry) as source, extracted.open("xb") as output:
                        shutil.copyfileobj(source, output)
    else:
        with tarfile.open(path) as archive:
            for entry in archive.getmembers():
                package_path(entry.name)
                if entry.issym():
                    package_path(entry.linkname)
                elif not (entry.isfile() or entry.isdir()):
                    raise ValueError("native tar contains an unsupported file type")
            archive.extractall(destination, filter="data")
    verify_native_runtime(destination.resolve(), index.manifest_sha256)
    return index.manifest_sha256


def smoke(
    root: Path,
    expected_digest: str,
    *,
    require_no_compiler: bool = False,
    application_check: bool = False,
) -> dict[str, object]:
    """Test installation/codec capability, never production containment or budgets."""
    runtime = verify_native_runtime(root.resolve(), expected_digest)
    if require_no_compiler and any(shutil.which(name) for name in ("gcc", "cc", "cl", "make")):
        raise ValueError("compiler-free installation check found build tools on PATH")
    if os.name == "nt":
        system_root = os.environ.get("SYSTEMROOT", r"C:\Windows")
        environment = {"SystemRoot": system_root, "PATH": str(Path(system_root) / "System32")}
    else:
        environment = {"PATH": "/usr/bin:/bin", "LC_ALL": "C"}
    results: dict[str, object] = {"target": runtime.target, "manifest_sha256": expected_digest}
    for channels, header in (("mono", b"\xff\xfb\x90\xc4"), ("stereo", b"\xff\xfb\x90\x00")):
        # MPEG-1 Layer III, 128 kbit/s, 44.1 kHz. Zero side information carries
        # zero spectral coefficients; each owned frame has 1,152 silent samples.
        encoded = (header + bytes(413)) * 40
        command = [
            str(runtime.executable),
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-threads",
            "1",
            "-filter_threads",
            "1",
            "-f",
            "mp3",
            "-protocol_whitelist",
            "pipe",
            "-i",
            "pipe:0",
            "-map",
            "0:a:0",
            "-vn",
            "-sn",
            "-dn",
            "-ar",
            "48000",
            "-ac",
            "2",
            "-c:a",
            "pcm_s16le",
            "-f",
            "s16le",
            "pipe:1",
        ]
        # The input and expected output are small and fixed for this smoke only.
        # Production decoding uses the separately qualified bounded producer.
        result = subprocess.run(  # noqa: S603 - absolute verified native path
            command, input=encoded, capture_output=True, timeout=15, env=environment, check=False
        )
        if result.returncode or result.stderr or not 190_000 <= len(result.stdout) <= 210_000:
            raise ValueError("synthetic native decode failed its bounded capability check")
        if len(result.stdout) % 4 or any(result.stdout):
            raise ValueError("synthetic native decode did not emit stereo s16 silence")
        results[channels] = {
            "encoded_bytes": len(encoded),
            "pcm_bytes": len(result.stdout),
            "pcm_sha256": hashlib.sha256(result.stdout).hexdigest(),
        }
    if application_check:
        # Recipient tools deliberately retain their standalone native.py closure.
        # Only installed application/image checks opt into the production module.
        from sobafm.decoder_check import check_decoder

        # CI test trust binds this run's exact paired package; it is temporary,
        # outside the package, and never edits the shipped production registry.
        with tempfile.TemporaryDirectory(prefix="sobafm-native-ci-") as directory:
            registry = Path(directory) / "test-trust.json"
            registry.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "approvals": [
                            {
                                "target": runtime.target,
                                "manifest_sha256": runtime.manifest_sha256,
                                "source_bundle_sha256": runtime.source_bundle.sha256,
                                "approval_record": (
                                    "CI owned-fixture check only; not production approval"
                                ),
                            }
                        ],
                        "revoked_manifest_sha256": [],
                    }
                ),
                encoding="utf-8",
            )
            approved = verify_approved_native_runtime(root.resolve(), registry=registry)
            results["application_capability"] = asyncio.run(check_decoder(approved))
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="command", required=True)
    install = subcommands.add_parser("install")
    install.add_argument("directory", type=Path)
    install.add_argument("destination", type=Path)
    check = subcommands.add_parser("smoke")
    check.add_argument("root", type=Path)
    check.add_argument("manifest_sha256")
    check.add_argument("--require-no-compiler", action="store_true")
    check.add_argument("--application-check", action="store_true")
    args = parser.parse_args()
    if args.command == "install":
        print(install_package(args.directory, args.destination))
    else:
        print(
            json.dumps(
                smoke(
                    args.root,
                    args.manifest_sha256,
                    require_no_compiler=args.require_no_compiler,
                    application_check=args.application_check,
                ),
                indent=2,
                sort_keys=True,
            )
        )


if __name__ == "__main__":
    main()
