"""Additional license notices retain authenticated archive bytes and fail closed."""

from __future__ import annotations

import bz2
import hashlib
import io
import json
import tarfile
from pathlib import Path

import pytest

from scripts.native_notices import collect_support_notices
from sobafm.native import file_sha256


@pytest.mark.parametrize("defect", [None, "member-hash", "archive-hash", "missing-member"])
def test_exact_notice_from_concatenated_bzip2_original(tmp_path: Path, defect: str | None) -> None:
    build, toolchain = tmp_path / "build", tmp_path / "toolchain"
    (build / "evidence").mkdir(parents=True)
    (toolchain / "sources").mkdir(parents=True)
    content = b"Complete copyright, permission and disclaimer.\n"
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode="w") as archive:
        padding = tarfile.TarInfo("padding")
        padding.size = 2048
        archive.addfile(padding, io.BytesIO(bytes(2048)))
        notice = tarfile.TarInfo("original/NOTICE")
        notice.size = len(content)
        archive.addfile(notice, io.BytesIO(content))
    raw = data.getvalue()
    material = toolchain / "sources/original.tar.bz2"
    material.write_bytes(bz2.compress(raw[:1024]) + bz2.compress(raw[1024:]))
    identity = {
        "name": material.name,
        "sha256": file_sha256(material),
        "size": material.stat().st_size,
    }
    authentication: dict[str, object] = {
        "schema_version": 1,
        "trust": "fixture",
        "keyring_sha256": "a" * 64,
        "releases": [],
        "indices": [],
        "binaries": [],
        "sources": [
            {
                "name": "fixture",
                "version": "1",
                "files": [identity],
            }
        ],
        "descriptor_signature_scope": "fixture",
        "snapshot_expiry": "fixture",
    }
    (build / "evidence/authenticated-toolchain.json").write_text(json.dumps(authentication))
    specification = tmp_path / "notices.json"
    record = {
        "path": "fixture/NOTICE",
        "sha256": hashlib.sha256(content).hexdigest(),
        "size": len(content),
        "targets": ["windows-amd64"],
        "archive": material.name,
        "archive_sha256": file_sha256(material),
        "member": "original/NOTICE",
    }
    if defect == "member-hash":
        record["sha256"] = "b" * 64
    elif defect == "archive-hash":
        record["archive_sha256"] = "b" * 64
    elif defect == "missing-member":
        record["member"] = "original/missing"
    specification.write_text(json.dumps({"schema_version": 1, "files": [record]}))
    output = tmp_path / "notices"
    if defect:
        with pytest.raises(ValueError, match=r"reviewed|missing"):
            collect_support_notices(specification, build, toolchain, output, "windows-amd64")
    else:
        collect_support_notices(specification, build, toolchain, output, "windows-amd64")
        assert (output / "fixture/NOTICE").read_bytes() == content
