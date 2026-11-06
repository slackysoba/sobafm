"""Keep the supply-chain record's Python list equal to the locked runtime packages.

docs/supply-chain.md lists what the image contains. A lock update that doesn't
update that list fails here, without Docker or network access.
"""

import re
import tomllib
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SECTION = "## Locked Python packages"


def locked_runtime_packages(lock: dict[str, Any]) -> dict[str, str]:
    """Walk from the project through its dependencies and requested extras.

    Every edge is followed. The only markers in today's closure exclude PyPy, so
    they're always true for the CPython in the image. A later platform marker
    would make this ask for a package the image lacks, and the failure would
    show it.
    """
    packages: dict[str, dict[str, Any]] = {package["name"]: package for package in lock["package"]}
    versions: dict[str, str] = {}
    pending: list[tuple[str, tuple[str, ...]]] = [("sobafm", ())]
    visited: set[tuple[str, tuple[str, ...]]] = set()
    while pending:
        name, extras = pending.pop()
        if (name, extras) in visited:
            continue
        visited.add((name, extras))
        package = packages[name]
        versions[name] = package["version"]
        edges: list[dict[str, Any]] = list(package.get("dependencies", []))
        for extra in extras:
            edges += package["optional-dependencies"][extra]
        pending += [(edge["name"], tuple(edge.get("extra", ()))) for edge in edges]
    return versions


def recorded_packages(record: str) -> dict[str, str]:
    """Read the name and version columns of the record's Python table."""
    section = re.split(r"^#+ ", record.split(SECTION, 1)[1], maxsplit=1, flags=re.MULTILINE)[0]
    rows = [line for line in section.splitlines() if line.startswith("| ")]
    versions: dict[str, str] = {}
    for row in rows[1:]:  # The first row is the header; the separator starts "| ---".
        cells = [cell.strip() for cell in row.split("|")]
        if not cells[1].startswith("---"):
            versions[re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", cells[1])] = cells[2]
    return versions


def test_the_record_lists_exactly_the_locked_runtime_packages() -> None:
    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    record = (ROOT / "docs" / "supply-chain.md").read_text(encoding="utf-8")
    assert recorded_packages(record) == locked_runtime_packages(lock)


def test_the_build_context_and_image_carry_the_notices_file() -> None:
    notices = "THIRD-PARTY-NOTICES.txt"
    assert (ROOT / notices).is_file()
    assert f"!{notices}" in (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert f"COPY {notices} /app/{notices}" in dockerfile
