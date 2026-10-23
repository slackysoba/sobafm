"""Build an isolated proposed SDK distribution from the exact reviewed inputs."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tomllib
import zipfile
from email.parser import BytesParser
from importlib.metadata import distributions
from pathlib import Path
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
    "prepare_buffered.py",
    "prepare_retirement.py",
    "build_retirement.ps1",
    "PUBLISHED-BUFFERED-INPUTS.toml",
    "BUFFERED-INPUT.toml",
    "buffered-input.patch",
    "NOTICE.buffered-input",
    "check_buffered_candidate.py",
    "build_buffered.ps1",
    "PUBLISHED-INPUTS.toml",
    "BUFFERED-INPUT.md",
    "check_candidate.py",
    "build-requirements.txt",
    "README.md",
    "NOTICE",
    "LICENSE",
    "LICENSE.sobafm",
)


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


_base_spec = importlib.util.spec_from_file_location(
    "sobafm_published_genai_recipe", Path(__file__).with_name("prepare.py")
)
if _base_spec is None or _base_spec.loader is None:
    raise RuntimeError("published recipe is unavailable")
_base_recipe = importlib.util.module_from_spec(_base_spec)
_base_spec.loader.exec_module(_base_recipe)
extract_source = _base_recipe.extract_source
tree_files = _base_recipe.tree_files


def prepare(
    archive: Path, output: Path, materials: Path, *, extension: Path | None = None
) -> dict[str, Any]:
    if output.exists():
        raise ValueError("output must be new; partial outputs are preserved")
    buffered_published = tomllib.loads(
        (materials / "PUBLISHED-BUFFERED-INPUTS.toml").read_text(encoding="utf-8")
    )
    for name, expected in buffered_published["sha256"].items():
        if digest((materials / name).read_bytes()) != expected:
            raise ValueError(f"published sobafm.2 material changed: {name}")
    published = tomllib.loads((materials / "PUBLISHED-INPUTS.toml").read_text(encoding="utf-8"))
    for name, expected in published["sha256"].items():
        if digest((materials / name).read_bytes()) != expected:
            raise ValueError(f"published sobafm.1 material changed: {name}")
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
    base_source_hashes = {name: after_patch[name] for name in config["sdk_paths"]}
    changed = {
        name
        for name in before.keys() | after_patch.keys()
        if before.get(name) != after_patch.get(name)
    }
    if changed != set(config["sdk_paths"]):
        raise ValueError("patch changed paths outside the reviewed SDK allowlist")
    reviewed = cast("dict[str, str]", config["reviewed_sources"])
    for name, expected in reviewed.items():
        if digest((source / name).read_bytes()) != expected:
            raise ValueError(f"patched source differs from reviewed prototype: {name}")
    material_names = MATERIAL_NAMES
    candidate_paths = list(config["sdk_paths"])
    notice_path = materials / "NOTICE"
    derived_version = DERIVED_VERSION
    extension_config: dict[str, Any] | None = None
    if extension is not None:
        extension_config = tomllib.loads(extension.read_text(encoding="utf-8"))
        if (
            extension_config["base_patch_sha256"] != config["patch_sha256"]
            or extension_config["base_derived_version"] != DERIVED_VERSION
            or extension_config["derived_version"] != "2.26.0+sobafm.3"
            or extension_config["patch"] != "connection-retirement.patch"
            or extension_config["notice"] != "NOTICE.connection-retirement"
        ):
            raise ValueError("buffered candidate base/version mismatch")
        buffered = tomllib.loads((materials / "BUFFERED-INPUT.toml").read_text(encoding="utf-8"))
        buffered_patch = materials / buffered["patch"]
        for command in (
            [*git, "apply", "--check", str(buffered_patch)],
            [*git, "apply", str(buffered_patch)],
        ):
            subprocess.run(command, cwd=source, env=apply_environment, check=True)  # noqa: S603
        for name, expected in buffered["candidate_sources"].items():
            if digest((source / name).read_bytes()) != expected:
                raise ValueError(f"published sobafm.2 source mismatch: {name}")
        extra_patch = materials / extension_config["patch"]
        if digest(extra_patch.read_bytes()) != extension_config["patch_sha256"]:
            raise ValueError("buffered patch SHA-256 mismatch")
        for command in (
            [*git, "apply", "--unidiff-zero", "--check", str(extra_patch)],
            [*git, "apply", "--unidiff-zero", str(extra_patch)],
        ):
            subprocess.run(command, cwd=source, env=apply_environment, check=True)  # noqa: S603
        after_patch = tree_files(source)
        reviewed = cast("dict[str, str]", extension_config["candidate_sources"])
        candidate_paths = list(reviewed)
        if set(candidate_paths) != set(config["sdk_paths"]) | {"google/genai/_buffered_input.py"}:
            raise ValueError("unexpected buffered SDK path set")
        for name, expected in reviewed.items():
            if after_patch.get(name) != expected:
                raise ValueError(f"buffered SDK source mismatch: {name}")
        derived_version = str(extension_config["derived_version"])
        notice_path = materials / extension_config["notice"]
        material_names += (
            extension.name,
            str(extension_config["patch"]),
            str(extension_config["notice"]),
            "check_retirement_candidate.py",
        )
    for name in reviewed:
        data = (source / name).read_bytes()
        notice = MODIFIED if name.endswith("/live_music.py") else ADDED
        (source / name).write_bytes(notice + data)
    version = source / "google/genai/version.py"
    version.write_bytes(
        MODIFIED + version.read_bytes().replace(b"'2.26.0'", repr(derived_version).encode())
    )
    project = source / "pyproject.toml"
    text = project.read_text(encoding="utf-8")
    text = text.replace('version = "2.26.0"', f'version = "{derived_version}"', 1)
    text = text.replace(
        'license = "Apache-2.0"',
        'license = "Apache-2.0 AND MIT"\nlicense-files = ["LICENSE", "LICENSE.sobafm", "NOTICE"]',
        1,
    )
    project.write_text(MODIFIED.decode() + text, encoding="utf-8", newline="\n")
    for name in ("LICENSE.sobafm", "NOTICE"):
        shutil.copyfile(notice_path if name == "NOTICE" else materials / name, source / name)
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
    for name in material_names:
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
    allowed = set(candidate_paths) | set(config["metadata_paths"])
    if any(name not in allowed and not name.startswith("sobafm-distribution/") for name in changed):
        raise ValueError("preparation changed unrelated upstream source")
    prepared_record = {
        "actual_base_source_hashes_before_extension": base_source_hashes,
        "actual_source_hashes_before_notices": {name: after_patch[name] for name in reviewed},
        "reviewed_base_source_bytes_equal": True,
        "candidate_source_bytes_equal": True,
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
    wheel = dist / f"google_genai-{derived_version}-py3-none-any.whl"
    sdist = dist / f"google_genai-{derived_version}.tar.gz"
    with zipfile.ZipFile(wheel) as package:
        metadata = BytesParser().parsebytes(
            package.read(f"google_genai-{derived_version}.dist-info/METADATA")
        )
        if (
            metadata["Version"] != derived_version
            or metadata["License-Expression"] != "Apache-2.0 AND MIT"
        ):
            raise ValueError("derived wheel metadata mismatch")
        for name in (*candidate_paths, "google/genai/version.py"):
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
                package.read(f"google_genai-{derived_version}.dist-info/licenses/{name}")
                != (source / name).read_bytes()
            ):
                raise ValueError("wheel omitted or changed license/notice")
    with tarfile.open(sdist) as package:
        for name in prepared:
            if name.startswith("google/genai/"):
                stream = package.extractfile(f"google_genai-{derived_version}/{name}")
                if stream is None or digest(stream.read()) != prepared[name]:
                    raise ValueError("sdist omitted or changed prepared SDK source")
        for name in (
            *candidate_paths,
            "google/genai/version.py",
            "pyproject.toml",
            "LICENSE",
            "LICENSE.sobafm",
            "NOTICE",
            *[f"sobafm-distribution/{item}" for item in material_names],
        ):
            stream = package.extractfile(f"google_genai-{derived_version}/{name}")
            if stream is None or stream.read() != (source / name).read_bytes():
                raise ValueError("sdist omitted or changed corresponding source/materials")
    record: dict[str, Any] = {
        "status": "unpublished-isolated-candidate",
        "actual_base_source_hashes_before_extension": base_source_hashes,
        "upstream": config,
        "extension": extension_config,
        "derived_version": derived_version,
        "python": sys.version,
        "build_inputs": {item.metadata["Name"]: item.version for item in distributions()},
        "materials": {name: digest((materials / name).read_bytes()) for name in material_names},
        "prepared_changed_paths": sorted(changed),
        "actual_source_hashes_before_notices": {name: after_patch[name] for name in reviewed},
        "reviewed_base_source_bytes_equal": True,
        "candidate_source_bytes_equal": True,
        "prepared_sdk_hashes": {name: prepared[name] for name in candidate_paths},
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
    parser.add_argument("--extension", required=True, type=Path)
    arguments = parser.parse_args()
    record = prepare(
        arguments.archive.resolve(),
        arguments.output.resolve(),
        Path(__file__).resolve().parent,
        extension=arguments.extension.resolve() if arguments.extension is not None else None,
    )
    print(json.dumps(record["artifacts"], indent=2))  # noqa: T201 - CLI artifact receipt


if __name__ == "__main__":
    main()
