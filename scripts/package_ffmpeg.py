"""Pair an inspected FFmpeg install with its exact source/build/notices materials.

This command consumes an isolated build, not a PATH executable or a binary supplier
archive. Outputs stay unqualified until the independent native supply review.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import stat
import tarfile
import zipfile
from pathlib import Path
from typing import Any, cast

from sobafm import native as native_module
from sobafm.native import (
    NativeFile,
    NativeLink,
    NativeManifest,
    SourceArchive,
    Target,
    file_sha256,
    verify_native_runtime,
)

if __package__:
    from .native_inventory import RuntimeClosure, component_inventory, write_spdx
    from .native_notices import collect_support_notices
else:
    from native_inventory import RuntimeClosure, component_inventory, write_spdx
    from native_notices import collect_support_notices

PROJECT = Path(__file__).resolve().parents[1]
EPOCH = 1789692057


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def source_record(path: Path) -> SourceArchive:
    return SourceArchive(name=path.name, sha256=file_sha256(path), size=path.stat().st_size)


def copy_material_file(source: str, destination: str) -> str:
    """Bound file-copy memory; kernel offloading hit ENOMEM in the 1 GiB package job."""
    with Path(source).open("rb") as input_file, Path(destination).open("wb") as output_file:
        shutil.copyfileobj(input_file, output_file, length=1024 * 1024)
        if input_file.tell() >= 8 * 1024 * 1024:
            output_file.flush()
            os.fsync(output_file.fileno())
    shutil.copystat(source, destination)
    return destination


def reproducible_archive(
    root: Path, destination: Path, *, executable_paths: frozenset[str] = frozenset()
) -> None:
    """Normalize archive metadata using explicit executable paths, never host modes."""
    if destination.suffix == ".zip":
        with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as output:
            for path in sorted(root.rglob("*")):
                if path.is_symlink():
                    raise ValueError("Windows runtime packages cannot contain symlinks")
                if path.is_dir():
                    continue
                if not path.is_file():
                    raise ValueError("native archive contains an unsupported file type")
                relative = path.relative_to(root).as_posix()
                info = zipfile.ZipInfo(relative, (2026, 9, 18, 0, 0, 0))
                info.create_system = 3
                mode = 0o755 if relative in executable_paths else 0o644
                info.external_attr = (stat.S_IFREG | mode) << 16
                output.writestr(info, path.read_bytes(), compress_type=zipfile.ZIP_DEFLATED)
        return
    with tarfile.open(destination, "w:xz", format=tarfile.PAX_FORMAT) as archive:
        for path in sorted(root.rglob("*")):
            relative = path.relative_to(root).as_posix()
            info = tarfile.TarInfo(relative)
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            info.mtime = EPOCH
            if path.is_symlink():
                info.type = tarfile.SYMTYPE
                info.linkname = path.readlink().as_posix()
                info.mode = 0o777
            elif path.is_dir():
                info.type = tarfile.DIRTYPE
                info.mode = 0o755
            elif path.is_file():
                info.size = path.stat().st_size
                info.mode = 0o755 if relative in executable_paths else 0o644
                with path.open("rb") as data:
                    archive.addfile(info, data)
                continue
            else:
                raise ValueError("native archive contains an unsupported file type")
            archive.addfile(info)


def stage_recipient_tools(destination: Path, project: Path, frozen_recipe: Path) -> None:
    """Deliver the explicit packaging/install import closure and existing lock context."""
    if destination.exists():
        raise ValueError("recipient tool staging requires a fresh destination")
    paths = (
        "scripts/package_ffmpeg.py",
        "scripts/native_inventory.py",
        "scripts/native_notices.py",
        "scripts/check_native_runtime.py",
        "scripts/qualify_native_runtime.py",
        "scripts/record_native_scan.py",
        "src/sobafm/__init__.py",
        "src/sobafm/native.py",
        "pyproject.toml",
        "uv.lock",
        "README.md",
        "LICENSE",
        "native/ffmpeg/component-policy.json",
        "native/ffmpeg/notice-inputs.json",
        "native/ffmpeg/replace-library.sh",
        "native/ffmpeg/qualify-ci.sh",
        "native/ffmpeg/security-review.json",
    )
    shutil.copytree(frozen_recipe, destination / "native/ffmpeg")
    for relative in paths:
        source = project / relative
        if source.is_symlink() or not source.is_file():
            raise ValueError(f"recipient tool input is absent or linked: {relative}")
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    (destination / "RECIPIENT.md").write_text(
        "# Recipient tool context\n\n"
        "Use Python 3.14 and the existing locked dependency context. From this directory:\n\n"
        "```sh\nuv sync --locked --no-dev --no-install-project\n"
        "PYTHONPATH=src uv run --no-sync python scripts/package_ffmpeg.py --help\n"
        "PYTHONPATH=src uv run --no-sync python scripts/check_native_runtime.py --help\n```\n\n"
        "On Windows set PYTHONPATH to this directory's src before those Python commands.\n"
        "The frozen native/ffmpeg recipe and the accompanying upstream materials are supplied.\n"
        "Use the top-level recipe/build.sh with the exact upstream archive mounted at /inputs,\n"
        "a fresh owned /work, writable /out, and read-only /recipe. The pinned Dockerfile and\n"
        "signed toolchain metadata describe the build inputs. Run unprivileged with network\n"
        "disabled, read-only root, dropped capabilities and no-new-privileges. /work/tmp\n"
        "supports configure probes; /tmp remains noexec. Build limits:2CPU,1GiB,128pids,\n"
        "600seconds plus15seconds termination grace. The source bundle is the input; do not\n"
        "reuse a supplier build directory. The repository docs/native-runtime.md and CI\n"
        "build-ci.sh give the complete sequence and qualification evidence requirements.\n\n"
        "Compare your prefix against the supplied installed-files/manifest using\n"
        "scripts/qualify_native_runtime.py compare. For a real ABI-compatible replacement,\n"
        "copy your successful recipient work into separate scratch, mount it as /work and\n"
        "run sh native/ffmpeg/replace-library.sh TARGET with fresh writable /out. This adds\n"
        "a version marker and increments the reported micro version. stage-replacement\n"
        "binds the changed library to the complete original sources plus exact patch;\n"
        "check-replacement proves native loading/decoding with the original executable.\n"
        "Its identity is explicitly unqualified. For production, review the new identity\n"
        "in an external operator registry and repeat the applicable execution qualification.\n",
        encoding="utf-8",
    )
    write_json(
        destination / "material-manifest.json",
        {
            "schema_version": 1,
            "dependency_context": "existing pyproject.toml and uv.lock; no new dependency",
            "files": [
                {
                    "path": path.relative_to(destination).as_posix(),
                    "size": path.stat().st_size,
                    "sha256": file_sha256(path),
                }
                for path in sorted(destination.rglob("*"))
                if path.is_file()
            ],
        },
    )


def validate_configuration(build: Path) -> list[str]:
    config = (build / "build/config.h").read_text(encoding="utf-8")
    for name in ("GPL", "NONFREE", "VERSION3", "NETWORK", "STATIC", "ICONV"):
        if f"#define CONFIG_{name} 0" not in config:
            raise ValueError(f"generated configuration did not disable {name}")
    if "#define CONFIG_SHARED 1" not in config:
        raise ValueError("generated configuration is not shared")
    components = (build / "build/config_components.h").read_text(encoding="utf-8")
    enabled = set(re.findall(r"^#define CONFIG_(\w+) 1$", components, re.MULTILINE))
    allowed = {
        "MP3FLOAT_DECODER",
        "MPEGAUDIO_PARSER",
        "MP3_DEMUXER",
        "PCM_S16LE_ENCODER",
        "PCM_S16LE_MUXER",
        "PIPE_PROTOCOL",
        "ARESAMPLE_FILTER",
        "AFORMAT_FILTER",
        "ANULL_FILTER",
        "ATRIM_FILTER",
        "CROP_FILTER",
        "FORMAT_FILTER",
        "HFLIP_FILTER",
        "NULL_FILTER",
        "ROTATE_FILTER",
        "TRANSPOSE_FILTER",
        "TRIM_FILTER",
        "VFLIP_FILTER",
    }
    if enabled != allowed:
        raise ValueError(f"unreviewed generated capabilities: {sorted(enabled ^ allowed)}")
    arguments = (
        (build / "evidence/configure-arguments.txt").read_text(encoding="utf-8").splitlines()
    )
    if not (build / "evidence/compiled-input-correspondence.tsv").stat().st_size:
        raise ValueError("actual source/header/support correspondence is missing")
    if not list((build / "evidence/links").glob("*.map")):
        raise ValueError("actual linker input/member maps are missing")
    for name in ("authenticated-toolchain.json", "input-mapping.json", "runtime-closure.json"):
        if not (build / "evidence" / name).stat().st_size:
            raise ValueError("authenticated component/runtime mapping is missing")
    if (build / "evidence/exit-code.txt").read_text(encoding="utf-8").strip() != "0":
        raise ValueError("native build did not complete successfully")
    return arguments


def package(
    build: Path, toolchain: Path, inputs: Path, output: Path, target: Target, builder_id: str
) -> Path:
    """Create compiler-free packages and bind their complete selected inputs."""
    if re.fullmatch(r"sha256:[0-9a-f]{64}", builder_id) is None:
        raise ValueError("the actual immutable builder image identity is required")
    if output.exists():
        raise ValueError("select a fresh output directory; previous evidence is preserved")
    configure = validate_configuration(build)
    frozen_recipe = build / "recipe"
    pin: dict[str, Any] = json.loads((frozen_recipe / "source.json").read_text(encoding="utf-8"))
    upstream = inputs / f"ffmpeg-{pin['version']}.tar.xz"
    if file_sha256(upstream) != pin["archive_sha256"]:
        raise ValueError("the corresponding source archive has the wrong identity")
    output.mkdir(parents=True)
    runtime = output / "runtime"
    runtime.mkdir()
    installed = build / "prefix/opt/sobafm-native"
    if target == "windows-amd64":
        (runtime / "bin").mkdir()
        for path in sorted((installed / "bin").iterdir()):
            if path.suffix in {".exe", ".dll"}:
                shutil.copy2(path, runtime / "bin" / path.name)
    else:
        shutil.copytree(installed / "bin", runtime / "bin", symlinks=True)
    if target != "windows-amd64":
        (runtime / "lib").mkdir()
        for library in sorted((installed / "lib").glob("*.so*")):
            shutil.copy2(library, runtime / "lib" / library.name, follow_symlinks=False)
    notices = runtime / "notices"
    notices.mkdir()
    ffmpeg_source = build / "source"
    ffmpeg_notices = [
        "LICENSE.md",
        "COPYING.LGPLv2.1",
        "libavcodec/jrevdct.c",
        "libavcodec/jfdctfst.c",
        "libavcodec/jfdctint.c",
        "libavcodec/jfdctint_template.c",
        "libavcodec/faandct.c",
        "libavutil/avsscanf.c",
        "libavutil/adler32.c",
    ]
    for relative in ffmpeg_notices:
        destination = notices / "ffmpeg" / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ffmpeg_source / relative, destination)
    shutil.copytree(toolchain / "notices", notices / "toolchain")
    collect_support_notices(
        PROJECT / "native/ffmpeg/notice-inputs.json", build, toolchain, notices / "extra", target
    )
    notice_inputs = build / "evidence/selected-input-notices"
    if not (notice_inputs / "index.json").is_file():
        raise ValueError("complete selected source/header notice inputs are absent")
    reproducible_archive(notice_inputs, notices / "selected-input-notices.tar.xz")
    (runtime / "NOTICE.txt").write_text(
        "SobaFM native FFmpeg runtime; application code remains MIT.\n"
        "FFmpeg uses LGPL-2.1-or-later with the component terms under notices/.\n"
        "This software is based in part on the work of the Independent JPEG Group.\n"
        "SobaFM makes no FFmpeg source changes. Upstream IJG modifications and all\n"
        "original terms are retained in the corresponding source and notices.\n"
        "OS-provided system libraries are requirements, not files in this archive;\n"
        "their OS license terms and update mechanisms apply. The machine-readable\n"
        "closure records exact imported names. Incorporated startup/header support\n"
        "has separate source, notice and component entries. Full selected source\n"
        "and header notices accompany this package in the ordinary archive\n"
        "notices/selected-input-notices.tar.xz (including its entry/hash index).\n"
        "Sources, build/install materials and library replacement: see the paired\n"
        "source bundle and source-access.json. No compiler is needed to install.\n",
        encoding="utf-8",
    )
    source_root = output / "source-materials"
    source_root.mkdir()
    shutil.copytree(frozen_recipe, source_root / "recipe")
    imported_native = native_module.__file__
    if Path(imported_native).resolve() != (PROJECT / "src/sobafm/native.py").resolve():
        raise ValueError("packager imported a native verifier outside its supplied project")
    stage_recipient_tools(source_root / "recipient-tools", PROJECT, frozen_recipe)
    shutil.copytree(
        inputs,
        source_root / "upstream",
        ignore=shutil.ignore_patterns("gnupg"),
        copy_function=copy_material_file,
    )
    shutil.copytree(
        toolchain / "sources", source_root / "toolchain-sources", copy_function=copy_material_file
    )
    shutil.copytree(
        toolchain / "apt", source_root / "authenticated-metadata", copy_function=copy_material_file
    )
    # Stable build/configuration/correspondence belongs with recipient materials.
    # Kernel/user text, logs and actual execution image IDs remain private evidence.
    build_materials = source_root / "build-materials"
    for relative in (
        "build/config.h",
        "build/config_components.h",
        "build/ffbuild/config.mak",
        "evidence/configure-arguments.txt",
        "evidence/compiled-input-correspondence.tsv",
        "evidence/selected-archive-members.tsv",
        "evidence/classified-scaffolding.tsv",
        "evidence/authenticated-toolchain.json",
        "evidence/input-mapping.json",
        "evidence/runtime-closure.json",
    ):
        destination = build_materials / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(build / relative, destination)
    shutil.copytree(build / "evidence/links", build_materials / "evidence/links")
    shutil.copytree(
        build / "evidence/archive-members", build_materials / "evidence/archive-members"
    )
    for dependency in sorted((build / "build").rglob("*.d")):
        destination = build_materials / dependency.relative_to(build)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(dependency, destination)
    provenance = output / "provenance"
    provenance.mkdir()
    shutil.copytree(build / "evidence", provenance / "build-evidence")
    write_json(
        provenance / "execution.json", {"actual_builder_image": builder_id, "target": target}
    )
    shutil.copytree(notices, source_root / "notices")
    shutil.copy2(toolchain / "packages.tsv", source_root / "toolchain-packages.tsv")
    shutil.copy2(toolchain / "covered-packages.tsv", source_root / "covered-packages.tsv")
    policy_path = source_root / "component-policy.json"
    shutil.copy2(PROJECT / "native/ffmpeg/component-policy.json", policy_path)
    shutil.copy2(
        PROJECT / "native/ffmpeg/security-review.json", source_root / "security-review.json"
    )
    write_json(
        source_root / "toolchain.json",
        {
            "target": target,
            "packages": source_record(source_root / "toolchain-packages.tsv").model_dump(),
            "covered_packages": source_record(source_root / "covered-packages.tsv").model_dump(),
            "dockerfile_sha256": file_sha256(frozen_recipe / "Dockerfile"),
        },
    )
    access = {
        "policy": (
            "Publish paired sources/notices in the same versioned GitHub release before "
            "binaries/images; retain while binaries remain obtainable."
        ),
        "publication_owner": "slackysoba/sobafm issue 132; no release is published by this build",
        "source_date_epoch": EPOCH,
        "source_access": "Direct accompanying source bundle; no written-offer substitution",
        "exact_binding": (
            "manifest.json source_bundle records the exact accompanying filename/SHA256"
        ),
    }
    write_json(runtime / "source-access.json", access)
    files: list[NativeFile] = []
    links: list[NativeLink] = []
    for path in sorted(runtime.rglob("*")):
        relative = path.relative_to(runtime).as_posix()
        if path.is_symlink():
            links.append(NativeLink(path=relative, target=path.readlink().as_posix()))
        elif path.is_file():
            files.append(
                NativeFile(
                    path=relative,
                    sha256=file_sha256(path),
                    size=path.stat().st_size,
                    binary=relative.startswith(("bin/", "lib/")),
                )
            )
    source = source_record(upstream)
    components, assessment, _ = component_inventory(
        build, toolchain, files, source, pin["version"], policy_path, target
    )
    write_json(source_root / "component-assessment.json", assessment)
    sbom = runtime / "sbom.spdx.json"
    closure = RuntimeClosure.model_validate_json(
        (build / "evidence/runtime-closure.json").read_bytes()
    )
    write_spdx(
        sbom, runtime, components, files, target, EPOCH, policy_path, closure.system_libraries
    )
    files.append(NativeFile(path=sbom.name, sha256=file_sha256(sbom), size=sbom.stat().st_size))
    shutil.copy2(sbom, source_root / sbom.name)
    write_json(
        source_root / "installed-files.json",
        {
            "files": [item.model_dump() for item in files],
            "links": [item.model_dump() for item in links],
        },
    )
    source_bundle = output / f"ffmpeg-{pin['version']}-sobafm1-{target}-sources.tar.xz"
    source_executables = frozenset(
        {
            "recipient-tools/native/ffmpeg/replace-library.sh",
            "recipient-tools/native/ffmpeg/qualify-ci.sh",
        }
    ) | frozenset(
        prefix + path.name
        for prefix in ("recipe/", "recipient-tools/native/ffmpeg/")
        for path in frozen_recipe.glob("*.sh")
    )
    reproducible_archive(source_root, source_bundle, executable_paths=source_executables)
    manifest = NativeManifest(
        schema_version=1,
        target=target,
        ffmpeg_version=pin["version"],
        upstream_commit=pin["commit"],
        source=source,
        source_bundle=source_record(source_bundle),
        recipe_sha256=file_sha256(frozen_recipe / "build.sh"),
        toolchain_sha256=file_sha256(source_root / "toolchain.json"),
        configure=configure,
        executable="bin/ffmpeg.exe" if target == "windows-amd64" else "bin/ffmpeg",
        files=files,
        links=links,
        components=components,
    )
    write_json(runtime / "manifest.json", manifest.model_dump())
    manifest_digest = file_sha256(runtime / "manifest.json")
    verify_native_runtime(runtime.resolve(), manifest_digest, target=target)
    suffix = ".zip" if target == "windows-amd64" else ".tar.xz"
    runtime_archive = output / f"ffmpeg-{pin['version']}-sobafm1-{target}{suffix}"
    reproducible_archive(
        runtime, runtime_archive, executable_paths=frozenset({manifest.executable})
    )
    index = output / "index.json"
    write_json(
        index,
        {
            "schema_version": 1,
            "target": target,
            "status": "candidate-unqualified",
            "catalog_ready": False,
            "manifest_sha256": manifest_digest,
            "runtime": source_record(runtime_archive).model_dump(),
            "sources": source_record(source_bundle).model_dump(),
        },
    )
    write_json(
        output / "footprint.json",
        {
            "schema_version": 1,
            "target": target,
            "manifest_sha256": manifest_digest,
            "units": "bytes; regular file contents, excluding directory/symlink metadata",
            "native_binary_bytes": sum(item.size for item in files if item.binary),
            "runtime_regular_file_bytes": sum(item.size for item in files)
            + (runtime / "manifest.json").stat().st_size,
            "runtime_archive_bytes": runtime_archive.stat().st_size,
            "source_archive_bytes": source_bundle.stat().st_size,
            "previous_qualified_native_build": None,
            "image_comparison": (
                "issue 132 must measure integrated platform images; native is not copied here"
            ),
        },
    )
    return index


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build", type=Path, required=True)
    parser.add_argument("--toolchain", type=Path, required=True)
    parser.add_argument("--inputs", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--target", choices=("linux-amd64", "linux-arm64", "windows-amd64"), required=True
    )
    parser.add_argument("--builder-image-id", required=True)
    args = parser.parse_args()
    print(
        package(
            args.build,
            args.toolchain,
            args.inputs,
            args.output,
            cast(Target, args.target),
            args.builder_image_id,
        )
    )


if __name__ == "__main__":
    main()
