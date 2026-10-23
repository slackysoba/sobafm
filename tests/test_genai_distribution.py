"""Owned source, dependency and installed metadata checks; no external calls."""

import hashlib
import importlib.util
import io
import json
import re
import shlex
import tarfile
import tomllib
from importlib.metadata import distribution
from pathlib import Path
from types import ModuleType
from urllib.parse import unquote

import pytest
from packaging.requirements import Requirement

MATERIALS = Path(__file__).resolve().parents[1] / "third_party/google-genai"
SDK_VERSION = "2.26.0+sobafm.1"
SDK_WHEEL_URL = (
    "https://github.com/slackysoba/sobafm/releases/download/"
    "sdk-google-genai-2.26.0-sobafm.1/"
    "google_genai-2.26.0%2Bsobafm.1-py3-none-any.whl"
)
SDK_WHEEL_SHA256 = "95d2a9077f3c4730f846faa1e572fb52bf511333d4ab03bfd1f518c85af3bf03"


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


def test_reviewed_patch_and_published_dependency_are_pinned() -> None:
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
    assert dependency.url == f"{SDK_WHEEL_URL}#sha256={SDK_WHEEL_SHA256}"
    lock = tomllib.loads((MATERIALS.parents[1] / "uv.lock").read_text())
    sdk = next(package for package in lock["package"] if package["name"] == "google-genai")
    assert sdk["version"] == SDK_VERSION
    assert sdk["source"] == {"url": SDK_WHEEL_URL}
    assert sdk["wheels"] == [{"url": SDK_WHEEL_URL, "hash": f"sha256:{SDK_WHEEL_SHA256}"}]


def test_canonical_patch_reconstructs_the_exact_reviewed_patch() -> None:
    upstream = tomllib.loads((MATERIALS / "UPSTREAM.toml").read_text(encoding="utf-8"))
    canonical = (MATERIALS / upstream["patch"]).read_bytes()
    assert canonical.count(b"-\n+\n") == 6
    reviewed = canonical.replace(b"-\n+\n", b" \n")
    assert len(reviewed) == upstream["reviewed_patch_bytes"]
    assert hashlib.sha256(reviewed).hexdigest() == upstream["reviewed_patch_sha256"]
    assert len(canonical) == upstream["patch_bytes"]


def test_installed_sdk_and_project_metadata_match_the_published_dependency() -> None:
    requirements = distribution("sobafm").requires
    assert requirements is not None
    dependency = next(
        Requirement(value) for value in requirements if Requirement(value).name == "google-genai"
    )
    assert dependency.url == f"{SDK_WHEEL_URL}#sha256={SDK_WHEEL_SHA256}"
    sdk = distribution("google-genai")
    assert sdk.version == SDK_VERSION
    assert sdk.metadata["License-Expression"] == "Apache-2.0 AND MIT"
    direct = sdk.read_text("direct_url.json")
    assert direct is not None
    assert unquote(json.loads(direct)["url"]) == unquote(SDK_WHEEL_URL)


def test_upstream_advisory_input_matches_the_adopted_component() -> None:
    root = MATERIALS.parents[1]
    upstream = tomllib.loads((MATERIALS / "UPSTREAM.toml").read_text(encoding="utf-8"))
    lines = (root / ".github/genai-upstream/requirements.txt").read_text().splitlines()
    requirements = [line for line in lines if line.strip() and not line.startswith("#")]
    assert requirements == [f"{upstream['name']}=={upstream['version']}"]
    requirement = Requirement(requirements[0])
    assert requirement.name == "google-genai"
    assert requirement.url is None
    assert upstream["upstream_purl"] == f"pkg:pypi/{requirement.name}@{upstream['version']}"
    lock = tomllib.loads((root / "uv.lock").read_text())
    sdk = next(package for package in lock["package"] if package["name"] == requirement.name)
    assert sdk["version"] == upstream["derived_version"] == SDK_VERSION
    assert sdk["source"] == {"url": SDK_WHEEL_URL}
    assert sdk["wheels"] == [{"url": SDK_WHEEL_URL, "hash": f"sha256:{SDK_WHEEL_SHA256}"}]


def test_derived_license_override_supplies_only_verified_metadata() -> None:
    upstream = tomllib.loads((MATERIALS / "UPSTREAM.toml").read_text(encoding="utf-8"))
    config = tomllib.loads((MATERIALS.parents[1] / "osv-scanner.toml").read_text())
    overrides = [entry for entry in config["PackageOverrides"] if entry["name"] == "google-genai"]
    assert len(overrides) == 1
    override = overrides[0]
    assert set(override) == {"name", "version", "ecosystem", "license", "reason"}
    assert override["version"] == upstream["derived_version"] == SDK_VERSION
    assert override["ecosystem"] == "PyPI"
    licenses = [upstream["upstream_license"], upstream["additions_license"]]
    assert override["license"] == {"override": licenses}
    assert distribution("google-genai").metadata["License-Expression"] == " AND ".join(licenses)


def test_normal_security_scan_covers_lock_and_explicit_upstream_identity() -> None:
    workflow = (MATERIALS.parents[1] / ".github/workflows/security.yml").read_text()
    commands = re.findall(
        r"      - name: Scan uv.lock\n        run: >-\n((?:          .+\n)+)", workflow
    )
    assert len(commands) == 1
    tokens = shlex.split(" ".join(line.strip() for line in commands[0].splitlines()))
    assert tokens == [
        "./osv-scanner",
        "scan",
        "source",
        "--config",
        "osv-scanner.toml",
        "--lockfile",
        "uv.lock",
        "--lockfile",
        "requirements.txt:.github/genai-upstream/requirements.txt",
        "--licenses=${ALLOWED_LICENSES}",
        "--format=json",
        "--all-packages",
    ]
