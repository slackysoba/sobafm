import pytest
from discord.opus import Encoder

from sobafm.pcm import (
    BYTES_PER_SECOND,
    FRAME_BYTES,
    MIME_TYPE,
    SILENCE,
    FrameSplitter,
    matches_mime_type,
)


def test_frames_match_what_discord_encodes() -> None:
    assert FRAME_BYTES == Encoder.FRAME_SIZE
    assert BYTES_PER_SECOND == Encoder.SAMPLING_RATE * Encoder.SAMPLE_SIZE
    assert len(SILENCE) == FRAME_BYTES
    assert not any(SILENCE)


def test_splits_chunks_into_whole_frames() -> None:
    splitter = FrameSplitter()

    assert len(splitter.split(bytes(FRAME_BYTES * 3))) == 3


def test_carries_partial_frames_over() -> None:
    splitter = FrameSplitter()
    audio = bytes(i % 251 for i in range(FRAME_BYTES * 2))

    first = splitter.split(audio[: FRAME_BYTES + 100])
    second = splitter.split(audio[FRAME_BYTES + 100 :])

    assert [len(frame) for frame in first + second] == [FRAME_BYTES, FRAME_BYTES]
    assert b"".join(first + second) == audio
    assert splitter.split(b"") == []


@pytest.mark.parametrize(
    ("mime_type", "expected"),
    [
        (MIME_TYPE, True),
        ("AUDIO/L16; Channels=2; Rate=48000", True),
        ('audio/l16; rate="48000"; channels=2', True),
        ("audio/l16;rate=48000;channels=2;endianness=little", True),
        ("audio/l16;rate=24000;channels=2", False),
        ("audio/l16;rate=48000;channels=1", False),
        ("audio/l16", False),
        ("audio/wav;rate=48000;channels=2", False),
        ("", False),
    ],
    ids=[
        "exact",
        "case and order",
        "quoted",
        "other parameter",
        "rate",
        "channels",
        "no parameters",
        "type",
        "empty",
    ],
)
def test_matches_its_mime_type_as_mime_compares_them(mime_type: str, *, expected: bool) -> None:
    assert matches_mime_type(mime_type) is expected
