"""Native supply checks must refuse substitution before any executable runs."""

from __future__ import annotations

import hashlib
import json
import os
import struct
from pathlib import Path
from typing import Any

import pytest

from sobafm.native import (
    NativeSupplyError,
    Target,
    native_target,
    verify_approved_native_runtime,
    verify_native_runtime,
)


def binary(target: Target) -> bytes:
    header = bytearray(128)
    if target.startswith("linux-"):
        header[:6] = b"\x7fELF\x02\x01"
        struct.pack_into("<H", header, 18, 62 if target == "linux-amd64" else 183)
    else:
        header[:2] = b"MZ"
        struct.pack_into("<I", header, 60, 64)
        header[64:70] = b"PE\x00\x00\x64\x86"
    return bytes(header)


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def save_manifest(root: Path, data: dict[str, Any]) -> str:
    content = json.dumps(data).encode()
    (root / "manifest.json").write_bytes(content)
    return digest(content)


def package(root: Path, target: Target = "linux-amd64") -> tuple[dict[str, Any], str]:
    root.mkdir()
    (root / "bin").mkdir()
    executable = "bin/ffmpeg.exe" if target == "windows-amd64" else "bin/ffmpeg"
    content = binary(target)
    (root / executable).write_bytes(content)
    (root / executable).chmod(0o755)
    (root / "NOTICE.txt").write_bytes(b"FFmpeg LGPL and component notices\n")
    source = {"name": "ffmpeg-source.tar.xz", "sha256": "a" * 64, "size": 123}
    data: dict[str, Any] = {
        "schema_version": 1,
        "target": target,
        "ffmpeg_version": "9.0.2",
        "upstream_commit": "b" * 40,
        "source": source,
        "source_bundle": {"name": "sources.tar.xz", "sha256": "c" * 64, "size": 456},
        "recipe_sha256": "d" * 64,
        "toolchain_sha256": "e" * 64,
        "configure": [
            "--disable-all",
            "--disable-autodetect",
            "--disable-network",
            "--disable-iconv",
            "--disable-gpl",
            "--disable-nonfree",
            "--disable-version3",
            "--disable-static",
            "--enable-shared",
            "--enable-decoder=mp3float",
            "--enable-parser=mpegaudio",
            "--enable-demuxer=mp3",
            "--enable-encoder=pcm_s16le",
            "--enable-muxer=pcm_s16le",
            "--enable-protocol=pipe",
        ],
        "executable": executable,
        "files": [
            {"path": executable, "sha256": digest(content), "size": len(content), "binary": True},
            {
                "path": "NOTICE.txt",
                "sha256": digest((root / "NOTICE.txt").read_bytes()),
                "size": 33,
            },
        ],
        "links": [],
        "components": [
            {
                "name": "ffmpeg",
                "version": "9.0.2",
                "license_expression": "LGPL-2.1-or-later",
                "sources": [source],
                "notices": ["NOTICE.txt"],
                "files": [executable],
                "distribution": "runtime",
            }
        ],
    }
    data["files"][1]["size"] = (root / "NOTICE.txt").stat().st_size
    return data, save_manifest(root, data)


@pytest.mark.parametrize("target", ["linux-amd64", "linux-arm64", "windows-amd64"])
def test_verified_identity_requires_every_installed_byte(tmp_path: Path, target: Target) -> None:
    root = tmp_path / "native package with spaces"
    data, expected = package(root, target)
    verified = verify_native_runtime(root, expected, target=target)
    assert verified.executable == root / data["executable"]
    assert verified.source_bundle.sha256 == "c" * 64
    # No subprocess is needed to validate headers; these fixtures cannot execute.
    (root / data["executable"]).write_bytes(binary(target) + b"tampered")
    with pytest.raises(NativeSupplyError, match="recorded digest"):
        verify_native_runtime(root, expected, target=target)


def test_manifest_cannot_self_authorize_an_arbitrary_package(tmp_path: Path) -> None:
    root = tmp_path / "runtime"
    data, expected = package(root)
    data["upstream_commit"] = "f" * 40
    save_manifest(root, data)
    with pytest.raises(NativeSupplyError, match="trusted supply identity"):
        verify_native_runtime(root, expected, target="linux-amd64")


def test_os_requirements_have_no_source_claim_or_redistributed_files(tmp_path: Path) -> None:
    root = tmp_path / "runtime"
    data, _ = package(root)
    system: dict[str, Any] = {
        "name": "system",
        "version": "OS provided",
        "license_expression": "NOASSERTION",
        "sources": [],
        "notices": ["NOTICE.txt"],
        "files": [],
        "distribution": "system",
    }
    data["components"].append(system)
    verify_native_runtime(root, save_manifest(root, data), target="linux-amd64")
    system["files"] = ["bin/ffmpeg"]
    with pytest.raises(NativeSupplyError, match="system requirements cannot own"):
        verify_native_runtime(root, save_manifest(root, data), target="linux-amd64")


@pytest.mark.skipif(os.name == "nt", reason="Windows does not implement Unix setuid/setgid bits")
def test_privilege_bits_are_rejected_even_when_native_bytes_match(tmp_path: Path) -> None:
    root = tmp_path / "runtime"
    _, expected = package(root)
    (root / "bin/ffmpeg").chmod(0o4755)
    with pytest.raises(NativeSupplyError, match="cannot confer setuid/setgid"):
        verify_native_runtime(root, expected, target="linux-amd64")


def test_approved_registry_is_separate_and_can_revoke_an_identity(tmp_path: Path) -> None:
    root = tmp_path / "runtime"
    data, expected = package(root, native_target())
    registry = tmp_path / "operator-reviewed.json"
    trust: dict[str, Any] = {
        "schema_version": 1,
        "approvals": [
            {
                "target": data["target"],
                "manifest_sha256": expected,
                "source_bundle_sha256": data["source_bundle"]["sha256"],
                "approval_record": "explicit fixture review; never executable",
            }
        ],
        "revoked_manifest_sha256": [],
    }
    registry.write_text(json.dumps(trust))
    assert verify_approved_native_runtime(root, registry=registry).manifest_sha256 == expected
    trust["revoked_manifest_sha256"] = [expected]
    registry.write_text(json.dumps(trust))
    with pytest.raises(NativeSupplyError, match="independently reviewed"):
        verify_approved_native_runtime(root, registry=registry)


def test_rehashed_local_manifest_does_not_grant_production_approval(tmp_path: Path) -> None:
    root = tmp_path / "runtime"
    data, _ = package(root, native_target())
    data["upstream_commit"] = "f" * 40
    save_manifest(root, data)
    with pytest.raises(NativeSupplyError, match="independently reviewed"):
        verify_approved_native_runtime(root)


def test_package_cannot_supply_its_own_trust_registry(tmp_path: Path) -> None:
    root = tmp_path / "runtime"
    package(root, native_target())
    registry = root / "self-approved.json"
    registry.write_text("{}")
    with pytest.raises(NativeSupplyError, match="outside the package"):
        verify_approved_native_runtime(root, registry=registry)


@pytest.mark.parametrize("target", ["linux-arm64", "windows-amd64"])
def test_rejects_other_target_even_with_matching_file_hashes(
    tmp_path: Path, target: Target
) -> None:
    root = tmp_path / "runtime"
    _, expected = package(root)
    with pytest.raises(NativeSupplyError, match="requested platform"):
        verify_native_runtime(root, expected, target=target)


def test_rejects_wrong_machine_with_accurate_manifest_hashes(tmp_path: Path) -> None:
    root = tmp_path / "runtime"
    data, _ = package(root)
    wrong = binary("linux-arm64")
    (root / "bin/ffmpeg").write_bytes(wrong)
    data["files"][0]["sha256"] = digest(wrong)
    with pytest.raises(NativeSupplyError, match="ELF architecture"):
        verify_native_runtime(root, save_manifest(root, data), target="linux-amd64")


@pytest.mark.parametrize("kind", ["missing", "extra", "notice"])
def test_rejects_incomplete_or_injected_files(tmp_path: Path, kind: str) -> None:
    root = tmp_path / "runtime"
    _, expected = package(root)
    if kind == "extra":
        (root / "bin/injected.dll").write_bytes(b"unexpected")
    elif kind == "notice":
        (root / "NOTICE.txt").unlink()
    else:
        (root / "bin/ffmpeg").unlink()
    with pytest.raises(NativeSupplyError, match="missing or unexpected"):
        verify_native_runtime(root, expected, target="linux-amd64")


@pytest.mark.parametrize(
    "flag",
    [
        "--enable-gpl",
        "--enable-nonfree",
        "--enable-version3",
        "--enable-network",
        "--enable-iconv",
        "--enable-protocol=http",
        "--enable-decoder=h264",
    ],
)
def test_rejects_wider_build_configuration(tmp_path: Path, flag: str) -> None:
    root = tmp_path / "runtime"
    data, _ = package(root)
    data["configure"].append(flag)
    with pytest.raises(NativeSupplyError, match="minimal shared route"):
        verify_native_runtime(root, save_manifest(root, data), target="linux-amd64")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("sources", []),
        ("notices", ["not-installed.txt"]),
        ("license_expression", "GPL-3.0-or-later"),
    ],
)
def test_rejects_incomplete_correspondence(tmp_path: Path, field: str, value: object) -> None:
    root = tmp_path / "runtime"
    data, _ = package(root)
    data["components"][0][field] = value
    with pytest.raises(NativeSupplyError):
        verify_native_runtime(root, save_manifest(root, data), target="linux-amd64")


@pytest.mark.parametrize(
    "path",
    [
        "../ffmpeg",
        "/ffmpeg",
        "bin/../ffmpeg",
        "C:/ffmpeg",
        "bin\\ffmpeg",
        "bin//ffmpeg",
        "bin/./ffmpeg",
    ],
)
def test_rejects_noncanonical_paths(tmp_path: Path, path: str) -> None:
    root = tmp_path / "runtime"
    data, _ = package(root)
    data["files"][0]["path"] = path
    with pytest.raises(NativeSupplyError, match="manifest is invalid"):
        verify_native_runtime(root, save_manifest(root, data), target="linux-amd64")


def test_rejects_case_collisions(tmp_path: Path) -> None:
    root = tmp_path / "runtime"
    data, _ = package(root)
    data["files"].append({**data["files"][0], "path": "BIN/FFMPEG"})
    with pytest.raises(NativeSupplyError, match="ambiguous paths"):
        verify_native_runtime(root, save_manifest(root, data), target="linux-amd64")


def test_no_path_fallback_or_untrusted_digest(tmp_path: Path) -> None:
    root = tmp_path / "runtime"
    _, expected = package(root)
    with pytest.raises(NativeSupplyError, match="absolute"):
        verify_native_runtime(Path("runtime"), expected, target="linux-amd64")
    with pytest.raises(NativeSupplyError, match="trusted native manifest"):
        verify_native_runtime(root, "", target="linux-amd64")
