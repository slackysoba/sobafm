"""Selected native inputs and recipient tool delivery fail closed."""

from __future__ import annotations

import hashlib
import io
import json
import subprocess
import tarfile
from pathlib import Path
from typing import Any

import pytest

from native.ffmpeg import correspondence
from scripts.package_ffmpeg import stage_recipient_tools


@pytest.mark.parametrize("kind", ["compiler", "linker"])
def test_unresolved_selected_input_prevents_correspondence(tmp_path: Path, kind: str) -> None:
    work, toolchain = tmp_path / "work", tmp_path / "toolchain"
    (work / "source").mkdir(parents=True)
    (work / "build").mkdir()
    (work / "evidence/links").mkdir(parents=True)
    toolchain.mkdir()
    (toolchain / "covered-packages.tsv").write_text("")
    (work / "source/input.c").write_text("int fixture;\n")
    (work / "build/object.o").write_bytes(b"synthetic object")
    (work / "build/object.d").write_text(
        "object.o: ../source/absent.c\n" if kind == "compiler" else "object.o: ../source/input.c\n"
    )
    (work / "evidence/links/ffmpeg.map").write_text(
        "LOAD absent.o\n" if kind == "linker" else "LOAD object.o\n"
    )
    with pytest.raises(ValueError, match="unresolved selected input"):
        correspondence.collect(work, toolchain, tmp_path / "not-reached.tar.xz", "linux-amd64")
    assert not (work / "evidence/compiled-input-correspondence.tsv").exists()


def test_only_explicit_virtual_scaffolding_is_classified() -> None:
    selected, members, classified = correspondence.linker_inputs(
        '/DISCARD/\nSEARCH_DIR("=/allowed/absent-search-directory");\nLOAD missing.o\n'
    )
    assert selected == {"missing.o"}
    assert not members
    assert classified == {
        ("linker-virtual-section", "/DISCARD/"),
        ("linker-search-directory", "/allowed/absent-search-directory"),
    }
    selected, _, classified = correspondence.linker_inputs("/UNREVIEWED-VIRTUAL-PATH/\n")
    assert selected == {"/UNREVIEWED-VIRTUAL-PATH/"}
    assert not classified


def test_relative_linker_object_does_not_create_an_absolute_suffix() -> None:
    selected, members, _ = correspondence.linker_inputs(
        "LOAD libavcodec/mediacodec.o\n"
        " .text 0x0000 0x10 libavcodec/mediacodec.o\n"
        "LOAD /usr/lib/start.o\n"
        " /usr/lib/support.a(member.o)\n"
    )
    assert selected == {"libavcodec/mediacodec.o", "/usr/lib/start.o", "/usr/lib/support.a"}
    assert members == {("/usr/lib/support.a", "member.o")}


def test_only_gnu_pe_fake_import_input_is_scaffolding() -> None:
    selected, _, classified = correspondence.linker_inputs("LOAD dll stuff\n")
    assert not selected
    assert classified == {("linker-generated-import-thunk", "dll stuff")}
    with pytest.raises(ValueError, match="unsupported linker LOAD"):
        correspondence.linker_inputs("LOAD unreviewed fake input\n")


def test_only_gnu_aarch64_fake_stub_input_is_scaffolding() -> None:
    selected, members, classified = correspondence.linker_inputs("LOAD linker stubs\n")
    assert not selected
    assert not members
    assert classified == {("linker-generated-branch-stub", "linker stubs")}
    with pytest.raises(ValueError, match="unsupported linker LOAD"):
        correspondence.linker_inputs("LOAD linker stubs unexpected\n")
    selected, _, classified = correspondence.linker_inputs("LOAD linker-stubs.o\n")
    assert selected == {"linker-stubs.o"}
    assert not classified


def test_source_correspondence_rejects_changed_extracted_bytes(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    selected = source / "input.c"
    selected.write_bytes(b"altered source bytes")
    archive = tmp_path / "upstream.tar"
    with tarfile.open(archive, "w") as output:
        entry = tarfile.TarInfo("ffmpeg-fixture/input.c")
        entry.size = len(b"pinned upstream bytes")
        output.addfile(entry, io.BytesIO(b"pinned upstream bytes"))
    with pytest.raises(ValueError, match="differs from"):
        correspondence.verify_upstream_inputs([selected], source, archive, "fixture")


def test_member_failure_propagates_and_preserves_partial_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive, output = tmp_path / "support.a", tmp_path / "selected.o"
    archive.write_bytes(b"synthetic archive; no native tool executes")

    def run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if command[1] == "t":
            return subprocess.CompletedProcess(command, 0, "selected.o\n", "")
        kwargs["stdout"].write(b"partial fixture bytes")
        if kwargs.get("check"):
            raise subprocess.CalledProcessError(7, command, stderr=b"fixture failure")
        return subprocess.CompletedProcess(command, 7, "", "fixture failure")

    monkeypatch.setattr(correspondence.subprocess, "run", run)
    with pytest.raises(subprocess.CalledProcessError) as failure:
        correspondence.extract_member(archive, "selected.o", output)
    assert failure.value.returncode == 7
    assert output.read_bytes() == b"partial fixture bytes"


def test_missing_member_rejected_even_if_ar_listing_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive, output = tmp_path / "support.a", tmp_path / "selected.o"
    archive.write_bytes(b"synthetic archive")
    calls: list[list[str]] = []

    def run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, "other.o\n", "")

    monkeypatch.setattr(correspondence.subprocess, "run", run)
    with pytest.raises(ValueError, match="absent or ambiguous"):
        correspondence.extract_member(archive, "selected.o", output)
    assert len(calls) == 1
    assert not output.exists()


def test_recipient_tools_manifest_covers_modules_lock_recipe_and_instructions(
    tmp_path: Path,
) -> None:
    project, recipe = tmp_path / "project", tmp_path / "recipe"
    for relative in (
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
    ):
        source = project / relative
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(f"exact fixture bytes: {relative}\n".encode())
    recipe.mkdir()
    (recipe / "correspondence.py").write_text("# frozen helper fixture\n")
    destination = tmp_path / "recipient-tools"
    stage_recipient_tools(destination, project, recipe)
    inventory = json.loads((destination / "material-manifest.json").read_text())
    actual = {
        path.relative_to(destination).as_posix()
        for path in destination.rglob("*")
        if path.is_file() and path.name != "material-manifest.json"
    }
    assert actual == {item["path"] for item in inventory["files"]}
    assert "RECIPIENT.md" in actual
    for item in inventory["files"]:
        data = (destination / item["path"]).read_bytes()
        assert len(data) == item["size"]
        assert hashlib.sha256(data).hexdigest() == item["sha256"]
