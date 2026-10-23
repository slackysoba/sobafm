"""Additive SDK material checks while the application retains the published SDK."""

import hashlib
import importlib.util
import tomllib
from pathlib import Path
from types import ModuleType

import pytest

MATERIALS = Path(__file__).resolve().parents[1] / "third_party/google-genai"


def recipe() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "owned_buffered_recipe", MATERIALS / "prepare_buffered.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_published_inputs_are_literal_and_additive_candidate_has_distinct_identity() -> None:
    published = tomllib.loads((MATERIALS / "PUBLISHED-INPUTS.toml").read_text())
    assert published["version"] == "2.26.0+sobafm.1"
    for name, expected in published["sha256"].items():
        assert hashlib.sha256((MATERIALS / name).read_bytes()).hexdigest() == expected
    base = tomllib.loads((MATERIALS / "UPSTREAM.toml").read_text())
    extension = tomllib.loads((MATERIALS / "BUFFERED-INPUT.toml").read_text())
    assert extension["base_derived_version"] == base["derived_version"]
    assert extension["base_patch_sha256"] == base["patch_sha256"]
    assert extension["derived_version"] == "2.26.0+sobafm.2"
    assert extension["derived_version"] != base["derived_version"]
    assert set(extension["candidate_sources"]) == set(base["sdk_paths"]) | {
        "google/genai/_buffered_input.py"
    }
    assert (
        hashlib.sha256((MATERIALS / extension["patch"]).read_bytes()).hexdigest()
        == extension["patch_sha256"]
    )
    name = "google/genai/json_shape_preflight.py"
    assert extension["candidate_sources"][name] == base["reviewed_sources"][name]


def test_candidate_refuses_existing_output_without_touching_it(tmp_path: Path) -> None:
    output = tmp_path / "existing"
    output.mkdir()
    marker = output / "keep.txt"
    marker.write_text("preserved")
    with pytest.raises(ValueError, match="must be new"):
        recipe().prepare(tmp_path / "absent.tar.gz", output, MATERIALS)
    assert marker.read_text() == "preserved"


def test_candidate_rejects_changed_published_material_before_archive_or_backend(
    tmp_path: Path,
) -> None:
    materials = tmp_path / "materials"
    materials.mkdir()
    (materials / "PUBLISHED-INPUTS.toml").write_text('[sha256]\n"NOTICE" = "' + "0" * 64 + '"\n')
    (materials / "NOTICE").write_text("changed")
    output = tmp_path / "not-created"
    with pytest.raises(ValueError, match=r"published sobafm\.1 material changed"):
        recipe().prepare(tmp_path / "absent.tar.gz", output, materials)
    assert not output.exists()
