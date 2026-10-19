"""Archive installation preserves the paired source identity and directory boundary."""

from __future__ import annotations

import hashlib
import io
import json
import tarfile
import zipfile
from pathlib import Path

import pytest

from scripts.check_native_runtime import install_package
from scripts.package_ffmpeg import reproducible_archive
from sobafm.native import package_path


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
