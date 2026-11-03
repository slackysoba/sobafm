"""Contained read-only ID3 inspection; GPL-compatible evaluation, see NOTICE.md."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import importlib
import os
import resource
import signal
import stat
import sys
import traceback
from collections import Counter
from collections.abc import Mapping
from typing import Protocol, cast

from evaluations.linux_resource.probe import ProbeError

from .cases import CASE_IDS, CaseBinding, CaseId
from .limits import MAX_FRAMES, MAX_SEEKS, REPORT_BYTES, TAG_BYTES, encode_report, json_size
from .report_io import RecordWrite

ADDRESS_BYTES = 67_108_864
DATA_BYTES = 50_331_648
CPU_SECONDS = 10
PARSER_TAG_BYTES = 524_288
DECODED_SOURCE_BYTES = 8192
FRAME_SOURCE_BYTES = 4096
# Preserve the raw envelope while bounding every frame allowed to allocate
# Unicode, uniqueness keys or textual projections. Opaque frames are never
# passed to a frame-specific parser. COMR's binary-bearing key is unsupported.
TEXT_FRAMES = frozenset({"COMM", "USER", "IPLS", "OWNE"})
UNSUPPORTED_FRAMES = {
    "CHAP": "unsupported_nested_id3",
    "CTOC": "unsupported_nested_id3",
    "COMR": "unsupported_commercial_notice",
}
COPY_ACCOUNTING = {
    "tag_slices_and_opaque_frames": 3 * PARSER_TAG_BYTES,
    "decoded_frame_transients_and_keys": 24 * DECODED_SOURCE_BYTES,
    "projection_framing_and_hashes": 2 * REPORT_BYTES,
    # Assign the old 2R serialization + R fixed margin, without a new pool.
    "report_work_immutable_and_prior_record": 3 * REPORT_BYTES,
}
assert sum(COPY_ACCOUNTING.values()) == 2_097_152


class MetadataRejected(ProbeError):
    """Only source-independent codes from this module may be returned to owner."""


REJECTION_REASONS = frozenset(
    {
        *UNSUPPORTED_FRAMES.values(),
        "metadata_file_binding",
        "metadata_read_limit",
        "metadata_seek_limit",
        "metadata_seek_mode",
        "metadata_seek_extent",
        "invalid_id3_size",
        "unsupported_id3_version",
        "unsupported_id3_header_flags",
        "metadata_tag_limit",
        "metadata_copy_reservation",
        "id3_nonzero_padding",
        "metadata_frame_limit",
        "unsupported_id3_frame_id",
        "unsupported_id3_frame_transform",
        "invalid_id3_frame_extent",
        "unresolved_duplicate_frame_id",
        "metadata_decoded_payload_reservation",
        "unsupported_id3_text_encoding",
        "metadata_notice_exceeds_supported_bound",
        "unsupported_notice_field",
        "metadata_dependency_version",
        "unsupported_textual_frame",
        "unresolved_dropped_or_changed_notice",
        "unresolved_opaque_frame_loss",
        "metadata_admission_revoked",
        "invalid_report_limit",
        "report_capacity_exceeded",
        "report_structure_limit",
        "report_integer_limit",
        "report_nonfinite_number",
        "report_string_key_required",
        "unsupported_report_value",
        "report_changed_during_encoding",
    }
)


def discard_failure(error: BaseException) -> None:
    """Drop completed parser/build frames and exception links before fallback."""
    pending = [error]
    while pending:
        current = pending.pop()
        traceback.clear_frames(current.__traceback__)
        cause, context = current.__cause__, current.__context__
        current.__traceback__ = current.__cause__ = current.__context__ = None
        current.args = ()
        # Sever before following, so even linked cycles terminate without GC.
        for linked in (cause, context):
            if linked is not None and linked is not current:
                pending.append(linked)


def selected_id(name: str) -> bool:
    return name.startswith(("T", "W")) or name in TEXT_FRAMES


class BoundedFile:
    """Borrow a read-only inode; never open a path, close it, or expose writes."""

    def __init__(self, fd: int, size: int) -> None:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_size != size
            or info.st_nlink != 1
            or fcntl.fcntl(fd, fcntl.F_GETFL) & os.O_ACCMODE != os.O_RDONLY
        ):
            raise ProbeError("metadata_file_binding")
        self.fd, self.size = fd, size
        self.read_bytes = self.seeks = 0
        self.name = "owned-pending-inode"

    def read(self, size: int = -1) -> bytes:
        if size < 0 or size > TAG_BYTES or self.read_bytes + size > 3 * self.size:
            raise ProbeError("metadata_read_limit")
        data = os.read(self.fd, size)
        self.read_bytes += len(data)
        return data

    def seek(self, offset: int, whence: int = os.SEEK_SET) -> int:
        self.seeks += 1
        if self.seeks > MAX_SEEKS:
            raise ProbeError("metadata_seek_limit")
        if whence == os.SEEK_SET:
            target = offset
        elif whence == os.SEEK_CUR:
            target = self.tell() + offset
        elif whence == os.SEEK_END:
            target = self.size + offset
        else:
            raise ProbeError("metadata_seek_mode")
        if not 0 <= target <= self.size:
            raise ProbeError("metadata_seek_extent")
        return os.lseek(self.fd, target, os.SEEK_SET)

    def tell(self) -> int:
        return os.lseek(self.fd, 0, os.SEEK_CUR)


def synchsafe(raw: bytes) -> int:
    if len(raw) != 4 or any(value & 0x80 for value in raw):
        raise ProbeError("invalid_id3_size")
    return sum(value << (7 * (3 - index)) for index, value in enumerate(raw))


def preflight(stream: BoundedFile) -> dict[str, object]:
    """Bound before library allocation. Unsupported framing is visible, not guessed.

    Compression/encryption/unsynchronization/extended headers need separate
    review; this candidate does not silently repair or inflate them.
    """
    stream.seek(0)
    header = stream.read(10)
    if not header.startswith(b"ID3"):
        stream.seek(0)
        return {"v2_present": False, "frame_ids": [], "duplicate_frame_ids": []}
    # v2.4's determine_bpi may choose unchecked ordinary-size boundaries.
    # This supported subset uses v2.3's one fixed interpretation only.
    if len(header) != 10 or header[3] != 3 or header[4] != 0:
        raise MetadataRejected("unsupported_id3_version")
    if header[5] != 0:
        raise ProbeError("unsupported_id3_header_flags")
    end = 10 + synchsafe(header[6:10])
    if end > stream.size or end > TAG_BYTES:
        raise ProbeError("metadata_tag_limit")
    if end > PARSER_TAG_BYTES:
        raise ProbeError("metadata_copy_reservation")
    version = header[3]
    ids: list[str] = []
    decoded_bytes = 0
    width = 10
    while stream.tell() < end:
        remaining = end - stream.tell()
        raw = stream.read(min(width, remaining))
        if not any(raw):
            while stream.tell() < end:
                if any(stream.read(min(4096, end - stream.tell()))):
                    raise ProbeError("id3_nonzero_padding")
            break
        if len(raw) != width or len(ids) >= MAX_FRAMES:
            raise ProbeError("metadata_frame_limit")
        name_bytes = raw[:4]
        if not all(48 <= value <= 57 or 65 <= value <= 90 for value in name_bytes):
            raise ProbeError("unsupported_id3_frame_id")
        length = int.from_bytes(raw[4:8], "big")
        if raw[9] != 0:
            raise ProbeError("unsupported_id3_frame_transform")
        if length <= 0 or stream.tell() + length > end:
            raise ProbeError("invalid_id3_frame_extent")
        name = name_bytes.decode("ascii")
        if name in UNSUPPORTED_FRAMES:
            raise MetadataRejected(UNSUPPORTED_FRAMES[name])
        if name in ids:
            raise MetadataRejected("unresolved_duplicate_frame_id")
        skip = length
        if selected_id(name):
            decoded_bytes += length
            if length > FRAME_SOURCE_BYTES or decoded_bytes > DECODED_SOURCE_BYTES:
                raise MetadataRejected("metadata_decoded_payload_reservation")
            if name.startswith("T") or name == "WXXX" or name in TEXT_FRAMES:
                # Encoding 0 is total Latin-1 decoding. UTF-16 repair can retain
                # exception/traceback cycles containing many shrinking suffixes
                # before projection rejects the value count. Exclude every
                # other encoding before library entry, including valid UTF-8.
                if stream.read(1) != b"\x00":
                    raise MetadataRejected("unsupported_id3_text_encoding")
                skip -= 1
        ids.append(name)
        stream.seek(skip, os.SEEK_CUR)
    stream.seek(0)
    return {
        "v2_present": True,
        "v2_version": [2, version, 0],
        "tag_bytes": end,
        "decoded_source_bytes": decoded_bytes,
        "frame_ids": ids,
        "duplicate_frame_ids": sorted(key for key, count in Counter(ids).items() if count > 1),
    }


class Id3Loader(Protocol):
    def __call__(
        self,
        fileobj: BoundedFile,
        *,
        known_frames: dict[str, object],
        translate: bool,
        load_v1: bool,
    ) -> object: ...


class V1Parser(Protocol):
    def __call__(self, data: bytes, v2_version: int = 4) -> object: ...


class ParsedTags(Protocol):
    unknown_frames: list[bytes]


class ParsedFrame(Protocol):
    @property
    def FrameID(self) -> str: ...


def bounded_text(value: object) -> str:
    text = str(value)
    if len(text) > 4096 or len(text.encode("utf-8")) > 16384:
        raise ProbeError("metadata_notice_exceeds_supported_bound")
    return text


def selected_frames(tags: Mapping[str, object]) -> tuple[list[dict[str, object]], list[str]]:
    selected: list[dict[str, object]] = []
    unsupported: list[str] = []
    if len(tags) > MAX_FRAMES:
        raise ProbeError("metadata_frame_limit")
    for frame in tags.values():
        # Never export a library HashKey: COMR, for example, serializes its
        # entire binary-bearing frame into that supposedly textual key.
        frame_id = cast(ParsedFrame, frame).FrameID
        if not selected_id(frame_id):
            unsupported.append(bounded_text(frame_id))
            continue
        fields: dict[str, object] = {}
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
        ):
            value = cast(object, getattr(frame, name, None))
            if value is None:
                continue
            if isinstance(value, list):
                items = cast(list[object], value)
                if len(items) > 128:
                    raise ProbeError("metadata_notice_exceeds_supported_bound")
                if any(
                    isinstance(item, list) and len(cast(list[object], item)) > 128 for item in items
                ):
                    raise ProbeError("metadata_notice_exceeds_supported_bound")
                fields[name] = [
                    [bounded_text(part) for part in cast(list[object], item)]
                    if isinstance(item, list)
                    else bounded_text(item)
                    for item in items
                ]
            elif isinstance(value, str):
                fields[name] = bounded_text(value)
            else:
                raise ProbeError("unsupported_notice_field")
        selected.append({"id": frame_id, "key": frame_id, "fields": fields})
        # Reject before accumulating another projected notice; never truncate.
        json_size(selected, REPORT_BYTES // 2)
    return selected, sorted(unsupported)


def inspect(fd: int, size: int) -> dict[str, object]:
    """Only called after helper containment, or with owned synthetic test input."""
    if importlib.import_module("mutagen").version_string != "1.48.1":
        raise ProbeError("metadata_dependency_version")
    stream = BoundedFile(fd, size)
    framing = preflight(stream)
    load = cast(Id3Loader, importlib.import_module("mutagen.id3").ID3)
    v2: list[dict[str, object]] = []
    omitted: list[str] = []
    unknown: list[dict[str, object]] = []
    if framing["v2_present"]:
        definitions = cast(dict[str, object], importlib.import_module("mutagen.id3").Frames)
        known = {name: frame for name, frame in definitions.items() if selected_id(name)}
        source_ids = cast(list[str], framing["frame_ids"])
        # An unrecognized textual-looking frame is explicit unresolved evidence,
        # rather than a silently omitted licensing/contributor notice.
        if any(selected_id(name) and name not in known for name in source_ids):
            raise MetadataRejected("unsupported_textual_frame")
        tag = load(stream, known_frames=known, translate=False, load_v1=False)
        v2, omitted = selected_frames(cast(Mapping[str, object], tag))
        expected = {name for name in source_ids if name in known}
        if {item["id"] for item in v2} != expected or len(v2) != len(expected):
            raise MetadataRejected("unresolved_dropped_or_changed_notice")
        opaque_ids: list[str] = []
        for raw in cast(ParsedTags, tag).unknown_frames:
            name = raw[:4].decode("ascii")
            opaque_ids.append(name)
            unknown.append(
                {"id": name, "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
            )
            if len(unknown) > MAX_FRAMES:
                raise ProbeError("metadata_frame_limit")
        if Counter(opaque_ids) != Counter(name for name in source_ids if name not in known):
            raise MetadataRejected("unresolved_opaque_frame_loss")
        omitted.extend(opaque_ids)
    v1: list[dict[str, object]] = []
    v1_version: str | None = None
    tail_hash: str | None = None
    if size >= 128:
        stream.seek(-128, os.SEEK_END)
        tail = stream.read(128)
        if tail.startswith(b"TAG"):
            v1_version = "1.1" if tail[-3] == 0 else "1.0"
            tail_hash = hashlib.sha256(tail).hexdigest()
            parse_v1 = cast(V1Parser, importlib.import_module("mutagen.id3._id3v1").ParseID3v1)
            v1, extra = selected_frames(cast(Mapping[str, object], parse_v1(tail)))
            omitted.extend(extra)
    # Preserve both source versions. A differing shared field remains visible.
    by_v1 = {str(item["id"]): item["fields"] for item in v1}
    conflicts = sorted(
        {
            str(item["id"])
            for item in v2
            if str(item["id"]) in by_v1 and by_v1[str(item["id"])] != item["fields"]
        }
    )
    result: dict[str, object] = {
        "schema": "sobafm-fma-metadata-1",
        "parser": "mutagen-1.48.1",
        "framing": framing,
        "id3v2": v2,
        "id3v1": v1,
        "id3v1_version": v1_version,
        "id3v1_sha256": tail_hash,
        "conflicting_frame_ids": conflicts,
        "omitted_frame_ids": sorted(set(omitted)),
        "unknown_frames": unknown,
        "read_bytes": stream.read_bytes,
        "seek_count": stream.seeks,
        "credits_complete": False,
        "recording_correspondence": "requires_trusted_review",
        "links_followed": False,
    }
    json_size(result, REPORT_BYTES - 1)
    return result


def contain() -> None:
    for kind, limit in (
        (resource.RLIMIT_AS, ADDRESS_BYTES),
        (resource.RLIMIT_DATA, DATA_BYTES),
        (resource.RLIMIT_CPU, CPU_SECONDS),
        (resource.RLIMIT_CORE, 0),
        (resource.RLIMIT_FSIZE, 0),
    ):
        resource.setrlimit(kind, (limit, limit))
    signal.alarm(30)


def inspection_record(owned_fd: int, admit_fd: int, case: CaseBinding) -> bytes:
    contain()
    if os.read(admit_fd, 1) != b"x":
        raise ProbeError("metadata_admission_revoked")
    os.close(admit_fd)
    result = inspect(owned_fd, case.bytes)
    result["schema"] = case.schema("metadata")
    result.update(case_binding=case.record(), helper_pid=os.getpid())
    return encode_report(result, newline=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--owned-fd", type=int, required=True)
    parser.add_argument("--case", choices=CASE_IDS, default="night-owl")
    parser.add_argument("--cancel-fd", type=int, required=True)
    parser.add_argument("--admit-fd", type=int, required=True)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    if not args.execute or sys.platform != "linux":
        parser.error("explicit Linux evaluation required")
    case = CaseBinding(cast(CaseId, args.case))
    data: bytes | None = None
    reason: str | None = None
    try:
        data = inspection_record(args.owned_fd, args.admit_fd, case)
    except Exception as error:
        # Parser exceptions can contain source text; never print or trust them.
        reason = "metadata_inspection_failed"
        if isinstance(error, ProbeError) and len(error.args) == 1:
            code = error.args[0]
            if type(code) is str and len(code) <= 80 and code in REJECTION_REASONS:
                reason = code
            code = None
        discard_failure(error)
    failed = reason is not None
    # Outside the except suite: no active parser/build exception becomes the
    # rejection's context, and rejected backing is released before allocation.
    if failed:
        try:
            data = encode_report(
                {
                    "schema": case.schema("metadata-rejection"),
                    "reason": reason,
                    "credits_complete": False,
                    "case_binding": case.record(),
                    "helper_pid": os.getpid(),
                },
                newline=True,
            )
        except Exception as error:
            discard_failure(error)
            os.write(2, b"metadata_inspection_failed\n")
            return 1
    assert data is not None
    record = RecordWrite(data, "metadata_report_write")
    try:
        record.write(1)
    except Exception as error:
        # Even offset zero may mean an uncertain first submission. Never build
        # or append another terminal after this point. Keep record/data owned.
        discard_failure(error)
        failed = True
    if failed:
        os.write(2, b"metadata_inspection_failed\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
