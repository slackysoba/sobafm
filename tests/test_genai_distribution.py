"""Owned archive and proposal checks; no SDK build, install or external call."""

import hashlib
import importlib.util
import io
import tarfile
import tomllib
from pathlib import Path
from types import ModuleType

import pytest
from packaging.requirements import Requirement

MATERIALS = Path(__file__).resolve().parents[1] / "third_party/google-genai"


def recipe() -> ModuleType:
    spec = importlib.util.spec_from_file_location("owned_genai_recipe", MATERIALS / "prepare.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def archive_at(path: Path, name: str, *, link: bool = False) -> str:
    with tarfile.open(path, "w:gz") as archive:
        member = tarfile.TarInfo(name)
        if link:
            member.type = tarfile.SYMTYPE
            member.linkname = "../../outside"
            archive.addfile(member)
        else:
            member.size = 4
            archive.addfile(member, io.BytesIO(b"data"))
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.mark.parametrize(
    "name",
    [
        "../escape",
        "/absolute",
        "google_genai-2.26.0/../escape",
        "google_genai-2.26.0/a\\b",
        "google_genai-2.26.0/C:escape",
        "google_genai-2.26.0/./same",
        "google_genai-2.26.0/NUL",
        "google_genai-2.26.0/same.",
        "google_genai-2.26.0/name ",
    ],
)
def test_unsafe_source_paths_fail_before_any_extraction(tmp_path: Path, name: str) -> None:
    archive = tmp_path / "owned.tar.gz"
    expected = archive_at(archive, name)
    destination = tmp_path / "source"
    with pytest.raises(ValueError, match="unsafe"):
        recipe().extract_source(archive, destination, expected)
    assert not destination.exists()


def test_source_links_and_hash_mismatch_never_extract(tmp_path: Path) -> None:
    archive = tmp_path / "owned.tar.gz"
    expected = archive_at(archive, "google_genai-2.26.0/link", link=True)
    destination = tmp_path / "source"
    with pytest.raises(ValueError, match="SHA-256"):
        recipe().extract_source(archive, destination, "0" * 64)
    assert not destination.exists()
    with pytest.raises(ValueError, match="unsafe"):
        recipe().extract_source(archive, destination, expected)
    assert not destination.exists()


def test_safe_source_is_owned_and_existing_destination_is_preserved(tmp_path: Path) -> None:
    archive = tmp_path / "owned.tar.gz"
    expected = archive_at(archive, "google_genai-2.26.0/README.md")
    destination = tmp_path / "source"
    source = recipe().extract_source(archive, destination, expected)
    assert (source / "README.md").read_bytes() == b"data"
    with pytest.raises(ValueError, match="already exists"):
        recipe().extract_source(archive, destination, expected)
    assert (source / "README.md").read_bytes() == b"data"


def test_reviewed_patch_is_exact_and_active_dependency_remains_stock() -> None:
    upstream = tomllib.loads((MATERIALS / "UPSTREAM.toml").read_text(encoding="utf-8"))
    for expected in upstream["reviewed_sources"].values():
        assert len(bytes.fromhex(expected)) == 32
    assert (
        hashlib.sha256((MATERIALS / upstream["patch"]).read_bytes()).hexdigest()
        == upstream["patch_sha256"]
    )
    project = tomllib.loads((MATERIALS.parents[1] / "pyproject.toml").read_text())
    dependency = next(
        Requirement(value)
        for value in project["project"]["dependencies"]
        if Requirement(value).name == "google-genai"
    )
    assert dependency.url is None
    assert str(dependency.specifier) == ">=2.26.0"


def test_canonical_patch_reconstructs_the_exact_reviewed_patch() -> None:
    upstream = tomllib.loads((MATERIALS / "UPSTREAM.toml").read_text(encoding="utf-8"))
    canonical = (MATERIALS / upstream["patch"]).read_bytes()
    assert canonical.count(b"-\n+\n") == 6
    reviewed = canonical.replace(b"-\n+\n", b" \n")
    assert len(reviewed) == upstream["reviewed_patch_bytes"]
    assert hashlib.sha256(reviewed).hexdigest() == upstream["reviewed_patch_sha256"]
    assert len(canonical) == upstream["patch_bytes"]


def test_proposed_direct_reference_survives_standard_requirement_metadata() -> None:
    # This is an owned metadata specimen, never an active or downloadable dependency.
    url = (
        "https://example.invalid/google_genai-2.26.0%2Bsobafm.1-py3-none-any.whl#sha256=" + "a" * 64
    )
    requirement = Requirement(f"google-genai @ {url}")
    reparsed = Requirement(str(requirement))
    assert reparsed.name == "google-genai"
    assert reparsed.url == url
