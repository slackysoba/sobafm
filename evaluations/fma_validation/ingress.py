"""Finite FMA wire families, checked before the maintained JSON decoder.

This is channel admission, not an operator JSON loader. Raw bytes remain owned
and hashed in their original encoding; completed records are materialized once.
The schema table describes only the producers used by this evaluation.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import traceback
from dataclasses import dataclass, field
from typing import cast

from evaluations.linux_resource.probe import ProbeError

from .limits import REPORT_BYTES


@dataclass(frozen=True)
class Wire:
    kind: str
    fields: dict[str, Wire] = field(default_factory=lambda: dict[str, Wire]())
    optional: frozenset[str] = frozenset()
    item: Wire | None = None
    count: int = 0
    domain: str = "ascii"
    maximum: int | float = 2**64 - 1
    minimum: int = 0
    nullable: bool = False


def obj(fields: dict[str, Wire], *optional: str) -> Wire:
    return Wire("object", fields=fields, optional=frozenset(optional))


def array(item: Wire, count: int) -> Wire:
    return Wire("array", item=item, count=count)


def text(count: int, domain: str = "ascii", *, nullable: bool = False) -> Wire:
    return Wire("string", count=count, domain=domain, nullable=nullable)


BOOL = Wire("bool")
UINT = Wire("int")
NUMBER = Wire("number", maximum=math.inf, minimum=0)
OPTIONAL_NUMBER = Wire("number", maximum=math.inf, nullable=True)
CODE = text(80, "code", nullable=True)
HASH = text(64, "hex")
OPTIONAL_HASH = text(64, "hex", nullable=True)
PATH = text(4096, "path")
TIMESTAMP = text(REPORT_BYTES, "timestamp")
FD = Wire("int", maximum=2**31 - 1)
EXIT = Wire("int", minimum=-128, maximum=255, nullable=True)
CASE = obj(
    {
        "purpose": text(40),
        "case": text(40),
        "recipe": text(64, nullable=True),
        "input_bytes": UINT,
        "input_sha256": HASH,
    }
)
PIN = obj(
    {
        "pid": FD,
        "start": UINT,
        "parent": FD,
        "executable": text(64),
        "executable_device": UINT,
        "executable_inode": UINT,
        "uid": FD,
        "group": text(1),
    }
)
CLOCK = obj(
    {
        "whole_end_utc": NUMBER,
        "expires_utc": NUMBER,
        "cleanup_start_utc": OPTIONAL_NUMBER,
        "cleanup_deadline_met": BOOL,
        "cleanup_timing_unknown": BOOL,
        "case_deadline_met": BOOL,
    }
)
COUNTERS = Wire("counters", item=Wire("int", maximum=2**63), count=32)
FRAME_ID = text(4, "frame")
NOTICE = Wire("notice", count=4096, domain="latin1")
FRAME = obj(
    {
        "id": FRAME_ID,
        "key": FRAME_ID,
        "fields": obj(
            {
                name: NOTICE
                for name in (
                    "text",
                    "desc",
                    "lang",
                    "url",
                    "people",
                    "owner",
                    "email",
                    "price",
                    "date",
                    "contact",
                    "seller",
                )
            },
            "text",
            "desc",
            "lang",
            "url",
            "people",
            "owner",
            "email",
            "price",
            "date",
            "contact",
            "seller",
        ),
    }
)
FRAMING = obj(
    {
        "v2_present": BOOL,
        "frame_ids": array(FRAME_ID, 512),
        "duplicate_frame_ids": array(FRAME_ID, 512),
        "v2_version": array(UINT, 3),
        "tag_bytes": Wire("int", maximum=1_048_576),
        "decoded_source_bytes": Wire("int", maximum=65_536),
    },
    "v2_version",
    "tag_bytes",
    "decoded_source_bytes",
)
METADATA_FIELDS = {
    "schema": text(64),
    "parser": text(32),
    "framing": FRAMING,
    "id3v2": array(FRAME, 512),
    "id3v1": array(FRAME, 512),
    "id3v1_version": text(3, nullable=True),
    "id3v1_sha256": OPTIONAL_HASH,
    "conflicting_frame_ids": array(FRAME_ID, 512),
    "omitted_frame_ids": array(FRAME_ID, 512),
    "unknown_frames": array(obj({"id": FRAME_ID, "bytes": UINT, "sha256": HASH}), 512),
    "read_bytes": Wire("int", maximum=24_163_374),
    "seek_count": Wire("int", maximum=512),
    "credits_complete": BOOL,
    "recording_correspondence": text(32),
    "links_followed": BOOL,
}
METADATA = obj(METADATA_FIELDS)
REJECTION = obj({"schema": text(64), "reason": text(80, "code"), "credits_complete": BOOL})
METADATA_RESULT = obj(
    {**METADATA_FIELDS, "reason": text(80, "code")},
    *(key for key in METADATA_FIELDS if key not in ("schema", "credits_complete")),
    "reason",
)
DIAGNOSTICS = obj(
    {
        "bytes": Wire("int", maximum=REPORT_BYTES),
        "sha256": HASH,
        "format": text(34),
        "diagnostic_bytes": Wire("int", maximum=REPORT_BYTES),
        "diagnostic_sha256": HASH,
        "tag_block_sha256": OPTIONAL_HASH,
        "terminal_source_filter_required": BOOL,
    },
    "bytes",
    "sha256",
    "format",
    "diagnostic_bytes",
    "diagnostic_sha256",
    "tag_block_sha256",
    "terminal_source_filter_required",
)
DECODE = obj(
    {
        "event": text(32),
        "schema": text(64),
        "case_binding": CASE,
        "runner": PIN,
        "passed": BOOL,
        "failure": CODE,
        "input_bytes": Wire("int", maximum=8_119_994),
        "input_sha256": HASH,
        "input_complete": BOOL,
        "stdout_eof": BOOL,
        "stderr_eof": BOOL,
        "exit_code": EXIT,
        "pcm_bytes": Wire("int", maximum=46_145_536),
        "pcm_sha256": HASH,
        "measured_duration_seconds": NUMBER,
        "web_duration_seconds": OPTIONAL_NUMBER,
        "ready_seconds": OPTIONAL_NUMBER,
        "cleanup": CLOCK,
        "cleanup_debt": BOOL,
        "memory_event_delta": COUNTERS,
        "swap_current": text(20, nullable=True),
        "diagnostics": DIAGNOSTICS,
        "catalog_admitted": BOOL,
        "listening": BOOL,
    }
)
MAP = obj({"path": text(256), "permissions": text(4), "bytes": UINT})
GATE = obj(
    {
        "pid": FD,
        "start_ticks": UINT,
        "state": text(16),
        "rlimits": obj(
            {
                name: UINT
                for name in (
                    "Max data size",
                    "Max stack size",
                    "Max address space",
                    "Max core file size",
                )
            }
        ),
        "mappings": obj(
            {
                "records": array(MAP, 64),
                "ordinary_bytes_observed": UINT,
                "special_bytes_observed": UINT,
                "maps_sha256": HASH,
                "special_scope": text(64),
                "sampled_pcm_proof": BOOL,
            }
        ),
        "native_address_spaces": UINT,
        "input_bytes": UINT,
    }
)
ADMISSION = obj(
    {
        "event": text(32),
        "case_binding": CASE,
        "native_gate": GATE,
        "init": PIN,
        "owner": PIN,
        "runner": PIN,
    }
)
HELPER = obj(
    {
        "role": text(16),
        "stdout_bytes": Wire("int", maximum=REPORT_BYTES),
        "stderr_bytes": Wire("int", maximum=REPORT_BYTES),
        "process": Wire("pin_or_null"),
        "exit_code": EXIT,
        "launching": BOOL,
        "retained_fds": array(FD, 7),
        "in_flight_fds": array(FD, 7),
        "uncertain_closes": array(FD, 7),
        "stdout_eof": BOOL,
        "stderr_eof": BOOL,
        "stdout_sha256": HASH,
        "stderr_sha256": HASH,
        "failure": CODE,
    }
)
READER = obj(
    {
        "case_binding": CASE,
        "request_sha256": HASH,
        "expected_bytes": UINT,
        "expected_sha256": HASH,
        "payload_identity": array(UINT, 2),
        "read_bytes": UINT,
        "delivered_bytes": UINT,
        "read_sha256": OPTIONAL_HASH,
        "stream_verified": BOOL,
        "completed": BOOL,
        "failure": CODE,
        "in_flight": CODE,
        "descriptor_retained": BOOL,
        "consumer_released": BOOL,
        "settled": BOOL,
        "reader_alive": BOOL,
        "cancel_alive": BOOL,
    },
    "case_binding",
)
INTAKE = obj(
    {
        "schema": text(64),
        "case_binding": CASE,
        "created_at": TIMESTAMP,
        "source_url": text(4096),
        "metadata_digest": HASH,
        "credit_snapshot": text(4096),
        "acquired_at": text(40),
        "acquisition": obj(
            {
                "schema": text(32),
                "retention_anchor_utc": text(40),
                "basis": text(32),
                "operator_evidence_sha256": HASH,
            }
        ),
        "notices_count": UINT,
        "notices_sha256": HASH,
        "owner_id": text(32, "hex"),
        "phase": text(40, "code"),
        "failure": CODE,
        "request_sha256": HASH,
        "expires_at": TIMESTAMP,
        "expected_bytes": UINT,
        "copied_bytes": UINT,
        "copied_sha256": OPTIONAL_HASH,
        "payload_retained": BOOL,
        "original_io_in_flight": CODE,
        "retained_handles": array(text(32, "code"), 16),
        "worker_alive": BOOL,
        "cleanup_deadline": OPTIONAL_NUMBER,
        "cleanup_deadline_known": BOOL,
        "pending_valid": BOOL,
        "unresolved_previous_slot": BOOL,
        "cleanup_deadline_met": BOOL,
        "released_at": text(40, nullable=True),
        "released": BOOL,
        "cleanup_debt": BOOL,
        "slot_reusable": BOOL,
        "operator_original_deleted": BOOL,
        "admission_reviews_created": UINT,
        "recording_correspondence": text(32),
        "file_integrity_and_embedded_notices": text(32),
        "reader": Wire("reader_or_null"),
    },
    "case_binding",
    "created_at",
    "source_url",
    "metadata_digest",
    "credit_snapshot",
    "acquired_at",
    "acquisition",
    "notices_count",
    "notices_sha256",
    "reader",
)
VALIDATION = obj(
    {
        "case_binding": CASE,
        "phase": text(32, "code"),
        "finished": BOOL,
        "delivered_bytes": UINT,
        "metadata": Wire("metadata_or_null"),
        "decode": Wire("decode_or_null"),
        "terminal_error": CODE,
        "first_write_cancelled": BOOL,
        "helpers": array(HELPER, 2),
        "active": Wire("helper_or_null"),
        "cleanup": CLOCK,
    }
)
OWNER = obj(
    {
        "schema": text(64),
        "case_binding": CASE,
        "packet_sha256": HASH,
        "passed": BOOL,
        "failure": CODE,
        "init": PIN,
        "owner": PIN,
        "intake": INTAKE,
        "reader": Wire("reader_or_null"),
        "validation": VALIDATION,
        "memory_event_delta": COUNTERS,
        "cleanup": CLOCK,
        "catalog_admitted": BOOL,
        "container_exit_observed": BOOL,
        "qualification_passed": BOOL,
    },
    "qualification_passed",
)
LABELS = obj(
    {"org.sobafm.project": text(16), "org.sobafm.issue": text(8), "org.sobafm.packet": HASH}
)
STATE = obj({"Running": BOOL, "Pid": FD, "Status": text(32), "ExitCode": EXIT, "OOMKilled": BOOL})
HOST = obj(
    {
        "Init": BOOL,
        "ReadonlyRootfs": BOOL,
        "Privileged": BOOL,
        "NetworkMode": text(32),
        "CgroupnsMode": text(32),
        "Memory": UINT,
        "MemorySwap": UINT,
        "PidsLimit": UINT,
        "NanoCpus": UINT,
        "CapDrop": array(text(32), 64),
        "CapAdd": Wire("strings_or_null"),
        "SecurityOpt": array(text(128), 16),
        "RestartPolicy": obj({"Name": text(32)}),
        "LogConfig": obj({"Type": text(32)}),
    }
)
DOCKER_CLEANUP = obj(
    {"id": HASH, "name": PATH, "image": text(71), "labels": LABELS, "state": STATE}
)
DOCKER_CONFIG = obj(
    {
        **DOCKER_CLEANUP.fields,
        "host": HOST,
        "user": text(32),
        "path": PATH,
        "args": array(PATH, 8),
        "workdir": PATH,
        "mounts": array(
            obj(
                {
                    "Type": text(16),
                    "Source": PATH,
                    "Destination": PATH,
                    "RW": BOOL,
                    "Name": text(128),
                },
                "Name",
                "Source",
            ),
            4,
        ),
    }
)
DOCKER_INFO = obj({"architecture": text(32), "os": text(16), "seccomp": BOOL})
DOCKER_IMAGE = obj({"id": text(71), "architecture": text(32), "os": text(16)})
DOCKER_VOLUME = obj(
    {
        "Name": text(128),
        "CreatedAt": PATH,
        "Mountpoint": PATH,
        "Labels": obj({"org.sobafm.issue": text(8)}),
    }
)
FAMILIES = {
    "admission": ADMISSION,
    "decode": DECODE,
    "owner": OWNER,
    "metadata": METADATA,
    "rejection": REJECTION,
    "config": DOCKER_CONFIG,
    "cleanup": DOCKER_CLEANUP,
    "info": DOCKER_INFO,
    "image": DOCKER_IMAGE,
    "volume": DOCKER_VOLUME,
}
DECODE_ENVELOPE = frozenset({"event", "schema", "case_binding", "runner", "cleanup"})
OWNER_ENVELOPE = frozenset({"schema", "case_binding", "init", "owner", "packet_sha256", "cleanup"})
NUMBER_TOKEN = re.compile(rb"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?")


def discard_exception(error: BaseException) -> None:
    """Completed parse/command failures cannot retain payload through cleanup."""
    pending = [error]
    while pending:
        current = pending.pop()
        linked = (current.__cause__, current.__context__)
        current.__cause__ = current.__context__ = None
        current.args = ()
        if current.__traceback__ is not None:
            traceback.clear_frames(current.__traceback__)
        current.__traceback__ = None
        for item in linked:
            if item is not None and item is not current:
                pending.append(item)


class Scan:
    """Index-only lexical/shape pass; unsupported values never reach json.loads."""

    def __init__(self, data: memoryview, rule: Wire, *, utf8: bool = False) -> None:
        self.data, self.rule, self.utf8 = data, rule, utf8
        self.pos = self.nodes = 0
        self.valid = True
        self.spans: dict[str, tuple[int, int]] = {}
        self.envelope_valid = True
        self.envelope_keys = (
            DECODE_ENVELOPE
            if rule is DECODE
            else OWNER_ENVELOPE
            if rule is OWNER
            else frozenset[str]()
        )

    def space(self) -> None:
        while self.pos < len(self.data) and self.data[self.pos] in (9, 10, 13, 32):
            self.pos += 1

    def take(self, byte: int) -> None:
        self.space()
        if self.pos >= len(self.data) or self.data[self.pos] != byte:
            raise ProbeError("ingress_json_syntax")
        self.pos += 1

    def codepoint(self) -> int:
        byte = self.data[self.pos]
        self.pos += 1
        if byte == 92:
            if self.pos >= len(self.data):
                raise ProbeError("ingress_json_escape")
            escape = self.data[self.pos]
            self.pos += 1
            short = {34: 34, 92: 92, 47: 47, 98: 8, 102: 12, 110: 10, 114: 13, 116: 9}
            if escape in short:
                return short[escape]
            if escape != 117:
                raise ProbeError("ingress_json_escape")
            value = self.hex4()
            if (
                0xD800 <= value <= 0xDBFF
                and self.pos + 6 <= len(self.data)
                and self.data[self.pos] == 92
                and self.data[self.pos + 1] == 117
            ):
                saved = self.pos
                self.pos += 2
                low = self.hex4()
                if 0xDC00 <= low <= 0xDFFF:
                    return 0x10000 + ((value - 0xD800) << 10) + low - 0xDC00
                self.pos = saved
            return value
        if byte < 32:
            raise ProbeError("ingress_json_control")
        if byte < 128:
            return byte
        if not self.utf8 or not 0xC2 <= byte <= 0xF4:
            raise ProbeError("ingress_encoding")
        width = 2 if byte < 0xE0 else 3 if byte < 0xF0 else 4
        value = byte & (0x7F >> width)
        if self.pos + width - 1 > len(self.data):
            raise ProbeError("ingress_encoding")
        for _ in range(width - 1):
            byte = self.data[self.pos]
            self.pos += 1
            if byte & 0xC0 != 0x80:
                raise ProbeError("ingress_encoding")
            value = (value << 6) | (byte & 0x3F)
        if value < (128, 2048, 65536)[width - 2] or value > 0x10FFFF or 0xD800 <= value <= 0xDFFF:
            raise ProbeError("ingress_encoding")
        return value

    def hex4(self) -> int:
        if self.pos + 4 > len(self.data):
            raise ProbeError("ingress_json_escape")
        value = 0
        for _ in range(4):
            byte = self.data[self.pos]
            self.pos += 1
            digit = (
                byte - 48
                if 48 <= byte <= 57
                else byte - 65 + 10
                if 65 <= byte <= 70
                else byte - 97 + 10
                if 97 <= byte <= 102
                else -1
            )
            if digit < 0:
                raise ProbeError("ingress_json_escape")
            value = (value << 4) | digit
        return value

    def string(self, rule: Wire | None, *, key: bool = False) -> str:
        self.take(34)
        count = 0
        separators = 0
        chars: list[str] = []
        while self.pos < len(self.data) and self.data[self.pos] != 34:
            cp = self.codepoint()
            count += 1
            if key:
                if count > 64 or not 32 <= cp < 127:
                    raise ProbeError("ingress_key_domain")
                chars.append(chr(cp))
            elif rule is not None:
                domain = rule.domain
                if domain == "timestamp":
                    ordinary = 48 <= cp <= 57 or cp in (43, 44, 45, 46, 58, 84, 87, 90, 116)
                    if not ordinary:
                        separators += 1
                    if count > rule.count or (
                        not ordinary and (count not in (8, 9, 11) or separators > 1)
                    ):
                        self.valid = False
                    continue
                allowed = (
                    cp <= 255
                    if domain == "latin1"
                    else 32 <= cp <= 0x10FFFF and not 0xD800 <= cp <= 0xDFFF
                    if domain == "path"
                    else 48 <= cp <= 57 or 65 <= cp <= 90
                    if domain == "frame"
                    else 48 <= cp <= 57 or 97 <= cp <= 102
                    if domain == "hex"
                    else 48 <= cp <= 57 or 65 <= cp <= 90 or 97 <= cp <= 122 or cp == 95
                    if domain == "code"
                    else cp < 128
                )
                if count > rule.count or not allowed:
                    self.valid = False
        if self.pos >= len(self.data):
            raise ProbeError("ingress_json_string")
        self.pos += 1
        return "".join(chars)

    def value(self, rule: Wire | None, depth: int = 0) -> None:
        self.space()
        self.nodes += 1
        if depth > 12 or self.nodes > 8192 or self.pos >= len(self.data):
            raise ProbeError("ingress_structure_limit")
        byte = self.data[self.pos]
        if rule is not None and rule.kind.endswith("_or_null"):
            base = rule.kind.removesuffix("_or_null")
            rule = (
                {
                    "pin": PIN,
                    "reader": READER,
                    "helper": HELPER,
                    "decode": DECODE,
                    "metadata": METADATA_RESULT,
                    "strings": array(text(32), 64),
                }[base]
                if byte != 110
                else Wire("null")
            )
        if rule is not None and rule.kind == "notice":
            rule = text(4096, "latin1") if byte == 34 else array(Wire("notice_item"), 128)
        if rule is not None and rule.kind == "notice_item":
            rule = text(4096, "latin1") if byte == 34 else array(text(4096, "latin1"), 128)
        if byte == 123:
            if rule is not None and rule.kind not in ("object", "counters"):
                self.valid, rule = False, None
            self.pos += 1
            self.space()
            seen: set[str] = set()
            while self.pos < len(self.data) and self.data[self.pos] != 125:
                key = self.string(None, key=True)
                if key in seen or len(seen) >= 128:
                    raise ProbeError(
                        "duplicate_report_field" if key in seen else "ingress_key_limit"
                    )
                seen.add(key)
                child = None if rule is None else rule.fields.get(key)
                if rule is not None and rule.kind == "counters":
                    if len(seen) > 32 or not re.fullmatch(r"[A-Za-z0-9_]{1,32}", key):
                        self.valid = False
                    child = rule.item
                elif rule is not None and child is None:
                    self.valid = False
                self.take(58)
                self.space()
                start, previous = self.pos, self.valid
                self.valid = True
                self.value(child, depth + 1)
                child_valid = self.valid
                self.valid &= previous
                if depth == 0 and key in self.envelope_keys:
                    self.spans[key] = (start, self.pos)
                    self.envelope_valid &= child_valid and child is not None
                self.space()
                if self.pos < len(self.data) and self.data[self.pos] == 125:
                    break
                self.take(44)
                self.space()
                if self.pos < len(self.data) and self.data[self.pos] == 125:
                    raise ProbeError("ingress_json_syntax")
            self.take(125)
            if (
                rule is not None
                and rule.kind == "object"
                and rule.fields.keys() - rule.optional - seen
            ):
                self.valid = False
            if rule is METADATA_RESULT and seen not in (
                METADATA.fields.keys(),
                REJECTION.fields.keys(),
            ):
                self.valid = False
            if rule is DIAGNOSTICS and seen not in (
                {"bytes", "sha256"},
                {
                    "format",
                    "diagnostic_bytes",
                    "diagnostic_sha256",
                    "tag_block_sha256",
                    "terminal_source_filter_required",
                },
            ):
                self.valid = False
            if rule is FRAMING and seen not in (
                {"v2_present", "frame_ids", "duplicate_frame_ids"},
                FRAMING.fields.keys(),
            ):
                self.valid = False
        elif byte == 91:
            if rule is not None and rule.kind != "array":
                self.valid, rule = False, None
            self.pos += 1
            self.space()
            count = 0
            while self.pos < len(self.data) and self.data[self.pos] != 93:
                count += 1
                if rule is not None and count > rule.count:
                    self.valid = False
                self.value(None if rule is None else rule.item, depth + 1)
                self.space()
                if self.pos < len(self.data) and self.data[self.pos] == 93:
                    break
                self.take(44)
                self.space()
                if self.pos < len(self.data) and self.data[self.pos] == 93:
                    raise ProbeError("ingress_json_syntax")
            self.take(93)
        elif byte == 34:
            if rule is not None and rule.kind != "string":
                self.valid, rule = False, None
            self.string(rule)
        elif byte in (110, 116, 102):
            literal = b"null" if byte == 110 else b"true" if byte == 116 else b"false"
            if self.pos + len(literal) > len(self.data) or any(
                self.data[self.pos + i] != byte for i, byte in enumerate(literal)
            ):
                raise ProbeError("ingress_json_literal")
            self.pos += len(literal)
            if rule is not None and not (
                rule.kind == ("null" if byte == 110 else "bool") or byte == 110 and rule.nullable
            ):
                self.valid = False
        else:
            match = NUMBER_TOKEN.match(self.data, self.pos)
            if match is None:
                raise ProbeError("ingress_number")
            start = self.pos
            self.pos = match.end()
            if rule is None:
                return
            integer = not any(self.data[i] in (46, 101, 69) for i in range(start, self.pos))
            if integer and self.pos - start > 40:
                self.valid = False
                return
            # A meaningful finite float may have a long fractional/exponent
            # spelling. This one token can cost R transient bytes; its conversion
            # scope ends before whole-record JSON materialization. No J<=2R claim.
            token = bytes(self.data[start : self.pos])
            number = int(token) if integer else float(token)
            if not math.isfinite(number) or integer and abs(number) >= 2**128:
                self.valid = False
            if (
                rule.kind not in ("int", "number")
                or rule.kind == "int"
                and not integer
                or not rule.minimum <= number <= rule.maximum
            ):
                self.valid = False

    def run(self) -> None:
        self.value(self.rule)
        self.space()
        if self.pos != len(self.data):
            raise ProbeError("ingress_json_suffix")


def pairs(items: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in items:
        if key in result:
            raise ProbeError("duplicate_report_field")
        result[key] = value
    return result


def materialize(data: memoryview, *, utf8: bool = False) -> dict[str, object]:
    value: object = json.loads(
        bytes(data).decode("utf-8" if utf8 else "ascii"), object_pairs_hook=pairs
    )
    if type(value) is not dict:
        raise ProbeError("helper_report_object_required")
    return cast(dict[str, object], value)


def decode(data: bytes | bytearray | memoryview, family: str) -> dict[str, object]:
    """One selected Docker object or producer object; no encoding autodetection."""
    if len(data) > REPORT_BYTES:
        raise ProbeError("helper_report_bound")
    with memoryview(data) as view:
        scan = Scan(
            view, FAMILIES[family], utf8=family in ("info", "image", "volume", "config", "cleanup")
        )
        scan.run()
        if not scan.valid:
            raise ProbeError("ingress_schema")
        return materialize(view, utf8=scan.utf8)


@dataclass
class Record:
    value: dict[str, object] | None
    envelope: dict[str, object] | None
    family: str


class Ingress:
    """One original stream budget, two finite slots and sticky suffix failure.

    A retained raw prefix is not refunded by parsing or retiring a graph. The
    externally visible backing is checked for mutation before using cached facts.
    """

    def __init__(self, role: str) -> None:
        self.role = role
        self.raw = bytearray()
        self.count = 0
        self.digest = hashlib.sha256()
        self.offset = 0
        self.records: list[Record] = []
        self.error: str | None = None
        self.retired = False
        self.final = False

    def feed(self, data: bytes) -> None:
        if self.retired or self.final:
            self.error = "ingress_after_eof"
            return
        remaining = REPORT_BYTES - len(self.raw)
        with memoryview(data)[:remaining] as part:
            self.raw.extend(part)
        if len(data) > remaining:
            self.error = "helper_report_bound"

    def check_prefix(self) -> bool:
        if self.retired:
            return False
        with memoryview(self.raw) as view:
            if (
                len(view) < self.count
                or hashlib.sha256(view[: self.count]).digest() != self.digest.digest()
            ):
                self.error = "ingress_prefix_changed"
                return False
            self.digest.update(view[self.count :])
            self.count = len(view)
        if self.count > REPORT_BYTES:
            self.error = "helper_report_bound"
            return False
        return True

    def scan(self, *, final: bool = False) -> None:
        if not self.check_prefix():
            return
        self.final |= final
        while self.offset < len(self.raw):
            end = self.raw.find(b"\n", self.offset)
            if end < 0:
                if not final:
                    break
                if self.role != "metadata":
                    self.error = self.error or "ingress_partial_record"
                    break
                end = len(self.raw)
            start, self.offset = self.offset, end + 1
            if end == start:
                continue
            if len(self.records) >= (2 if self.role == "decoder" else 1):
                self.error = self.error or "unexpected_helper_report"
                continue
            with memoryview(self.raw)[start:end] as view:
                self.record(view)

    def record(self, view: memoryview) -> None:
        # Lexically read only the bounded discriminator; no arbitrary JSON graph.
        candidates = (
            ("admission", "decode")
            if self.role == "decoder"
            else ("metadata", "rejection")
            if self.role == "metadata"
            else ("owner",)
        )
        record: Record | None = None
        try:
            for family in candidates:
                rule = FAMILIES[family]
                if self.role == "metadata":
                    rule = obj({**rule.fields, "case_binding": CASE, "helper_pid": FD})
                scan = Scan(view, rule)
                scan.run()
                if scan.valid:
                    value = materialize(view)
                    record = Record(value, value, family)
                    break
                if family in ("decode", "owner") and scan.envelope_valid:
                    envelope: dict[str, object] = {}
                    for key, (start, end) in scan.spans.items():
                        envelope[key] = json.loads(
                            bytes(view[start:end]).decode("ascii"), object_pairs_hook=pairs
                        )
                    record = Record(None, envelope, family)
            if record is None:
                record = Record(None, None, self.role)
            if record.value is None:
                self.error = self.error or "ingress_schema"
        except (ValueError, TypeError, RecursionError, ProbeError) as error:
            discard_exception(error)
            record = Record(None, None, self.role)
            self.error = self.error or "ingress_invalid_record"
        self.records.append(record)

    def retire(self) -> None:
        self.check_prefix()
        self.raw.clear()
        self.records.clear()
        self.retired = True
