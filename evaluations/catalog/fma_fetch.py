"""Read selected files from the FMA dataset's full archive, inside explicit limits.

Not part of CI or the application. Run it only under a recorded maintainer authorization:
ADR 0007 and issue 122 record the current one. Decisions about which tracks qualify live in
`sobafm.catalog_prepare`; this script only makes the requests.

The archive is a single 943 GB zip file. Ordinary range requests read its directory and then
each selected file, so nothing else in it is downloaded. Every request goes through one
`Meter`, which keeps a running count on disk, and any response other than the expected partial
content stops the whole run. Once stopped it stays stopped until a person removes the marker.
A transfer that drops resumes from where it stopped, at most twice per read. There are no
changes of headers, identity or client, and no request goes to freemusicarchive.org.

Usage, from a container of the project's image:

    python fma_fetch.py directory --state DIR
    python fma_fetch.py audio --state DIR --media DIR --limit 50
"""

from __future__ import annotations

import argparse
import bz2
import hashlib
import http.client
import json
import os
import struct
import time
import urllib.error
import urllib.request
import zlib
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import cast

from sobafm import catalog_prepare as prepare

ARCHIVE_URL = "https://os.unil.cloud.switch.ch/fma/fma_full.zip"
USER_AGENT = "sobafm-catalog/0"
TAIL_BYTES = 1_048_576
MAX_DIRECTORY_BYTES = 40_000_000
MAX_FILES = 230
MIN_SPACING_SECONDS = 2.0
MAX_RESUMES = 2
READ_TIMEOUT_SECONDS = 180
# The local header's name and extra fields come before the data; this always covers them.
LOCAL_HEADER_ROOM = 1_024
STORED = 0
DEFLATED = 8
BZIP2 = 12


class Stopped(Exception):
    """A stop condition was met. Nothing more may be requested."""


class BadArchive(Exception):
    """The archive's bytes aren't what its directory says, for this file only."""


@dataclass(frozen=True)
class Entry:
    """One file's place in the archive, from its central directory."""

    method: int
    compressed: int
    plain: int
    offset: int
    crc: int


def directory_location(tail: bytes, total: int) -> tuple[int, int]:
    """The central directory's offset and size, from the last bytes of the archive."""
    end = tail.rfind(b"PK\x05\x06")
    if end < 0:
        raise BadArchive("no end-of-directory record")
    size, offset = struct.unpack("<II", tail[end + 12 : end + 20])
    if size == 0xFFFFFFFF or offset == 0xFFFFFFFF:
        locator = tail.rfind(b"PK\x06\x07", 0, end)
        if locator < 0:
            raise BadArchive("no zip64 locator")
        record = struct.unpack("<Q", tail[locator + 8 : locator + 16])[0]
        at = record - (total - len(tail))
        if at < 0 or tail[at : at + 4] != b"PK\x06\x06":
            raise BadArchive("no zip64 record in the tail")
        size, offset = struct.unpack("<QQ", tail[at + 40 : at + 56])
    return offset, size


def _zip64_values(extra: bytes, plain: int, compressed: int, offset: int) -> tuple[int, int, int]:
    """Replace the fields a zip64 extra record holds instead."""
    at = 0
    while at + 4 <= len(extra):
        kind, length = struct.unpack("<HH", extra[at : at + 4])
        if kind == 1:
            values = extra[at + 4 : at + 4 + length]
            index = 0
            if plain == 0xFFFFFFFF:
                plain = struct.unpack("<Q", values[index : index + 8])[0]
                index += 8
            if compressed == 0xFFFFFFFF:
                compressed = struct.unpack("<Q", values[index : index + 8])[0]
                index += 8
            if offset == 0xFFFFFFFF:
                offset = struct.unpack("<Q", values[index : index + 8])[0]
        at += 4 + length
    return plain, compressed, offset


def parse_directory(data: bytes) -> dict[str, Entry]:
    entries: dict[str, Entry] = {}
    position = 0
    while position + 46 <= len(data) and data[position : position + 4] == b"PK\x01\x02":
        method = struct.unpack("<H", data[position + 10 : position + 12])[0]
        crc, compressed, plain = struct.unpack("<III", data[position + 16 : position + 28])
        name_length, extra_length, comment_length = struct.unpack(
            "<HHH", data[position + 28 : position + 34]
        )
        offset = struct.unpack("<I", data[position + 42 : position + 46])[0]
        name_end = position + 46 + name_length
        name = data[position + 46 : name_end].decode("utf-8", "replace")
        plain, compressed, offset = _zip64_values(
            data[name_end : name_end + extra_length], plain, compressed, offset
        )
        entries[name] = Entry(method, compressed, plain, offset, crc)
        position = name_end + extra_length + comment_length
    return entries


def extract(entry: Entry, data: bytes) -> bytes:
    """A file's plain bytes from a read starting at its local header, checked by CRC-32."""
    if data[:4] != b"PK\x03\x04":
        raise BadArchive("no local file header where the directory said")
    name_length, extra_length = struct.unpack("<HH", data[26:30])
    start = 30 + name_length + extra_length
    body = data[start : start + entry.compressed]
    if len(body) != entry.compressed:
        raise BadArchive("read shorter than the directory said")
    try:
        if entry.method == STORED:
            plain = body
        elif entry.method == DEFLATED:
            plain = zlib.decompress(body, -15)
        elif entry.method == BZIP2:
            plain = bz2.decompress(body)
        else:
            raise BadArchive(f"unsupported zip method {entry.method}")
    except (OSError, ValueError, zlib.error) as exc:
        raise BadArchive("file does not decompress") from exc
    if zlib.crc32(plain) != entry.crc or len(plain) != entry.plain:
        raise BadArchive("file differs from the archive's CRC-32 or size")
    return plain


class Meter:
    """The one owner of a run's requests: count, spacing and stop conditions."""

    def __init__(self, state: Path) -> None:
        self._log = state / "fma-meter.jsonl"
        self._stop_file = state / "fma-STOPPED"
        self._last_start = 0.0
        self.requests = 0
        self.wire_bytes = 0
        if self._log.exists():
            for line in self._log.read_text(encoding="utf-8").splitlines():
                self.requests += 1
                self.wire_bytes += cast(int, json.loads(line)["bytes"])

    def check(self) -> None:
        if self._stop_file.exists():
            raise Stopped(self._stop_file.read_text(encoding="utf-8"))

    def stop(self, reason: str) -> None:
        self._stop_file.write_text(reason, encoding="utf-8")
        raise Stopped(reason)

    def pace(self) -> None:
        wait = self._last_start + MIN_SPACING_SECONDS - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._last_start = time.monotonic()

    def record(self, kind: str, status: int | None, count: int, target: str) -> None:
        self.requests += 1
        self.wire_bytes += count
        entry = {"t": time.time(), "kind": kind, "status": status, "bytes": count, "target": target}
        with self._log.open("a", encoding="utf-8") as log:
            log.write(json.dumps(entry) + "\n")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: object, **kwargs: object) -> None:
        return None


class Archive:
    """Range reads of the one archive, through the meter."""

    def __init__(self, meter: Meter, url: str = ARCHIVE_URL) -> None:
        self._meter = meter
        self._url = url
        self._opener = urllib.request.build_opener(_NoRedirect)

    def read(self, spec: str, kind: str, target: str) -> tuple[bytes, str, bool]:
        """One range read: its bytes, Content-Range, and whether it arrived in full."""
        self._meter.check()
        self._meter.pace()
        request = urllib.request.Request(
            self._url, headers={"User-Agent": USER_AGENT, "Range": spec}
        )
        body = bytearray()
        status: int | None = None
        content_range = ""
        promised: int | None = None
        try:
            with self._opener.open(request, timeout=READ_TIMEOUT_SECONDS) as response:
                status = response.status
                if status != 206:
                    self._meter.record(kind, status, 0, target)
                    self._meter.stop(f"HTTP {status} on {kind} {target}")
                content_range = response.headers.get("Content-Range", "")
                length = response.headers.get("Content-Length")
                promised = int(length) if length else None
                while chunk := response.read(1 << 16):
                    body += chunk
        except urllib.error.HTTPError as exc:
            exc.close()
            self._meter.record(kind, exc.code, 0, target)
            self._meter.stop(f"HTTP {exc.code} on {kind} {target}")
        except (OSError, http.client.HTTPException) as exc:
            # A dropped transfer: what arrived still counts and can be built on.
            self._meter.record(kind, status, len(body), target)
            print("transfer dropped:", type(exc).__name__, target, flush=True)
            return bytes(body), content_range, False
        self._meter.record(kind, status, len(body), target)
        return bytes(body), content_range, promised is None or len(body) == promised

    def span(self, start: int, end: int, kind: str, target: str) -> bytes:
        """Bytes ``start`` to ``end`` inclusive, resuming a dropped transfer at most twice."""
        data = bytearray()
        resumes = 0
        while start + len(data) <= end:
            piece, _, whole = self.read(f"bytes={start + len(data)}-{end}", kind, target)
            data += piece
            if not whole or not piece:
                resumes += 1
                if resumes > MAX_RESUMES:
                    self._meter.stop(f"transfer kept dropping on {kind} {target}")
        return bytes(data)


def archive_path(track_id: str) -> str:
    digits = f"{int(track_id):06d}"
    return f"fma_full/{digits[:3]}/{digits}.mp3"


def _read_json(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))


def _directory(state: Path, url: str = ARCHIVE_URL) -> None:
    """Read the directory once, keep the chosen files' entries and screen out short ones."""
    meter = Meter(state)
    archive = Archive(meter, url)
    tail, content_range, whole = archive.read(f"bytes=-{TAIL_BYTES}", "directory", "tail")
    if not whole or not content_range:
        meter.stop("the archive's tail did not arrive")
    total = int(content_range.rsplit("/", 1)[1])
    offset, size = directory_location(tail, total)
    if size > MAX_DIRECTORY_BYTES:
        meter.stop(f"directory is {size} bytes, over its limit")
    entries = parse_directory(archive.span(offset, offset + size - 1, "directory", "central"))
    rows = cast(dict[str, prepare.Listing], _read_json(state / "chosen-rows.json"))
    chosen = cast(list[str], _read_json(state / "chosen.json"))
    kept: list[str] = []
    saved: dict[str, object] = {}
    rejected: dict[str, str] = {}
    for track_id in chosen:
        entry = entries.get(archive_path(track_id))
        if entry is None:
            rejected[track_id] = "not in the archive"
            continue
        share = entry.plain / prepare.expected_bytes(rows[track_id])
        if share < prepare.MIN_SIZE_SHARE:
            rejected[track_id] = f"file is {share:.0%} of its predicted size"
        elif entry.compressed > prepare.MAX_FILE_BYTES or entry.plain > prepare.MAX_FILE_BYTES:
            rejected[track_id] = "file over 20 MB"
        else:
            kept.append(track_id)
            saved[track_id] = asdict(entry)
    (state / "fma-archive.json").write_text(json.dumps({"bytes": total}), encoding="utf-8")
    (state / "fma-entries.json").write_text(json.dumps(saved), encoding="utf-8")
    (state / "fetch-order.json").write_text(json.dumps(kept), encoding="utf-8")
    (state / "size-rejections.json").write_text(json.dumps(rejected, indent=1), encoding="utf-8")
    print(
        f"archive {total} bytes, {len(entries)} entries; kept {len(kept)}, rejected {rejected}; "
        f"{meter.requests} requests, {meter.wire_bytes} bytes"
    )


def _audio(state: Path, media: Path, limit: int, url: str = ARCHIVE_URL) -> None:
    """Fetch the next files in order. A finished file is never requested again."""
    meter = Meter(state)
    archive = Archive(meter, url)
    order = cast(list[str], _read_json(state / "fetch-order.json"))
    entries = cast(dict[str, dict[str, int]], _read_json(state / "fma-entries.json"))
    last_byte = cast(dict[str, int], _read_json(state / "fma-archive.json"))["bytes"] - 1
    log = state / "fma-files.jsonl"
    done: dict[str, dict[str, object]] = {}
    if log.exists():
        for line in log.read_text(encoding="utf-8").splitlines():
            saved = cast(dict[str, object], json.loads(line))
            done[str(saved["id"])] = saved
    media.mkdir(parents=True, exist_ok=True)
    plain_total = sum(cast(int, item["size"]) for item in done.values())
    attempted = 0
    for track_id in order[:MAX_FILES]:
        if attempted >= limit:
            break
        if track_id in done or (state / f"fma-bad-{track_id}").exists():
            continue
        entry = Entry(**entries[track_id])
        if plain_total + entry.plain > prepare.MAX_TOTAL_BYTES:
            meter.stop("3 GB limit reached")
        attempted += 1
        end = entry.offset + 30 + LOCAL_HEADER_ROOM + entry.compressed - 1
        data = archive.span(entry.offset, min(end, last_byte), "audio", track_id)
        try:
            plain = extract(entry, data)
        except BadArchive as exc:
            (state / f"fma-bad-{track_id}").write_text(str(exc), encoding="utf-8")
            print("rejected", track_id, exc, flush=True)
            continue
        destination = media / f"{track_id}.mp3"
        partial = destination.with_suffix(".part")
        with partial.open("wb") as output:
            output.write(plain)
            output.flush()
            os.fsync(output.fileno())
        partial.replace(destination)
        finished: dict[str, object] = {
            "id": track_id,
            "size": len(plain),
            "sha256": hashlib.sha256(plain).hexdigest(),
        }
        with log.open("a", encoding="utf-8") as output:
            output.write(json.dumps(finished) + "\n")
        done[track_id] = finished
        plain_total += len(plain)
        print("saved", track_id, len(plain), flush=True)
    print(
        f"{len(done)} files saved, {plain_total} bytes of audio, "
        f"{meter.requests} requests, {meter.wire_bytes} bytes transferred"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    directory = commands.add_parser("directory")
    audio = commands.add_parser("audio")
    audio.add_argument("--media", type=Path, required=True)
    audio.add_argument("--limit", type=int, required=True)
    for command in (directory, audio):
        command.add_argument("--state", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "directory":
            _directory(args.state)
        else:
            _audio(args.state, args.media, args.limit)
    except Stopped as exc:
        print("STOPPED:", exc, flush=True)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
