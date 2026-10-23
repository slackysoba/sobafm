"""Build an isolated proposed SDK distribution from the exact reviewed inputs."""

from __future__ import annotations

import argparse
import hashlib
import json
import ntpath
import os
import shutil
import subprocess
import sys
import tarfile
import tomllib
import zipfile
from email.parser import BytesParser
from importlib.metadata import distributions
from pathlib import Path, PurePosixPath
from typing import Any, cast

VERSION = "2.26.0"
DERIVED_VERSION = "2.26.0+sobafm.1"
MODIFIED = b"# Modified by SobaFM, 2026; see NOTICE and sobafm-distribution/.\n"
ADDED = b"# SobaFM addition, 2026; MIT license, see LICENSE.sobafm and NOTICE.\n"
MATERIAL_NAMES = (
    "UPSTREAM.toml",
    "google-genai-2.26.0-bounded-receive.patch",
    "build.ps1",
    "prepare.py",
    "check_candidate.py",
    "build-requirements.txt",
    "README.md",
    "NOTICE",
    "LICENSE",
    "LICENSE.sobafm",
)


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def extract_source(archive: Path, destination: Path, expected_hash: str) -> Path:
    """Authenticate before extraction; reject links, special files and path escapes."""
    if digest(archive.read_bytes()) != expected_hash:
        raise ValueError("upstream archive SHA-256 mismatch")
    if destination.exists():
        raise ValueError("extraction destination already exists")
    with tarfile.open(archive, "r:gz") as bundle:
        members = bundle.getmembers()
        seen: set[str] = set()
        for member in members:
            path = PurePosixPath(member.name)
            if (
                not member.name
                or "\\" in member.name
                or ":" in member.name
                or path.is_absolute()
                or ".." in path.parts
                or not path.parts
                or path.as_posix() != member.name.rstrip("/")
                or path.parts[0] != f"google_genai-{VERSION}"
                or any(ntpath.isreserved(part) for part in path.parts)
                or not (member.isfile() or member.isdir())
                or member.name.casefold() in seen
            ):
                raise ValueError("unsafe or duplicate source archive member")
            seen.add(member.name.casefold())
        destination.mkdir(parents=True)
        bundle.extractall(destination, members=members, filter="data")
    return destination / f"google_genai-{VERSION}"


def tree_files(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): digest(path.read_bytes())
        for path in root.rglob("*")
        if path.is_file()
    }


def prepare(archive: Path, output: Path, materials: Path) -> dict[str, Any]:
    if output.exists():
        raise ValueError("output must be new; partial outputs are preserved")
    config = tomllib.loads((materials / "UPSTREAM.toml").read_text(encoding="utf-8"))
    if config["version"] != VERSION or config["derived_version"] != DERIVED_VERSION:
        raise ValueError("unexpected package identity")
    patch = materials / str(config["patch"])
    if digest(patch.read_bytes()) != config["patch_sha256"]:
        raise ValueError("reviewed patch SHA-256 mismatch")
    output.mkdir(parents=True)
    source = extract_source(archive, output / "source", str(config["source_sha256"]))
    before = tree_files(source)
    declaration = tomllib.loads((source / "pyproject.toml").read_text(encoding="utf-8"))
    expected_requirements = [
        "setuptools",
        "wheel",
        "twine>=6.1.0",
        "packaging>=24.2",
        "pkginfo>=1.12.0",
    ]
    if declaration["build-system"] != {"requires": expected_requirements}:
        raise ValueError("upstream build declaration changed; inspect before execution")
    if declaration["project"]["version"] != VERSION or (source / "setup.py").exists():
        raise ValueError("upstream build/version layout changed")
    apply_environment = os.environ.copy()
    apply_environment["GIT_CEILING_DIRECTORIES"] = str(source.parent)
    git = ["git", "-c", "core.autocrlf=false"]
    for command in ([*git, "apply", "--check", str(patch)], [*git, "apply", str(patch)]):
        # Fixed Git action against the authenticated patch in the new owned source.
        subprocess.run(command, cwd=source, env=apply_environment, check=True)  # noqa: S603
    after_patch = tree_files(source)
    changed = {
        name
        for name in before.keys() | after_patch.keys()
        if before.get(name) != after_patch.get(name)
    }
    if changed != set(config["sdk_paths"]):
        raise ValueError("patch changed paths outside the reviewed SDK allowlist")
    reviewed = cast("dict[str, str]", config["reviewed_sources"])
    for name, expected in reviewed.items():
        data = (source / name).read_bytes()
        if digest(data) != expected:
            raise ValueError(f"patched source differs from reviewed prototype: {name}")
        notice = MODIFIED if name.endswith("/live_music.py") else ADDED
        (source / name).write_bytes(notice + data)
    version = source / "google/genai/version.py"
    version.write_bytes(MODIFIED + version.read_bytes().replace(b"'2.26.0'", b"'2.26.0+sobafm.1'"))
    project = source / "pyproject.toml"
    text = project.read_text(encoding="utf-8")
    text = text.replace('version = "2.26.0"', 'version = "2.26.0+sobafm.1"', 1)
    text = text.replace(
        'license = "Apache-2.0"',
        'license = "Apache-2.0 AND MIT"\nlicense-files = ["LICENSE", "LICENSE.sobafm", "NOTICE"]',
        1,
    )
    project.write_text(MODIFIED.decode() + text, encoding="utf-8", newline="\n")
    for name in ("LICENSE.sobafm", "NOTICE"):
        shutil.copyfile(materials / name, source / name)
    if (source / "LICENSE").read_bytes() != (materials / "LICENSE").read_bytes():
        raise ValueError("upstream license changed")
    manifest = source / "MANIFEST.in"
    original_manifest = manifest.read_bytes() if manifest.exists() else b""
    manifest.write_bytes(
        MODIFIED
        + original_manifest
        + b"\ninclude LICENSE.sobafm NOTICE\nrecursive-include sobafm-distribution *\n"
    )
    bundled = source / "sobafm-distribution"
    bundled.mkdir()
    for name in MATERIAL_NAMES:
        shutil.copyfile(materials / name, bundled / name)
    prepared = tree_files(source)
    notice_prefixes: dict[str, str] = {}
    for name, expected in reviewed.items():
        notice = MODIFIED if name.endswith("/live_music.py") else ADDED
        final = (source / name).read_bytes()
        if not final.startswith(notice) or digest(final[len(notice) :]) != expected:
            raise ValueError("SDK packaging delta is not the declared notice-only prefix")
        notice_prefixes[name] = notice.decode()
    changed = {
        name for name in before.keys() | prepared.keys() if before.get(name) != prepared.get(name)
    }
    allowed = set(config["sdk_paths"]) | set(config["metadata_paths"])
    if any(name not in allowed and not name.startswith("sobafm-distribution/") for name in changed):
        raise ValueError("preparation changed unrelated upstream source")
    prepared_record = {
        "actual_source_hashes_before_notices": {name: after_patch[name] for name in reviewed},
        "reviewed_source_bytes_equal": True,
        "prepared_sdk_hashes": {name: prepared[name] for name in reviewed},
        "sdk_notice_only_prefixes": notice_prefixes,
        "prepared_changed_paths": sorted(changed),
    }
    (output / "prepared.json").write_text(
        json.dumps(prepared_record, indent=2) + "\n", encoding="utf-8"
    )
    dist = output / "dist"
    dist.mkdir()
    environment = os.environ.copy()
    environment["SOURCE_DATE_EPOCH"] = str(config["source_date_epoch"])
    command = (
        "import setuptools.build_meta as m; "
        f"m.__legacy__.build_sdist({str(dist)!r}); "
        f"m.__legacy__.build_wheel({str(dist)!r})"
    )
    with (output / "backend.log").open("x", encoding="utf-8") as log:
        subprocess.run(  # noqa: S603 - inspected backend, fixed code and owned output path
            [sys.executable, "-I", "-B", "-c", command],
            cwd=source,
            env=environment,
            stdout=log,
            stderr=subprocess.STDOUT,
            check=True,
        )
    wheel = dist / f"google_genai-{DERIVED_VERSION}-py3-none-any.whl"
    sdist = dist / f"google_genai-{DERIVED_VERSION}.tar.gz"
    with zipfile.ZipFile(wheel) as package:
        metadata = BytesParser().parsebytes(
            package.read(f"google_genai-{DERIVED_VERSION}.dist-info/METADATA")
        )
        if (
            metadata["Version"] != DERIVED_VERSION
            or metadata["License-Expression"] != "Apache-2.0 AND MIT"
        ):
            raise ValueError("derived wheel metadata mismatch")
        for name in (*config["sdk_paths"], "google/genai/version.py"):
            if package.read(name) != (source / name).read_bytes():
                raise ValueError("wheel/source SDK mismatch")
            if digest((source / name).read_bytes()) != prepared[name]:
                raise ValueError("backend changed prepared SDK source")
        for name in package.namelist():
            if (
                name.startswith("google/genai/")
                and not name.endswith("/")
                and package.read(name) != (source / name).read_bytes()
            ):
                raise ValueError("wheel contains changed or unknown SDK source")
        for name in ("LICENSE", "LICENSE.sobafm", "NOTICE"):
            if (
                package.read(f"google_genai-{DERIVED_VERSION}.dist-info/licenses/{name}")
                != (source / name).read_bytes()
            ):
                raise ValueError("wheel omitted or changed license/notice")
    with tarfile.open(sdist) as package:
        for name in prepared:
            if name.startswith("google/genai/"):
                stream = package.extractfile(f"google_genai-{DERIVED_VERSION}/{name}")
                if stream is None or digest(stream.read()) != prepared[name]:
                    raise ValueError("sdist omitted or changed prepared SDK source")
        for name in (
            *config["sdk_paths"],
            "google/genai/version.py",
            "pyproject.toml",
            "LICENSE",
            "LICENSE.sobafm",
            "NOTICE",
            *[f"sobafm-distribution/{item}" for item in MATERIAL_NAMES],
        ):
            stream = package.extractfile(f"google_genai-{DERIVED_VERSION}/{name}")
            if stream is None or stream.read() != (source / name).read_bytes():
                raise ValueError("sdist omitted or changed corresponding source/materials")
    record: dict[str, Any] = {
        "status": "unpublished-isolated-candidate",
        "upstream": config,
        "python": sys.version,
        "build_inputs": {item.metadata["Name"]: item.version for item in distributions()},
        "materials": {name: digest((materials / name).read_bytes()) for name in MATERIAL_NAMES},
        "prepared_changed_paths": sorted(changed),
        "actual_source_hashes_before_notices": {name: after_patch[name] for name in reviewed},
        "reviewed_source_bytes_equal": True,
        "prepared_sdk_hashes": {name: prepared[name] for name in config["sdk_paths"]},
        "sdk_notice_only_prefixes": notice_prefixes,
        "artifacts": {
            path.name: {"sha256": digest(path.read_bytes()), "bytes": path.stat().st_size}
            for path in (wheel, sdist)
        },
    }
    (output / "candidate.json").write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    return record


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    arguments = parser.parse_args()
    record = prepare(
        arguments.archive.resolve(), arguments.output.resolve(), Path(__file__).resolve().parent
    )
    print(json.dumps(record["artifacts"], indent=2))  # noqa: T201 - CLI artifact receipt


if __name__ == "__main__":
    main()
