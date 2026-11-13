"""Local media for admitted recordings and the decode-backed playback producer.

The catalog snapshot carries metadata and credits but no audio location. A
separate operator-prepared media manifest binds each playable recording key to
one local file, its exact size and SHA-256, and a finite PCM output bound. File
names are relative to the manifest's directory and may not leave it.

``MediaProducer`` implements the adapter's ``Producer`` with one
``DecodeProducer`` per track. It fills the bank the adapter already leased, runs
the blocking decode in a worker thread, and returns or raises only after the
decoder process, its threads and descriptors have actually settled, including
after cancellation. The adapter keeps the lease and releases it.

``stream`` dispatches an immutable Jamendo Candidate to an injected checked
stream factory. Its refreshed preparation and final bank/journal release remain
explicit operations for station integration; a Recording cannot supply them.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import cast

from sobafm.catalog import Recording, RecordingKey, Source
from sobafm.decode import DecodeInput, DecodeProducer, DecoderCommand, DecodeReport
from sobafm.jamendo_admission import Candidate
from sobafm.jamendo_stream import JamendoStream, StreamFactory
from sobafm.pcm import BYTES_PER_SECOND, FRAME_BYTES, PcmBank

log = logging.getLogger(__name__)

MAX_MEDIA_BYTES = 256_000
MAX_MEDIA_ENTRIES = 1_000
MAX_FILE_NAME = 255
MAX_INPUT_BYTES = 1_000_000_000
# Four hours of PCM: a hard ceiling on any one recording's output bound.
MAX_PCM_BYTES = 4 * 3_600 * BYTES_PER_SECOND
# How long one settlement wait lasts before the owner checks again.
RELEASE_POLL_SECONDS = 1.0

type Media = Mapping[RecordingKey, DecodeInput]


class MediaInputError(ValueError):
    """The media manifest is invalid; messages never contain values."""


class MediaMissingError(LookupError):
    """A selected recording has no entry in the media manifest."""


def _reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result = dict(pairs)
    if len(result) != len(pairs):
        raise MediaInputError("duplicate key")
    return result


def _fields(value: object, names: set[str]) -> dict[str, object]:
    if not isinstance(value, dict):
        raise MediaInputError("expected an object")
    data = cast(dict[str, object], value)
    if data.keys() != names:
        raise MediaInputError("missing or unsupported fields")
    return data


def _integer(value: object, maximum: int) -> int:
    if type(value) is not int or not 1 <= value <= maximum:
        raise MediaInputError("expected a bounded positive integer")
    return value


def _text(value: object, maximum: int) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= maximum:
        raise MediaInputError("expected bounded text")
    if not value.isprintable() or value != value.strip():
        raise MediaInputError("text contains control characters or whitespace edges")
    return value


def _file(value: object, root: Path) -> Path:
    name = _text(value, MAX_FILE_NAME)
    relative = PurePosixPath(name)
    if relative.is_absolute() or "\\" in name or any(p in ("", ".", "..") for p in name.split("/")):
        raise MediaInputError("file must be a plain path inside the manifest directory")
    return root.joinpath(*relative.parts)


def parse_media(payload: str, root: Path) -> dict[RecordingKey, DecodeInput]:
    """Validate a media manifest whose relative file names resolve under ``root``.

    The manifest is ``{"version": 1, "recordings": [entry, ...]}`` and each entry
    is ``{"key": {"source", "recording_id"}, "file", "size", "sha256",
    "max_pcm_bytes"}``. ``max_pcm_bytes`` is a whole number of frames.
    """
    try:
        document = json.loads(payload, object_pairs_hook=_reject_duplicates)
    except json.JSONDecodeError as exc:
        raise MediaInputError("manifest is not JSON") from exc
    data = _fields(document, {"version", "recordings"})
    if type(data["version"]) is not int or data["version"] != 1:
        raise MediaInputError("unsupported manifest version")
    entries = data["recordings"]
    if not isinstance(entries, list) or len(cast(list[object], entries)) > MAX_MEDIA_ENTRIES:
        raise MediaInputError("recordings must be a bounded list")
    media: dict[RecordingKey, DecodeInput] = {}
    for entry in cast(list[object], entries):
        item = _fields(entry, {"key", "file", "size", "sha256", "max_pcm_bytes"})
        key_fields = _fields(item["key"], {"source", "recording_id"})
        source = _text(key_fields["source"], 16)
        if source not in tuple(Source):
            raise MediaInputError("unknown source")
        key = RecordingKey(Source(source), _text(key_fields["recording_id"], 256))
        if key in media:
            raise MediaInputError("duplicate recording key")
        digest = item["sha256"]
        if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            raise MediaInputError("sha256 must be 64 lowercase hex digits")
        limit = _integer(item["max_pcm_bytes"], MAX_PCM_BYTES)
        if limit % FRAME_BYTES:
            raise MediaInputError("max_pcm_bytes must be a whole number of frames")
        media[key] = DecodeInput(
            path=_file(item["file"], root),
            size=_integer(item["size"], MAX_INPUT_BYTES),
            sha256=digest,
            pcm_limit=limit,
        )
    return media


def load_media(path: Path) -> dict[RecordingKey, DecodeInput]:
    """Read a bounded UTF-8 manifest file; entries resolve beside it."""
    with path.open("rb") as file:
        payload = file.read(MAX_MEDIA_BYTES + 1)
    if len(payload) > MAX_MEDIA_BYTES:
        raise MediaInputError("manifest exceeds byte limit")
    return parse_media(payload.decode("utf-8"), path.parent)


class MediaProducer:
    """The adapter's ``Producer``: one stock decoder per track into the leased bank."""

    def __init__(
        self,
        media: Media,
        *,
        command: DecoderCommand | None = None,
        stream_factory: StreamFactory | None = None,
    ) -> None:
        self._media = media
        self._command = command
        self._stream_factory = stream_factory

    def stream(self, candidate: Candidate, bank: PcmBank) -> JamendoStream:
        """Prepare a source candidate through its owned refreshed-stream interface.

        Integration must use this handle's refreshed Prepared before switching;
        a Recording alone cannot authorize a source request or stream open.
        """
        if candidate.recording.key.source != Source.JAMENDO or self._stream_factory is None:
            raise MediaMissingError("recording has no stream producer")
        return self._stream_factory(candidate, bank)

    async def produce(self, recording: Recording, bank: PcmBank) -> None:
        source = self._media.get(recording.key)
        if source is None:
            raise MediaMissingError("recording has no media entry")
        decode = DecodeProducer(source, command=self._command, bank=bank)
        work = asyncio.ensure_future(asyncio.to_thread(_decode, decode))
        try:
            report = await asyncio.shield(work)
        except asyncio.CancelledError:
            decode.cancel()
            while not work.done():  # own the decode until it has actually settled
                try:
                    await asyncio.shield(work)
                except asyncio.CancelledError:
                    decode.cancel()
                except Exception:
                    break
            if not work.cancelled() and work.exception() is not None:
                log.error("decode worker failed: %s", type(work.exception()).__name__)
            raise
        if report.failure is not None:
            log.warning("decode failed: %s", report.failure)
        if report.cleanup != "clean":
            log.error("decoder cleanup missed its deadline")


def _decode(decode: DecodeProducer) -> DecodeReport:
    """Run one decode to settlement on a worker thread."""
    decode.start()
    report = decode.finish()
    while not decode.release(RELEASE_POLL_SECONDS):
        pass  # an expired cleanup stays owned until the process and threads settle
    return report
