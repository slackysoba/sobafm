"""Keep the supply-chain record's Python list equal to the locked runtime packages.

docs/supply-chain.md lists what the image contains. A lock update that doesn't
update that list fails here, without Docker or network access.
"""

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SECTION = "## Locked Python packages"


def locked_runtime_packages(project: Path) -> dict[str, str]:
    """Ask the project's pinned uv for edges active on the inspected image."""
    result = subprocess.run(
        [
            "uv",
            "tree",
            "--locked",
            "--offline",
            "--no-default-groups",
            "--python",
            sys.executable,
            "--python-version",
            "3.14.7",
            "--python-platform",
            "x86_64-unknown-linux-gnu",
            "--format",
            "json",
        ],
        cwd=project,
        check=True,
        stdout=subprocess.PIPE,
        text=True,
        timeout=30,
    )
    tree = json.loads(result.stdout)
    # uv also displays unrequested project extras as separate roots. Start at
    # the base project, following an extra only when a selected edge requests it.
    pending: list[str] = [
        next(member["id"] for member in tree["members"] if member["name"] == "sobafm")
    ]
    visited: set[str] = set()
    versions: dict[str, str] = {}
    while pending:
        node_id = pending.pop()
        if node_id in visited:
            continue
        visited.add(node_id)
        node = tree["resolution"][node_id]
        if node["kind"] == "package":
            versions[node["name"]] = node["version"]
        pending.extend(edge["id"] for edge in node["dependencies"])
    return versions


def recorded_packages(record: str) -> dict[str, str]:
    """Read the name and version columns of the record's Python table."""
    section = re.split(r"^#+ ", record.split(SECTION, 1)[1], maxsplit=1, flags=re.MULTILINE)[0]
    rows = [line for line in section.splitlines() if line.startswith("| ")]
    versions: dict[str, str] = {}
    for row in rows[1:]:  # The first row is the header; the separator starts "| ---".
        cells = [cell.strip() for cell in row.split("|")]
        if not cells[1].startswith("---"):
            name = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", cells[1])
            assert name not in versions, f"duplicate inventory entry: {name}"
            versions[name] = cells[2]
    return versions


def test_the_record_lists_exactly_the_locked_runtime_packages() -> None:
    record = (ROOT / "docs" / "supply-chain.md").read_text(encoding="utf-8")
    assert recorded_packages(record) == locked_runtime_packages(ROOT)


def test_voice_extra_requested_only_on_aarch64_is_excluded(tmp_path: Path) -> None:
    # Local metadata exercises real uv locking without a registry or a warm cache.
    for name in ("disnake", "voice-lib"):
        path = tmp_path / name
        path.mkdir()
        metadata = f'[project]\nname = "{name}"\nversion = "1"\n'
        if name == "disnake":
            metadata += '[project.optional-dependencies]\nvoice = ["voice-lib"]\n'
            metadata += '[tool.uv.sources]\nvoice-lib = { path = "../voice-lib" }\n'
        (path / "pyproject.toml").write_text(metadata, encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text(
        """\
[project]
name = "sobafm"
version = "1"
requires-python = ">=3.14,<3.15"
dependencies = ["disnake", "disnake[voice]; platform_machine == 'aarch64'"]
[project.optional-dependencies]
all-voice = ["disnake[voice]"]
[tool.uv.sources]
disnake = { path = "disnake" }
""",
        encoding="utf-8",
    )
    subprocess.run(
        ["uv", "lock", "--offline", "--python", sys.executable],
        cwd=tmp_path,
        check=True,
        timeout=30,
    )
    assert locked_runtime_packages(tmp_path) == {"sobafm": "1", "disnake": "1"}


def test_duplicate_row_cannot_hide_a_stale_version() -> None:
    record = f"{SECTION}\n| Package | Version |\n| sobafm | 0.0.1 |\n| sobafm | 0.0.0 |"
    with pytest.raises(AssertionError, match="duplicate inventory entry: sobafm"):
        recorded_packages(record)


def test_the_build_context_and_image_carry_the_notices_file() -> None:
    notices = "THIRD-PARTY-NOTICES.txt"
    assert (ROOT / notices).is_file()
    assert f"!{notices}" in (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert f"COPY {notices} /app/{notices}" in dockerfile
