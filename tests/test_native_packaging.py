"""Archive installation preserves the paired source identity and directory boundary."""

from __future__ import annotations

import builtins
import hashlib
import importlib.util
import io
import json
import tarfile
import zipfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from scripts.check_native_runtime import install_package
from scripts.package_ffmpeg import reproducible_archive
from sobafm.native import SourceArchive, VerifiedNativeRuntime, package_path


def paired_index(directory: Path, archive: Path) -> None:
    sources = directory / "sources.tar.xz"
    sources.write_bytes(b"test source materials")

    def record(path: Path) -> dict[str, str | int]:
        return {
            "name": path.name,
            "size": path.stat().st_size,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }

    (directory / "index.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "target": "windows-amd64",
                "status": "test-fixture",
                "catalog_ready": False,
                "manifest_sha256": "a" * 64,
                "runtime": record(archive),
                "sources": record(sources),
            }
        ),
        encoding="utf-8",
    )


@pytest.mark.parametrize(
    "name",
    ["../escaped", "/absolute", "C:/drive", "bin\\outside", "NUL", "nul.txt", "file.", "file "],
)
def test_zip_rejects_paths_before_writing(tmp_path: Path, name: str) -> None:
    archive = tmp_path / "runtime.zip"
    with zipfile.ZipFile(archive, "w") as output:
        info = zipfile.ZipInfo(name)
        info.external_attr = 0o100644 << 16
        output.writestr(info, b"payload")
    if "\\" in name:
        # ZipInfo normalizes the host separator on Windows. Change both filename
        # headers to represent an archive actually supplied with a backslash.
        archive.write_bytes(
            archive.read_bytes().replace(name.replace("\\", "/").encode(), name.encode())
        )
    paired_index(tmp_path, archive)
    with pytest.raises(ValueError, match="canonical"):
        install_package(tmp_path, tmp_path / "install")
    assert not (tmp_path / "escaped").exists()


def test_tar_rejects_link_outside_install(tmp_path: Path) -> None:
    archive = tmp_path / "runtime.tar.xz"
    with tarfile.open(archive, "w:xz") as output:
        info = tarfile.TarInfo("lib/library.so")
        info.type = tarfile.SYMTYPE
        info.linkname = "../../escaped"
        output.addfile(info)
    paired_index(tmp_path, archive)
    with pytest.raises(ValueError, match="canonical"):
        install_package(tmp_path, tmp_path / "install")


def test_tar_rejects_device_file(tmp_path: Path) -> None:
    archive = tmp_path / "runtime.tar.xz"
    with tarfile.open(archive, "w:xz") as output:
        info = tarfile.TarInfo("device")
        info.type = tarfile.CHRTYPE
        output.addfile(info)
    paired_index(tmp_path, archive)
    with pytest.raises(ValueError, match="file type"):
        install_package(tmp_path, tmp_path / "install")


def test_source_bundle_mismatch_prevents_any_install(tmp_path: Path) -> None:
    archive = tmp_path / "runtime.tar.xz"
    with tarfile.open(archive, "w:xz") as output:
        data = b"runtime"
        info = tarfile.TarInfo("bin/ffmpeg")
        info.size = len(data)
        output.addfile(info, io.BytesIO(data))
    paired_index(tmp_path, archive)
    (tmp_path / "sources.tar.xz").write_bytes(b"different source")
    with pytest.raises(ValueError, match="artifact digest mismatch"):
        install_package(tmp_path, tmp_path / "install")
    assert not (tmp_path / "install").exists()


@pytest.mark.parametrize("suffix", [".tar.xz", ".zip"])
def test_packaging_is_independent_of_input_mtimes(tmp_path: Path, suffix: str) -> None:
    source = tmp_path / "staged"
    source.mkdir()
    path = source / "NOTICE.txt"
    path.write_bytes(b"unchanged\n")
    first = tmp_path / f"first{suffix}"
    second = tmp_path / f"second{suffix}"
    reproducible_archive(source, first)
    path.touch()
    reproducible_archive(source, second)
    assert first.read_bytes() == second.read_bytes()
    assert package_path("lib/libavcodec.so.63") == "lib/libavcodec.so.63"


def test_standalone_supply_tool_import_needs_no_application_decoder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_import = builtins.__import__

    def narrow_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name in {"sobafm.decoder_check", "sobafm.decoder_process", "sobafm.catalog_audio"}:
            raise ModuleNotFoundError("application modules deliberately absent")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", narrow_import)
    path = Path(__file__).parents[1] / "scripts/check_native_runtime.py"
    spec = importlib.util.spec_from_file_location("recipient_supply_check", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert callable(module.install_package)
    assert callable(module.smoke)

    def verify(*args: Any, **kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(executable=Path("never-executed"), target="windows-amd64")

    def run(*args: Any, **kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(returncode=0, stderr=b"", stdout=bytes(192000))

    monkeypatch.setattr(module, "verify_native_runtime", verify)
    monkeypatch.setattr(module.subprocess, "run", run)
    result = cast(dict[str, Any], module.smoke(Path("not-accessed"), "a" * 64))
    assert "application_capability" not in result
    assert result["mono"]["pcm_bytes"] == 192000


def test_application_ci_check_uses_ephemeral_exact_package_trust(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import scripts.check_native_runtime as supply
    import sobafm.decoder_check as checks

    runtime = VerifiedNativeRuntime(
        executable=tmp_path / "not-executed.exe",
        manifest_sha256="a" * 64,
        target="windows-amd64",
        source_bundle=SourceArchive(name="paired.zip", sha256="b" * 64, size=1),
    )
    seen: list[Path] = []

    def verify(*args: Any, **kwargs: Any) -> VerifiedNativeRuntime:
        return runtime

    def reviewed(root: Path, *, registry: Path) -> VerifiedNativeRuntime:
        seen.append(registry)
        data = json.loads(registry.read_text(encoding="utf-8"))
        assert root == tmp_path
        assert not registry.is_relative_to(root)
        assert data["approvals"][0]["manifest_sha256"] == "a" * 64
        assert data["approvals"][0]["source_bundle_sha256"] == "b" * 64
        assert data["revoked_manifest_sha256"] == []
        assert "not production approval" in data["approvals"][0]["approval_record"]
        return runtime

    def run(*args: Any, **kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(returncode=0, stdout=bytes(192000), stderr=b"")

    async def check(value: VerifiedNativeRuntime) -> dict[str, int]:
        assert value is runtime
        return {"owned": 192000}

    monkeypatch.setattr(supply, "verify_native_runtime", verify)
    monkeypatch.setattr(supply, "verify_approved_native_runtime", reviewed)
    monkeypatch.setattr(supply.subprocess, "run", run)
    monkeypatch.setattr(checks, "check_decoder", check)
    result = supply.smoke(tmp_path, "a" * 64, application_check=True)
    assert result["application_capability"] == {"owned": 192000}
    assert len(seen) == 1
    assert not seen[0].exists()  # test trust never becomes a persistent production approval
