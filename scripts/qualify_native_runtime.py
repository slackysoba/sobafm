"""Recipient payload comparison and an explicitly unqualified library replacement."""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import re
import shutil
import subprocess
import tarfile
import zipfile
from pathlib import Path

from sobafm.native import (
    NativeFile,
    NativeManifest,
    NativeSupplyError,
    file_sha256,
    package_path,
    verify_native_runtime,
)

if __package__:
    from .check_native_runtime import PackageIndex, smoke
    from .package_ffmpeg import source_record, write_json
else:
    from check_native_runtime import PackageIndex, smoke
    from package_ffmpeg import source_record, write_json


MARKER = "-sobafm-recipient-test"


def extract_sources(packages: Path, destination: Path) -> None:
    """Authenticate the paired source bytes and tool inventory before recipient use."""
    index = PackageIndex.model_validate_json((packages / "index.json").read_bytes())
    source = packages / index.sources.name
    if source_record(source) != index.sources or destination.exists():
        raise ValueError("recipient extraction requires exact sources and fresh scratch")
    destination.mkdir(parents=True)
    with tarfile.open(source) as archive:
        for member in archive.getmembers():
            package_path(member.name)
            if not (member.isfile() or member.isdir()):
                raise ValueError("recipient source bundle contains an unsupported entry")
        archive.extractall(destination, filter="data")
    tools = destination / "recipient-tools"
    inventory = json.loads((tools / "material-manifest.json").read_bytes())
    expected = {"material-manifest.json"}
    for entry in inventory["files"]:
        item = NativeFile.model_validate(entry)
        path = tools / item.path
        if path.stat().st_size != item.size or file_sha256(path) != item.sha256:
            raise ValueError("delivered recipient tool inventory does not match its bytes")
        expected.add(item.path)
    if expected != {
        path.relative_to(tools).as_posix() for path in tools.rglob("*") if path.is_file()
    }:
        raise ValueError("delivered recipient tools include unlisted files")


def compare_packages(original: Path, reproduced: Path) -> dict[str, object]:
    first = PackageIndex.model_validate_json((original / "index.json").read_bytes())
    second = PackageIndex.model_validate_json((reproduced / "index.json").read_bytes())
    if first != second:
        raise ValueError("independent paired package identities differ")
    for item in (first.runtime, first.sources):
        if (
            source_record(original / item.name) != item
            or source_record(reproduced / item.name) != item
        ):
            raise ValueError("independent paired package bytes differ")
    return {"result": "identical runtime and source archives", "index": first.model_dump()}


def compare_payload(manifest_path: Path, rebuilt_prefix: Path) -> dict[str, object]:
    """Compare actual independently rebuilt native bytes and loader link targets."""
    manifest = NativeManifest.model_validate_json(manifest_path.read_bytes())
    compared: list[dict[str, object]] = []
    for item in manifest.files:
        if not item.binary:
            continue
        path = rebuilt_prefix / item.path
        if path.stat().st_size != item.size or file_sha256(path) != item.sha256:
            raise ValueError(f"independent native payload differs: {item.path}")
        compared.append(item.model_dump())
    for link in manifest.links:
        if (rebuilt_prefix / link.path).readlink().as_posix() != link.target:
            raise ValueError("independent native library link differs")
    return {"result": "identical native payload", "files": compared, "links": len(manifest.links)}


def stage_replacement(
    original: Path,
    packages: Path,
    library: Path,
    patch: Path,
    changed_source: Path,
    output: Path,
) -> str:
    """Bind a changed library to accompanying sources without approving production use."""
    index = PackageIndex.model_validate_json((packages / "index.json").read_bytes())
    manifest_path = original / "manifest.json"
    manifest = NativeManifest.model_validate_json(manifest_path.read_bytes())
    verify_native_runtime(original.resolve(), index.manifest_sha256, target=manifest.target)
    original_sources = packages / index.sources.name
    if source_record(original_sources) != index.sources:
        raise ValueError("replacement qualification lacks the original paired sources")
    if (
        output.exists()
        or patch.stat().st_size > 64 * 1024
        or changed_source.stat().st_size > 64 * 1024
    ):
        raise ValueError("select fresh bounded recipient replacement inputs/output")
    candidates = [
        item
        for item in manifest.files
        if item.binary and Path(item.path).name.startswith(("libavutil.so.", "avutil-"))
    ]
    if len(candidates) != 1 or file_sha256(library) == candidates[0].sha256:
        raise ValueError("replacement must change the single actual avutil shared library")
    output.mkdir(parents=True)
    runtime = output / "runtime"
    shutil.copytree(original, runtime, symlinks=True)
    target = runtime / candidates[0].path
    shutil.copyfile(library, target)
    materials = output / "replacement-materials"
    materials.mkdir()
    shutil.copyfile(patch, materials / "recipient-change.patch")
    shutil.copyfile(changed_source, materials / "version.modified.c")
    write_json(
        materials / "base.json",
        {
            "status": "recipient qualification only; no production approval",
            "base_manifest_sha256": index.manifest_sha256,
            "base_sources": index.sources.model_dump(),
            "executable_sha256": file_sha256(runtime / manifest.executable),
            "changed_library": candidates[0].path,
            "changed_library_sha256": file_sha256(target),
            "marker": MARKER,
        },
    )
    (materials / "REPLACEMENT.md").write_text(
        "# Recipient qualification change\n\n"
        "Unpack the accompanying original source bundle and perform its independent build.\n"
        "Apply recipient-change.patch with patch -p1 in that build's FFmpeg source.\n"
        "It appends an av_version_info() marker and increments the reported micro version,\n"
        "preserving the shared-library ABI. This is a test variant, not an upstream release.\n"
        "From the configured build run make -j2 libavutil/libavutil.so.61 on Linux,\n"
        "or make -j2 libavutil/avutil-61.dll for Windows, using the original frozen wrappers.\n"
        "Strip with the same target strip tool and replace only the installed avutil library.\n"
        "The original executable must remain byte-identical. Retain the changed source,\n"
        "patch and build results. Re-run native synthetic decoding and -version to prove\n"
        "the new marker is loaded. This test identity never updates application approvals.\n",
        encoding="utf-8",
    )
    sources = output / "recipient-replacement-sources.zip"
    # The original complete source bundle is already compressed. Store it verbatim
    # alongside the exact patch and instructions with normalized ZIP metadata.
    with zipfile.ZipFile(sources, "w", compression=zipfile.ZIP_STORED) as archive:
        for path in [original_sources, *sorted(materials.iterdir())]:
            info = zipfile.ZipInfo(path.name, (2026, 9, 18, 0, 0, 0))
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            with path.open("rb") as source, archive.open(info, "w", force_zip64=True) as stream:
                shutil.copyfileobj(source, stream)
    notice = runtime / "NOTICE.txt"
    with notice.open("a", encoding="utf-8") as stream:
        stream.write(
            "\nRecipient qualification variant: av_version_info() has an ABI-compatible\n"
            "marker change. Exact changed source and patch accompany the original full\n"
            "sources in recipient-replacement-sources.zip. This variant is unapproved.\n"
        )
    sbom_path = runtime / "sbom.spdx.json"
    sbom = json.loads(sbom_path.read_bytes())
    for entry in sbom["files"]:
        path = runtime / entry["fileName"].removeprefix("./")
        entry["checksums"] = [{"algorithm": "SHA256", "checksumValue": file_sha256(path)}]
    for package in sbom["packages"]:
        if package["name"].startswith("ffmpeg"):
            package["versionInfo"] += MARKER
            package["sourceInfo"] += "; recipient-change.patch SHA256 " + file_sha256(patch)
    sbom["documentNamespace"] += "/recipient-" + file_sha256(target)
    write_json(sbom_path, sbom)
    data = manifest.model_dump()
    data["source_bundle"] = source_record(sources).model_dump()
    data["ffmpeg_version"] += MARKER
    for item in data["files"]:
        path = runtime / item["path"]
        item.update(sha256=file_sha256(path), size=path.stat().st_size)
    for component in data["components"]:
        if component["name"].startswith("ffmpeg"):
            component["sources"].append(
                source_record(materials / "recipient-change.patch").model_dump()
            )
    write_json(runtime / "manifest.json", data)
    digest = file_sha256(runtime / "manifest.json")
    verify_native_runtime(runtime.resolve(), digest, target=manifest.target)
    write_json(
        output / "replacement.json",
        {
            "schema_version": 1,
            "status": "candidate-unqualified",
            "catalog_ready": False,
            "original_manifest_sha256": index.manifest_sha256,
            "manifest_sha256": digest,
            "source_bundle": source_record(sources).model_dump(),
            "original_executable_sha256": file_sha256(original / manifest.executable),
        },
    )
    return digest


def check_replacement(root: Path, record: Path) -> dict[str, object]:
    """Execute the replaced library on its actual native platform, with no compiler."""
    evidence = json.loads(record.read_bytes())
    digest = evidence["manifest_sha256"]
    verified = verify_native_runtime(root.resolve(), digest)
    if file_sha256(verified.executable) != evidence["original_executable_sha256"]:
        raise ValueError("replacement unexpectedly changed the original executable")
    try:
        verify_native_runtime(root.resolve(), evidence["original_manifest_sha256"])
    except NativeSupplyError:
        pass
    else:
        raise ValueError("original supply identity accepted the replacement")
    result = smoke(root, digest, require_no_compiler=True)
    environment = {"PATH": "/usr/bin:/bin", "LC_ALL": "C"}
    if os.name == "nt":
        system = os.environ.get("SYSTEMROOT", r"C:\Windows")
        environment = {"SystemRoot": system, "PATH": str(Path(system) / "System32")}
    version = subprocess.run(  # noqa: S603 - absolute independently checked native executable
        [str(verified.executable), "-version"],
        capture_output=True,
        timeout=15,
        env=environment,
        check=False,
    )
    if (
        version.returncode
        or re.search(rb"libavutil\s+61\.\s*1\.\s*102\s*/\s*61\.\s*1\.\s*103", version.stdout)
        is None
    ):
        raise ValueError("native executable did not load the modified shared library")
    manifest = NativeManifest.model_validate_json((root / "manifest.json").read_bytes())
    library_path = next(
        root / item.path
        for item in manifest.files
        if item.binary and Path(item.path).name.startswith(("libavutil.so.", "avutil-"))
    )
    library = ctypes.CDLL(str(library_path.resolve()))
    library.av_version_info.restype = ctypes.c_char_p
    marker = library.av_version_info()
    if not isinstance(marker, bytes) or MARKER.encode() not in marker:
        raise ValueError("replacement library lacks the built source marker")
    result.update(marker=MARKER, executable_unchanged=True, original_identity_rejected=True)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    extract = commands.add_parser("extract-sources")
    extract.add_argument("packages", type=Path)
    extract.add_argument("destination", type=Path)
    packages = commands.add_parser("compare-packages")
    packages.add_argument("original", type=Path)
    packages.add_argument("reproduced", type=Path)
    compare = commands.add_parser("compare")
    compare.add_argument("manifest", type=Path)
    compare.add_argument("rebuilt_prefix", type=Path)
    stage = commands.add_parser("stage-replacement")
    for name in ("original", "packages", "library", "patch", "changed_source", "output"):
        stage.add_argument(name, type=Path)
    check = commands.add_parser("check-replacement")
    check.add_argument("root", type=Path)
    check.add_argument("record", type=Path)
    args = parser.parse_args()
    result: object
    if args.command == "extract-sources":
        extract_sources(args.packages, args.destination)
        result = "exact source bundle and tool inventory verified"
    elif args.command == "compare-packages":
        result = compare_packages(args.original, args.reproduced)
    elif args.command == "compare":
        result = compare_payload(args.manifest, args.rebuilt_prefix)
    elif args.command == "stage-replacement":
        result = stage_replacement(
            args.original, args.packages, args.library, args.patch, args.changed_source, args.output
        )
    else:
        result = check_replacement(args.root, args.record)
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
