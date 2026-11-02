"""Closed evaluation identities; unfinished derivative bindings cannot execute.

This is not a provider registry. Only Night Owl and the three reviewed #44
qualification recipes exist. No caller can supply a replacement digest/oracle.
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Literal, cast

from evaluations.linux_resource.probe import ProbeError
from sobafm.fma_intake import FileIdentity, IntakeError

from .limits import FILE_BYTES, FILE_SHA256, WHOLE_SECONDS, encode_report, utc_seconds

CaseId = Literal["night-owl", "tagged-full", "metadata-rejection", "first-write-cancel"]
CASE_IDS: tuple[CaseId, ...] = (
    "night-owl",
    "tagged-full",
    "metadata-rejection",
    "first-write-cancel",
)
BASE_BYTES = 1_920_768
# Public owned-fixture integrity digests, never credentials.
BASE_SHA256 = (
    "1a431c03098d8be79a522a3b1f64a8182445a8bf6c009f54c1b3bb1f6dad044d"  # pragma: allowlist secret
)
PCM_SHA256 = (
    "ce1390078e09a1b06bcb10d64515886ce4d865456346d7be1b70e455703062df"  # pragma: allowlist secret
)
REJECTION_SHA256 = (
    "88fc476283d8dcccc07d02fdd3a59b7d2bbb92a64d8614cc6989fa7b2de5d3f3"  # pragma: allowlist secret
)


def tag_header(size: int) -> bytes:
    return b"ID3\x03\0\0" + bytes(size >> shift & 127 for shift in (21, 14, 7, 0))


def rejection_fixture() -> bytes:
    payload = b"\x01" + b"a\x00\x00\x00" * 1023
    frame = b"TIT2" + len(payload).to_bytes(4, "big") + b"\0\0" + payload
    return tag_header(len(frame)) + frame


def tagged_prefix() -> bytes:
    """Recipe bytes only. This function never reads or derives retained media."""
    payload = b"\0" + b"a" * 4094 + b"\xe9"
    frames = b"".join(
        name + len(payload).to_bytes(4, "big") + b"\0\0" + payload for name in (b"TIT2", b"TPE1")
    )
    opaque = b"\xff" * (524_288 - 10 - len(frames) - 10)
    frames += b"APIC" + len(opaque).to_bytes(4, "big") + b"\0\0" + opaque
    return tag_header(len(frames)) + frames


def tagged_suffix() -> bytes:
    return (
        b"TAG"
        + b"synthetic\r\n\x1b\xfftitle".ljust(30, b" ")
        + b"not a trusted credit".ljust(30, b" ")
        + b"synthetic\x00album".ljust(30, b" ")
        + b"0000"
        + b"not recording correspondence".ljust(30, b" ")
        + b"\xff"
    )


@dataclass(frozen=True)
class CaseBinding:
    name: CaseId = "night-owl"

    def __post_init__(self) -> None:
        if self.name not in CASE_IDS:
            raise ProbeError("unknown_validation_case")

    @property
    def synthetic(self) -> bool:
        return self.name != "night-owl"

    @property
    def bytes(self) -> int:
        return (
            4113
            if self.name == "metadata-rejection"
            else 2_445_184
            if self.synthetic
            else FILE_BYTES
        )

    @property
    def sha256(self) -> str | None:
        if self.name == "metadata-rejection":
            return REJECTION_SHA256
        # Deliberately unbound: hashing the base cannot establish derivative identity.
        return None if self.synthetic else FILE_SHA256

    def require_frozen(self) -> None:
        if self.sha256 is None:
            raise ProbeError("qualification_derivative_unfrozen")

    def schema(self, component: str) -> str:
        namespace = "owned-qualification" if self.synthetic else "fma"
        return f"sobafm-{namespace}-{component}-1"

    def record(self) -> dict[str, object]:
        return {
            "purpose": "owned-synthetic-qualification"
            if self.synthetic
            else "night-owl-validation",
            "case": self.name,
            "recipe": "sobafm-owned-qualification-recipes-1" if self.synthetic else None,
            "input_bytes": self.bytes,
            "input_sha256": self.sha256,
        }

    @classmethod
    def from_record(cls, value: object) -> CaseBinding:
        for name in CASE_IDS:
            candidate = cls(name)
            if value == candidate.record():
                return candidate
        raise ProbeError("validation_case_binding")


NIGHT_OWL = CaseBinding()


@dataclass(frozen=True)
class OwnedQualificationRequest:
    """Second and only non-FMA caller of the shared local ownership machinery."""

    case: CaseBinding
    created_at: str
    expires_at: str
    file: FileIdentity

    @property
    def version(self) -> Literal[2]:
        return 2

    def ledger_fields(self) -> dict[str, object]:
        return {
            "schema": self.case.schema("owned-slot"),
            "case_binding": self.case.record(),
            "created_at": self.created_at,
        }

    def summary_fields(self) -> dict[str, object]:
        return {**self.ledger_fields(), "schema": self.case.schema("pending-intake")}

    def reader_fields(self) -> dict[str, object]:
        return {"case_binding": self.case.record()}

    def manifest(self) -> dict[str, object]:
        return {
            **self.ledger_fields(),
            "schema": self.case.schema("intake"),
            "expires_at": self.expires_at,
            "file": asdict(self.file),
        }

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(encode_report(self.manifest())).hexdigest()

    def validate(self, now: datetime, *, recovery: bool = False) -> None:
        self.case.require_frozen()
        start, end = utc_seconds(self.created_at), utc_seconds(self.expires_at)
        if (
            not self.case.synthetic
            or now.tzinfo is None
            or start > now.timestamp()
            or not start < end <= start + WHOLE_SECONDS
            or (not recovery and now.timestamp() >= end)
            or self.file.bytes != self.case.bytes
            or self.file.sha256 != self.case.sha256
            or any(
                type(value) is not int or value < 0
                for value in (self.file.device, self.file.mtime_ns, self.file.ctime_ns)
            )
            or type(self.file.inode) is not int
            or self.file.inode <= 0
        ):
            raise IntakeError("owned_qualification_request_binding")


def parse_owned_request(
    value: object, now: datetime, *, recovery: bool = False
) -> OwnedQualificationRequest:
    if not isinstance(value, dict):
        raise ProbeError("qualification_request_object")
    raw = cast(dict[str, object], value)
    if set(raw) != {"schema", "case_binding", "created_at", "expires_at", "file"}:
        raise ProbeError("qualification_request_fields")
    case = CaseBinding.from_record(raw["case_binding"])
    if raw["schema"] != case.schema("intake") or not case.synthetic:
        raise ProbeError("qualification_request_purpose")
    file = raw["file"]
    if not isinstance(file, dict) or set(cast(dict[str, object], file)) != {
        "device",
        "inode",
        "bytes",
        "mtime_ns",
        "ctime_ns",
        "sha256",
    }:
        raise ProbeError("qualification_file_fields")
    identity = cast(dict[str, object], file)
    if (
        any(
            type(identity[key]) is not int
            for key in ("device", "inode", "bytes", "mtime_ns", "ctime_ns")
        )
        or not isinstance(identity["sha256"], str)
        or not isinstance(raw["created_at"], str)
        or not isinstance(raw["expires_at"], str)
    ):
        raise ProbeError("qualification_request_types")
    request = OwnedQualificationRequest(
        case,
        raw["created_at"],
        raw["expires_at"],
        FileIdentity(
            cast(int, identity["device"]),
            cast(int, identity["inode"]),
            cast(int, identity["bytes"]),
            cast(int, identity["mtime_ns"]),
            cast(int, identity["ctime_ns"]),
            identity["sha256"],
        ),
    )
    request.validate(now, recovery=recovery)
    return request
