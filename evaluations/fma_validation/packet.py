"""Closed, one-shot execution contract. A template is never an execution receipt."""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from evaluations.linux_resource.probe import ProbeError
from sobafm.catalog import parse_catalog
from sobafm.fma_intake import IntakeRequest, parse_request

from .cases import NIGHT_OWL, CaseBinding, OwnedQualificationRequest, parse_owned_request
from .consumer import object_pairs
from .limits import FILE_BYTES, FILE_SHA256, REPORT_BYTES, WHOLE_SECONDS, encode_report, utc_seconds

SOURCE_FILES = (
    "src/sobafm/__init__.py",
    "src/sobafm/catalog.py",
    "src/sobafm/fma_intake.py",
    "src/sobafm/json_records.py",
    "evaluations/linux_resource/mp3_vbr_contract.py",
    "evaluations/linux_resource/probe.py",
    "evaluations/linux_resource/decoder_startup.py",
    "evaluations/linux_resource/owned_mp3_decode.py",
    "evaluations/linux_resource/owned_mp3_overlap.py",
    "evaluations/linux_resource/owned_mp3_cancel.py",
    *(
        f"evaluations/fma_validation/{name}.py"
        for name in (
            "__init__",
            "limits",
            "report_io",
            "ingress",
            "packet",
            "metadata",
            "process",
            "observation",
            "native_records",
            "diagnostics",
            "pty_channel",
            "decoder",
            "consumer",
            "owner",
            "controller",
            "cases",
            "terminal",
            "qualification_controller",
            "qualification_owner",
        )
    ),
    "evaluations/catalog/fma-night-owl-catalog.json",
    "evaluations/fma_validation/Dockerfile",
    "evaluations/fma_validation/NOTICE.md",
    "evaluations/fma_validation/qualification-recipes.json",
    "evaluations/fma_validation/COPYING",
    "pyproject.toml",
    "uv.lock",
)


def object_record(value: object, keys: set[str]) -> dict[str, object]:
    if not isinstance(value, dict) or set(cast(dict[str, object], value)) != keys:
        raise ProbeError("packet_fields")
    return cast(dict[str, object], value)


def sha(value: object) -> str:
    if not isinstance(value, str) or re.fullmatch("[0-9a-f]{64}", value) is None:
        raise ProbeError("packet_digest")
    return value


def string(value: object) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= 4096 or any(ord(c) < 32 for c in value):
        raise ProbeError("packet_string")
    return value


def integer(value: object) -> int:
    if type(value) is not int or value < 0:
        raise ProbeError("packet_integer")
    return value


def read_bounded(path: Path, limit: int = REPORT_BYTES) -> bytes:
    # Configuration/source files only; never use this for media.
    with path.open("rb") as handle:
        data = handle.read(limit + 1)
    if len(data) > limit:
        raise ProbeError("configuration_file_bound")
    return data


@dataclass(frozen=True)
class Packet:
    raw: dict[str, object]
    request: IntakeRequest | OwnedQualificationRequest
    whole_end: float
    expires: float
    source_hashes: dict[str, str]

    @property
    def case(self) -> CaseBinding:
        return (
            self.request.case if isinstance(self.request, OwnedQualificationRequest) else NIGHT_OWL
        )

    @property
    def owner_module(self) -> str:
        return "evaluations.fma_validation." + (
            "qualification_owner" if self.case.synthetic else "owner"
        )

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(encode_report(self.raw)).hexdigest()

    def text(self, key: str) -> str:
        return string(self.raw[key])

    def verify_sources(self, root: Path) -> None:
        for name, expected in self.source_hashes.items():
            if hashlib.sha256(read_bounded(root / name, 262_144)).hexdigest() != expected:
                raise ProbeError("reviewed_source_changed")


def load(path: Path, root: Path, *, now: float | None = None, recovery: bool = False) -> Packet:
    return _load(path, root, now=now, recovery=recovery, qualification=False)


def load_qualification(
    path: Path, root: Path, *, now: float | None = None, recovery: bool = False
) -> Packet:
    return _load(path, root, now=now, recovery=recovery, qualification=True)


def _load(
    path: Path, root: Path, *, now: float | None, recovery: bool, qualification: bool
) -> Packet:
    from datetime import UTC, datetime

    raw = object_record(
        json.loads(read_bounded(path), object_pairs_hook=object_pairs),
        {
            "schema",
            "case_id",
            "review",
            "source_commit",
            "source_tree",
            "source_hashes",
            "image",
            "target",
            "init_sha256",
            "python_sha256",
            "python_path",
            "whole_end_utc",
            "request",
            "volume",
            "volume_created",
            "volume_mountpoint_sha256",
            "slot_device",
            "slot_inode",
            "source_path",
            "checkout_path",
            "packet_path",
            "synthetic_preflight_sha256",
        },
    )
    now = time.time() if now is None else now
    schema = "sobafm-owned-qualification-case-1" if qualification else "sobafm-fma-case-1"
    if raw["schema"] != schema or raw["target"] != "linux/amd64":
        raise ProbeError("packet_schema_or_target")
    for key in ("source_commit", "source_tree"):
        if re.fullmatch("[0-9a-f]{40}", string(raw[key])) is None:
            raise ProbeError("packet_source_revision")
    for key in (
        "init_sha256",
        "python_sha256",
        "volume_mountpoint_sha256",
        "synthetic_preflight_sha256",
    ):
        sha(raw[key])
    if not re.fullmatch("sha256:[0-9a-f]{64}", string(raw["image"])):
        raise ProbeError("packet_image")
    if string(raw["python_path"]) not in (
        "/usr/local/bin/python",
        "/usr/local/bin/python3.14",
        "/usr/bin/python3.14",
    ):
        raise ProbeError("packet_python_path")
    if not re.fullmatch("sobafm-44-[a-z0-9]{8,32}", string(raw["case_id"])):
        raise ProbeError("packet_case_id")
    if not re.fullmatch("sobafm-44-[a-z0-9-]{8,48}", string(raw["volume"])):
        raise ProbeError("packet_volume")
    if not string(raw["review"]).startswith("https://github.com/slackysoba/sobafm/"):
        raise ProbeError("packet_review_reference")
    for key in ("source_path", "checkout_path", "packet_path"):
        value = string(raw[key])
        if not value.startswith("/") or "," in value or ".." in Path(value).parts:
            raise ProbeError("packet_absolute_linux_mount")
    string(raw["volume_created"])
    integer(raw["slot_device"])
    if integer(raw["slot_inode"]) == 0:
        raise ProbeError("packet_slot_inode")
    hashes = object_record(raw["source_hashes"], set(SOURCE_FILES))
    source_hashes = {name: sha(value) for name, value in hashes.items()}
    request = (
        parse_owned_request(raw["request"], datetime.fromtimestamp(now, UTC), recovery=recovery)
        if qualification
        else parse_request(
            encode_report(raw["request"]),
            parse_catalog(
                read_bounded(root / "evaluations/catalog/fma-night-owl-catalog.json").decode()
            ),
            datetime.fromtimestamp(now, UTC),
            recovery=recovery,
        )
    )
    if not qualification and (
        request.version != 2
        or request.file.bytes != FILE_BYTES
        or request.file.sha256 != FILE_SHA256
    ):
        raise ProbeError("packet_candidate_changed")
    end = utc_seconds(string(raw["whole_end_utc"]))
    expiry = utc_seconds(request.expires_at)
    if not recovery and not now < end <= min(now + WHOLE_SECONDS, expiry):
        raise ProbeError("packet_case_window")
    packet = Packet(raw, request, end, expiry, source_hashes)
    packet.verify_sources(root)
    return packet
