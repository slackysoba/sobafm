"""Exercise the release workflow's command against real Buildx shapes and bad inputs."""

import json
import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import cast

import pytest

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests/fixtures/provenance"
PLATFORMS = ("linux/amd64", "linux/arm64")


@pytest.fixture
def workflow_command() -> list[str]:
    workflow = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
    commands = re.findall(
        r"^ +uv run --frozen python scripts/verify_provenance\.py .+$", workflow, re.M
    )
    assert len(commands) == 1, "The release workflow must execute the tested validator"
    tokens = shlex.split(commands[0])
    assert tokens[-1] == "$RUNNER_TEMP/provenance.json"
    assert workflow.index(commands[0]) < workflow.index("- name: Promote the verified digest")
    return [sys.executable, *tokens[4:-1]]


def inspect_fixture(version: str) -> dict[str, object]:
    return cast(
        "dict[str, object]",
        json.loads((FIXTURES / f"buildkit-{version}.json").read_text(encoding="utf-8")),
    )


def slsa_fields(document: dict[str, object], platform: str) -> dict[str, object]:
    entry = cast("dict[str, object]", document[platform])
    return cast("dict[str, object]", entry["SLSA"])


def run_check(command: list[str], tmp_path: Path, text: str) -> subprocess.CompletedProcess[str]:
    provenance = tmp_path / "provenance.json"
    provenance.write_text(text, encoding="utf-8")
    return subprocess.run(  # noqa: S603 - workflow arguments and fixture path, no shell
        [*command, str(provenance)], cwd=ROOT, capture_output=True, text=True, check=False
    )


@pytest.mark.parametrize("amd64", ["v0.2", "v1"])
@pytest.mark.parametrize("arm64", ["v0.2", "v1"])
def test_accepts_both_schema_versions_per_platform(
    workflow_command: list[str], tmp_path: Path, amd64: str, arm64: str
) -> None:
    document = {
        "linux/amd64": inspect_fixture(amd64)["linux/amd64"],
        "linux/arm64": inspect_fixture(arm64)["linux/arm64"],
    }
    result = run_check(workflow_command, tmp_path, json.dumps(document))
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("platform", PLATFORMS)
@pytest.mark.parametrize("version", ["v0.2", "v1"])
@pytest.mark.parametrize("value", [None, False, True, 0, 1, [], {}, "", " \t\n"])
def test_rejects_missing_or_malformed_build_type(
    workflow_command: list[str], tmp_path: Path, platform: str, version: str, value: object
) -> None:
    document = inspect_fixture(version)
    slsa = slsa_fields(document, platform)
    fields = cast("dict[str, object]", slsa["buildDefinition"]) if version == "v1" else slsa
    fields["buildType"] = value
    result = run_check(workflow_command, tmp_path, json.dumps(document))
    assert result.returncode != 0
    assert platform in result.stderr


@pytest.mark.parametrize("platform", PLATFORMS)
@pytest.mark.parametrize("version", ["v0.2", "v1"])
def test_rejects_absent_build_type(
    workflow_command: list[str], tmp_path: Path, platform: str, version: str
) -> None:
    document = inspect_fixture(version)
    slsa = slsa_fields(document, platform)
    fields = cast("dict[str, object]", slsa["buildDefinition"]) if version == "v1" else slsa
    del fields["buildType"]
    assert run_check(workflow_command, tmp_path, json.dumps(document)).returncode != 0


@pytest.mark.parametrize("platform", PLATFORMS)
@pytest.mark.parametrize("value", [None, False, 1, "bad", [], {}, {"buildType": ""}])
def test_malformed_v1_cannot_fall_back_to_legacy_build_type(
    workflow_command: list[str], tmp_path: Path, platform: str, value: object
) -> None:
    document = inspect_fixture("v1")
    slsa = slsa_fields(document, platform)
    slsa["buildType"] = slsa_fields(inspect_fixture("v0.2"), platform)["buildType"]
    slsa["buildDefinition"] = value
    assert run_check(workflow_command, tmp_path, json.dumps(document)).returncode != 0


@pytest.mark.parametrize("platform", PLATFORMS)
@pytest.mark.parametrize("value", [None, False, 1, "bad", [], {}])
@pytest.mark.parametrize("location", ["platform", "SLSA"])
def test_rejects_missing_or_malformed_platform_entries(
    workflow_command: list[str], tmp_path: Path, platform: str, value: object, location: str
) -> None:
    document = inspect_fixture("v1")
    if location == "platform":
        document[platform] = value
    else:
        cast("dict[str, object]", document[platform])["SLSA"] = value
    assert run_check(workflow_command, tmp_path, json.dumps(document)).returncode != 0


@pytest.mark.parametrize("platform", PLATFORMS)
def test_requires_both_platforms(
    workflow_command: list[str], tmp_path: Path, platform: str
) -> None:
    document = inspect_fixture("v1")
    del document[platform]
    assert run_check(workflow_command, tmp_path, json.dumps(document)).returncode != 0


@pytest.mark.parametrize("text", ["", "{", "null", "false", "1", '"bad"', "[]", "{}"])
def test_rejects_malformed_json_or_root(
    workflow_command: list[str], tmp_path: Path, text: str
) -> None:
    assert run_check(workflow_command, tmp_path, text).returncode != 0


def test_unreadable_inspection_fails_closed(workflow_command: list[str], tmp_path: Path) -> None:
    result = subprocess.run(  # noqa: S603 - fixed workflow command and missing fixture, no shell
        [*workflow_command, str(tmp_path / "missing.json")],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "Platform provenance validation failed" in result.stderr
