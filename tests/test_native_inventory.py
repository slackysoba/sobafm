"""Selected objects, headers and members determine actual runtime ownership."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import pytest

from scripts.native_inventory import component_inventory, write_spdx
from sobafm.native import NativeComponent, NativeFile, SourceArchive, Target, file_sha256


def write(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def inventory_fixture(tmp_path: Path) -> tuple[Path, Path, Path, list[NativeFile], SourceArchive]:
    build, toolchain = tmp_path / "build", tmp_path / "toolchain"
    evidence = build / "evidence"
    evidence.mkdir(parents=True)
    (toolchain / "sources").mkdir(parents=True)
    sources: list[dict[str, object]] = []
    for name in ("glibc", "gcc-14"):
        path = toolchain / "sources" / f"{name}.tar.xz"
        path.write_bytes(name.encode())
        sources.append(
            {
                "name": name,
                "version": "1",
                "files": [
                    {
                        "name": path.name,
                        "sha256": file_sha256(path),
                        "size": path.stat().st_size,
                    }
                ],
            }
        )
    write(
        evidence / "authenticated-toolchain.json",
        {
            "schema_version": 1,
            "trust": "fixture signed metadata",
            "keyring_sha256": "a" * 64,
            "releases": [],
            "indices": [],
            "sources": sources,
            "binaries": [
                {
                    "name": name,
                    "version": "1",
                    "archive": name + ".deb",
                    "sha256": "a" * 64,
                    "size": 1,
                }
                for name in ("libc6-dev", "gcc-14")
            ],
            "descriptor_signature_scope": "fixture",
            "snapshot_expiry": "fixture",
        },
    )
    (toolchain / "covered-packages.tsv").write_text(
        "libc6-dev\t1\tglibc\t1\ngcc-14\t1\tgcc-14\t1\n", encoding="utf-8"
    )
    selected = {
        "/work/build/decoder.o": "generated-build-input",
        "/work/source/decoder.c": "ffmpeg-source",
        "/usr/include/stdio.h": "libc6-dev",
        "/usr/lib/libgcc.a": "gcc-14",
        "/usr/lib/libgcc_s.so.1": "gcc-14",
    }
    (evidence / "compiled-input-correspondence.tsv").write_text(
        "".join("a" * 64 + f"\t{path}\t{owner}\n" for path, owner in selected.items()),
        encoding="utf-8",
    )
    write(
        evidence / "input-mapping.json",
        {
            "schema_version": 1,
            "compile_units": [
                {
                    "object": "/work/build/decoder.o",
                    "inputs": ["/work/source/decoder.c", "/usr/include/stdio.h"],
                }
            ],
            "link_units": [
                {
                    "map": "ffmpeg.map",
                    "map_sha256": "a" * 64,
                    "outputs": ["/work/build/ffmpeg"],
                    "inputs": [
                        "/work/build/decoder.o",
                        "/usr/lib/libgcc.a",
                        "/usr/lib/libgcc_s.so.1",
                    ],
                    "archive_members": [{"archive": "/usr/lib/libgcc.a", "member": "support.o"}],
                    "installed": {
                        "path": "bin/ffmpeg",
                        "linked_sha256": "a" * 64,
                        "installed_sha256": "b" * 64,
                    },
                }
            ],
        },
    )
    closure: dict[str, Any] = {
        "schema_version": 1,
        "target": "linux-amd64",
        "system_libraries": ["libc.so.6"],
        "system_distribution": "OS provided",
        "binaries": [
            {
                "path": "bin/ffmpeg",
                "sha256": "b" * 64,
                "size": 1,
                "needed": ["libc.so.6"],
                "runpath": ["$ORIGIN/../lib"],
                "glibc_symbol_versions": ["GLIBC_2.35"],
                "pe_security": {},
            }
        ],
    }
    write(evidence / "runtime-closure.json", closure)
    policy = tmp_path / "policy.json"
    write(
        policy,
        {
            "schema_version": 1,
            "status": "fixture",
            "scope": "fixture",
            "source_packages": {
                name: {"license_expression": "LGPL-2.1-or-later", "route": "fixture"}
                for name in ("glibc", "gcc-14")
            },
            "ffmpeg_terms": [],
            "license_ref_resolution": {},
            "license_ref_texts": {},
        },
    )
    files = [NativeFile(path="bin/ffmpeg", sha256="b" * 64, size=1, binary=True)]
    upstream = SourceArchive(name="ffmpeg.tar.xz", sha256="a" * 64, size=1)
    return build, toolchain, policy, files, upstream


def collect(tmp_path: Path) -> list[NativeComponent]:
    build, toolchain, policy, files, upstream = inventory_fixture(tmp_path)
    return component_inventory(build, toolchain, files, upstream, "9.0.2", policy, "linux-amd64")[0]


def test_members_and_compiled_headers_own_the_actual_installed_binary(tmp_path: Path) -> None:
    components = {item.name: item for item in collect(tmp_path)}
    assert components["source-gcc-14"].files == ["bin/ffmpeg"]
    assert components["source-glibc"].files == ["bin/ffmpeg"]
    assert components["os-system-libraries"].files == []
    assert components["os-system-libraries"].license_expression == "NOASSERTION"


def test_as_needed_shared_library_and_unselected_archive_do_not_own_bytes(tmp_path: Path) -> None:
    build, toolchain, policy, files, upstream = inventory_fixture(tmp_path)
    path = build / "evidence/input-mapping.json"
    mapping = json.loads(path.read_bytes())
    mapping["link_units"][0]["archive_members"] = []
    write(path, mapping)
    components = component_inventory(
        build, toolchain, files, upstream, "9.0.2", policy, "linux-amd64"
    )[0]
    gcc = next(item for item in components if item.name == "source-gcc-14")
    assert gcc.files == []
    assert gcc.distribution == "build-only"


@pytest.mark.parametrize("target", ["linux-amd64", "linux-arm64", "windows-amd64"])
def test_selected_arm_soft_fp_terms_do_not_change_other_target_routes(
    tmp_path: Path, target: Target
) -> None:
    build, toolchain, policy, files, upstream = inventory_fixture(tmp_path)
    base = "GPL-3.0-or-later WITH GCC-exception-3.1"
    arm = "(" + base + ") AND (LGPL-2.1-or-later WITH GCC-exception-2.0)"
    data = json.loads(policy.read_bytes())
    data["source_packages"]["gcc-14"]["license_expression"] = base
    data["target_source_packages"] = {
        "linux-arm64": {
            "gcc-14": {
                "license_expression": arm,
                "route": "Selected soft-fp support retains its distinct unlimited-link terms.",
                "required_archive_members": ["support.o"],
            }
        }
    }
    write(policy, data)
    closure = build / "evidence/runtime-closure.json"
    record = json.loads(closure.read_bytes())
    record["target"] = target
    write(closure, record)
    if target == "linux-arm64":
        files.append(
            NativeFile(path="notices/extra/gcc/libgcc/soft-fp/soft-fp.h", sha256="c" * 64, size=1)
        )
    components, assessment, _ = component_inventory(
        build, toolchain, files, upstream, "9.0.2", policy, target
    )
    gcc = next(item for item in components if item.name == "source-gcc-14")
    assert gcc.license_expression == (arm if target == "linux-arm64" else base)
    assert ("notices/extra/gcc/libgcc/soft-fp/soft-fp.h" in gcc.notices) == (
        target == "linux-arm64"
    )
    routes = cast(list[dict[str, Any]], assessment["assessments"])
    gcc_route = next(item for item in routes if item["component"] == "source-gcc-14")
    assert gcc_route["license_route_member_files"] == (
        {"support.o": ["bin/ffmpeg"]} if target == "linux-arm64" else {}
    )


def test_target_license_route_requires_its_actual_selected_members(tmp_path: Path) -> None:
    build, toolchain, policy, files, upstream = inventory_fixture(tmp_path)
    data = json.loads(policy.read_bytes())
    data["target_source_packages"] = {
        "linux-amd64": {
            "gcc-14": {
                "license_expression": "LGPL-2.1-or-later WITH GCC-exception-2.0",
                "route": "Absent contribution must not acquire a reviewed route.",
                "required_archive_members": ["absent-soft-fp.o"],
            }
        }
    }
    write(policy, data)
    with pytest.raises(ValueError, match="lacks its selected archive members"):
        component_inventory(build, toolchain, files, upstream, "9.0.2", policy, "linux-amd64")


@pytest.mark.parametrize("defect", ["missing-compile", "unknown-input", "wrong-install"])
def test_incomplete_object_graph_fails_closed(tmp_path: Path, defect: str) -> None:
    build, toolchain, policy, files, upstream = inventory_fixture(tmp_path)
    path = build / "evidence/input-mapping.json"
    mapping = json.loads(path.read_bytes())
    if defect == "missing-compile":
        mapping["compile_units"] = []
    elif defect == "unknown-input":
        mapping["compile_units"][0]["inputs"].append("/usr/include/unverified.h")
    else:
        mapping["link_units"][0]["installed"]["installed_sha256"] = "c" * 64
    write(path, mapping)
    with pytest.raises(
        ValueError, match=r"compilation input mapping|unverified|checked link output"
    ):
        component_inventory(build, toolchain, files, upstream, "9.0.2", policy, "linux-amd64")


def test_source_substitution_after_authentication_blocks_packaging(tmp_path: Path) -> None:
    build, toolchain, policy, files, upstream = inventory_fixture(tmp_path)
    (toolchain / "sources/glibc.tar.xz").write_bytes(b"different source")
    with pytest.raises(ValueError, match="source changed after authentication"):
        component_inventory(build, toolchain, files, upstream, "9.0.2", policy, "linux-amd64")


def test_custom_spdx_reference_contains_the_checked_complete_license_block(tmp_path: Path) -> None:
    build, _, policy, _, upstream = inventory_fixture(tmp_path)
    runtime = build / "runtime"
    runtime.mkdir()
    notice = runtime / "NOTICE.txt"
    notice.write_text("Complete original copyright, permission and disclaimer.\n", encoding="utf-8")
    data: dict[str, Any] = json.loads(policy.read_bytes())
    data["license_ref_texts"] = {
        "LicenseRef-exact": [
            {
                "path": "NOTICE.txt",
                "first_line": 1,
                "last_line": None,
                "sha256": file_sha256(notice),
            }
        ]
    }
    write(policy, data)
    component = NativeComponent(
        name="ffmpeg",
        version="9.0.2",
        license_expression="LicenseRef-exact",
        sources=[upstream],
        notices=["NOTICE.txt"],
        files=[],
        distribution="runtime",
    )
    destination = runtime / "sbom.spdx.json"
    write_spdx(destination, runtime, [component], [], "linux-amd64", 1, policy, [])
    sbom = json.loads(destination.read_bytes())
    assert sbom["hasExtractedLicensingInfos"][0]["extractedText"] == notice.read_bytes().decode()
    notice.write_text("incomplete replacement\n", encoding="utf-8")
    with pytest.raises(ValueError, match="differs from its reviewed notice"):
        write_spdx(
            runtime / "rejected.json", runtime, [component], [], "linux-amd64", 1, policy, []
        )
