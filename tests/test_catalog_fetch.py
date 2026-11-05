"""The FMA fetch tool against a loopback fake archive. It never contacts a real host."""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import sys
import threading
import zipfile
import zlib
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import ModuleType
from typing import ClassVar, cast

import pytest

from sobafm.catalog_prepare import Listing


def load_tool() -> ModuleType:
    path = Path(__file__).resolve().parents[1] / "evaluations/catalog/fma_fetch.py"
    spec = importlib.util.spec_from_file_location("fma_fetch", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses look their module up here
    spec.loader.exec_module(module)
    return module


TOOL = load_tool()
STOPPED = cast(type[Exception], TOOL.Stopped)
BAD_ARCHIVE = cast(type[Exception], TOOL.BadArchive)


def build_archive(files: dict[str, bytes], method: int = zipfile.ZIP_BZIP2) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=method) as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    return buffer.getvalue()


class Fake(BaseHTTPRequestHandler):
    archive: ClassVar[bytes] = b""
    status: ClassVar[int | None] = None
    drops_left: ClassVar[int] = 0
    requests: ClassVar[list[str]] = []
    agents: ClassVar[set[str]] = set()

    def do_GET(self) -> None:
        Fake.requests.append(self.headers.get("Range", ""))
        Fake.agents.add(self.headers.get("User-Agent", ""))
        if Fake.status is not None:
            self.send_error(Fake.status)
            return
        spec = self.headers["Range"].removeprefix("bytes=")
        total = len(Fake.archive)
        if spec.startswith("-"):
            start, end = max(0, total - int(spec[1:])), total - 1
        else:
            first, last = spec.split("-")
            start, end = int(first), min(int(last), total - 1)
        body = Fake.archive[start : end + 1]
        self.send_response(206)
        self.send_header("Content-Range", f"bytes {start}-{end}/{total}")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if Fake.drops_left > 0:
            Fake.drops_left -= 1
            self.wfile.write(body[: len(body) // 2])
            self.wfile.flush()
            self.connection.close()
            return
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return


@pytest.fixture
def server() -> Iterator[str]:
    Fake.status = None
    Fake.drops_left = 0
    Fake.requests = []
    Fake.agents = set()
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Fake)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_port}/fma_full.zip"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join()


@pytest.fixture(autouse=True)
def no_spacing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(TOOL, "MIN_SPACING_SECONDS", 0.0)


def listing_row(bit_rate: int = 8_000, seconds: int = 100) -> Listing:
    return {"track.bit_rate": str(bit_rate), "track.duration": str(seconds)}


def prepare_state(state: Path, ids: list[str], rows: dict[str, Listing] | None = None) -> None:
    (state / "chosen.json").write_text(json.dumps(ids))
    chosen = rows if rows is not None else {track_id: listing_row() for track_id in ids}
    (state / "chosen-rows.json").write_text(json.dumps(chosen))


def audio_for(track_id: str, size: int = 100_000) -> bytes:
    return hashlib.sha256(track_id.encode()).digest() * (size // 32)


def full_archive(*ids: str) -> dict[str, bytes]:
    return {TOOL.archive_path(track_id): audio_for(track_id) for track_id in ids}


def test_archive_paths_use_the_datasets_layout() -> None:
    assert TOOL.archive_path("42377") == "fma_full/042/042377.mp3"
    assert TOOL.archive_path("2") == "fma_full/000/000002.mp3"


@pytest.mark.parametrize("method", [zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED, zipfile.ZIP_BZIP2])
def test_directory_and_extract_read_a_real_zip(method: int) -> None:
    files = {"a/one.mp3": b"one" * 1_000, "a/two.mp3": b"two" * 2_000}
    archive = build_archive(files, method)
    offset, size = TOOL.directory_location(archive[-4_096:], len(archive))
    entries = TOOL.parse_directory(archive[offset : offset + size])
    assert sorted(entries) == sorted(files)
    entry = entries["a/two.mp3"]
    assert entry.method == method and entry.plain == 6_000
    start = entry.offset
    assert (
        TOOL.extract(entry, archive[start : start + 30 + 1_024 + entry.compressed])
        == files["a/two.mp3"]
    )


def test_a_changed_file_fails_its_crc_check() -> None:
    archive = build_archive({"a/one.mp3": b"one" * 1_000}, zipfile.ZIP_STORED)
    offset, size = TOOL.directory_location(archive, len(archive))
    entry = TOOL.parse_directory(archive[offset : offset + size])["a/one.mp3"]
    bad = bytearray(archive[entry.offset : entry.offset + 30 + 1_024 + entry.compressed])
    bad[60] ^= 0xFF
    with pytest.raises(BAD_ARCHIVE, match="CRC"):
        TOOL.extract(entry, bytes(bad))


def test_garbage_is_not_a_directory_or_a_file() -> None:
    with pytest.raises(BAD_ARCHIVE):
        TOOL.directory_location(b"not a zip", 9)
    entry = TOOL.Entry(0, 3, 3, 0, zlib.crc32(b"abc"))
    with pytest.raises(BAD_ARCHIVE):
        TOOL.extract(entry, b"not a header at all and long enough to read" * 2)


def test_the_directory_stage_keeps_whole_files_and_rejects_short_ones(
    server: str, tmp_path: Path
) -> None:
    files = full_archive("1", "2")
    files[TOOL.archive_path("3")] = audio_for("3", 50_000)  # half the predicted size
    Fake.archive = build_archive(files)
    prepare_state(tmp_path, ["1", "2", "3", "4"])
    TOOL._directory(tmp_path, server)  # pyright: ignore[reportPrivateUsage]
    assert json.loads((tmp_path / "fetch-order.json").read_text()) == ["1", "2"]
    rejected = json.loads((tmp_path / "size-rejections.json").read_text())
    assert rejected["3"] == "file is 50% of its predicted size"
    assert rejected["4"] == "not in the archive"


def fetch_ready(server: str, state: Path, *ids: str) -> None:
    Fake.archive = build_archive(full_archive(*ids))
    prepare_state(state, list(ids))
    TOOL._directory(state, server)  # pyright: ignore[reportPrivateUsage]
    Fake.requests = []


def test_files_are_saved_exactly_with_a_plain_client(server: str, tmp_path: Path) -> None:
    fetch_ready(server, tmp_path, "1", "2")
    media = tmp_path / "media"
    TOOL._audio(tmp_path, media, 10, server)  # pyright: ignore[reportPrivateUsage]
    assert (media / "1.mp3").read_bytes() == audio_for("1")
    assert (media / "2.mp3").read_bytes() == audio_for("2")
    assert not list(media.glob("*.part"))
    assert len(Fake.requests) == 2 and all(r.startswith("bytes=") for r in Fake.requests)
    assert Fake.agents == {TOOL.USER_AGENT}
    saved = [json.loads(line) for line in (tmp_path / "fma-files.jsonl").read_text().splitlines()]
    assert saved[0] == {
        "id": "1",
        "size": 100_000 // 32 * 32,
        "sha256": hashlib.sha256(audio_for("1")).hexdigest(),
    }


def test_a_batch_stops_at_its_limit_and_the_next_skips_what_it_holds(
    server: str, tmp_path: Path
) -> None:
    fetch_ready(server, tmp_path, "1", "2", "3")
    media = tmp_path / "media"
    TOOL._audio(tmp_path, media, 2, server)  # pyright: ignore[reportPrivateUsage]
    assert sorted(path.name for path in media.iterdir()) == ["1.mp3", "2.mp3"]
    Fake.requests = []
    TOOL._audio(tmp_path, media, 2, server)  # pyright: ignore[reportPrivateUsage]
    assert len(Fake.requests) == 1
    assert (media / "3.mp3").exists()


@pytest.mark.parametrize("status", [403, 429, 500])
def test_any_refusal_stops_the_run_and_stays_stopped(
    server: str, tmp_path: Path, status: int
) -> None:
    fetch_ready(server, tmp_path, "1", "2")
    Fake.status = status
    media = tmp_path / "media"
    with pytest.raises(STOPPED, match=str(status)):
        TOOL._audio(tmp_path, media, 10, server)  # pyright: ignore[reportPrivateUsage]
    assert len(Fake.requests) == 1
    assert not list(media.glob("*.mp3"))
    Fake.status = None
    Fake.requests = []
    with pytest.raises(STOPPED):
        TOOL._audio(tmp_path, media, 10, server)  # pyright: ignore[reportPrivateUsage]
    assert Fake.requests == []


def test_a_dropped_transfer_resumes_and_the_file_is_still_exact(
    server: str, tmp_path: Path
) -> None:
    fetch_ready(server, tmp_path, "1")
    Fake.drops_left = 1
    media = tmp_path / "media"
    TOOL._audio(tmp_path, media, 10, server)  # pyright: ignore[reportPrivateUsage]
    assert (media / "1.mp3").read_bytes() == audio_for("1")
    assert len(Fake.requests) == 2


def test_a_transfer_that_keeps_dropping_stops_the_run(server: str, tmp_path: Path) -> None:
    fetch_ready(server, tmp_path, "1")
    Fake.drops_left = 100
    with pytest.raises(STOPPED, match="kept dropping"):
        TOOL._audio(tmp_path, tmp_path / "media", 10, server)  # pyright: ignore[reportPrivateUsage]
    assert len(Fake.requests) == 3
    assert not (tmp_path / "media" / "1.mp3").exists()


def test_the_count_survives_between_runs(server: str, tmp_path: Path) -> None:
    fetch_ready(server, tmp_path, "1")
    TOOL._audio(tmp_path, tmp_path / "media", 10, server)  # pyright: ignore[reportPrivateUsage]
    meter = TOOL.Meter(tmp_path)
    assert meter.requests == 3  # the tail, the directory and the file
    assert meter.wire_bytes > 0


def test_a_file_that_fails_its_check_is_set_aside_and_the_run_continues(
    server: str, tmp_path: Path
) -> None:
    fetch_ready(server, tmp_path, "1", "2")
    entries = json.loads((tmp_path / "fma-entries.json").read_text())
    entries["1"]["crc"] = entries["1"]["crc"] ^ 1
    (tmp_path / "fma-entries.json").write_text(json.dumps(entries))
    media = tmp_path / "media"
    TOOL._audio(tmp_path, media, 10, server)  # pyright: ignore[reportPrivateUsage]
    assert not (media / "1.mp3").exists() and (media / "2.mp3").exists()
    assert "CRC" in (tmp_path / "fma-bad-1").read_text()


def test_recorded_limits_match_the_accepted_decision() -> None:
    assert TOOL.MAX_FILES == 230
    assert TOOL.MIN_SPACING_SECONDS in (0.0, 2.0)  # zero only under this file's fixture
    assert TOOL.MAX_RESUMES == 2


def test_the_tool_reads_only_the_dataset_host() -> None:
    assert TOOL.ARCHIVE_URL == "https://os.unil.cloud.switch.ch/fma/fma_full.zip"
    source = (Path(__file__).resolve().parents[1] / "evaluations/catalog/fma_fetch.py").read_text()
    assert source.count("https://") == 1
    assert "freemusicarchive.org" in source  # only in its statement that no request goes there
    assert "https://freemusicarchive" not in source
