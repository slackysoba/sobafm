import sys
import tracemalloc

import pytest
from discord.opus import Encoder

from sobafm.audio_budget import PCM_LIMIT, AudioLedger
from sobafm.owned_pcm import OwnedPCM, PCMCapacityError
from sobafm.pcm import (
    BYTES_PER_SECOND,
    FRAME_BYTES,
    MIME_TYPE,
    SILENCE,
    FrameSplitter,
    matches_mime_type,
)


def splitter_fixture() -> FrameSplitter:
    return FrameSplitter(AudioLedger().pcm.storage(PCM_LIMIT))


def test_frames_match_what_discord_encodes() -> None:
    assert FRAME_BYTES == Encoder.FRAME_SIZE
    assert BYTES_PER_SECOND == Encoder.SAMPLING_RATE * Encoder.SAMPLE_SIZE
    assert len(SILENCE) == FRAME_BYTES
    assert not any(SILENCE)


def test_splits_chunks_into_whole_frames() -> None:
    splitter = splitter_fixture()

    assert len(list(splitter.split(bytes(FRAME_BYTES * 3)))) == 3


def test_carries_partial_frames_over() -> None:
    splitter = splitter_fixture()
    audio = bytes(i % 251 for i in range(FRAME_BYTES * 2))

    first = list(splitter.split(audio[: FRAME_BYTES + 100]))
    second = list(splitter.split(audio[FRAME_BYTES + 100 :]))

    assert [len(frame.data) for frame in first + second] == [FRAME_BYTES, FRAME_BYTES]
    assert b"".join(frame.data for frame in first + second) == audio
    assert list(splitter.split(b"")) == []


@pytest.mark.parametrize(
    "size", [0, 1, FRAME_BYTES - 1, FRAME_BYTES, FRAME_BYTES + 1, 1024 * FRAME_BYTES + 7]
)
def test_exact_owned_frames_and_bounded_carry(size: int) -> None:
    splitter = splitter_fixture()
    audio = bytes(range(251)) * (size // 251) + bytes(range(size % 251))
    references = sys.getrefcount(audio)
    frames = list(splitter.split(audio))
    whole = size - size % FRAME_BYTES

    assert all(type(frame.data) is bytes and len(frame.data) == FRAME_BYTES for frame in frames)
    assert b"".join(frame.data for frame in frames) == audio[:whole]
    pending = splitter._pending  # pyright: ignore[reportPrivateUsage]
    carry = pending.data if pending is not None else b""
    assert type(carry) is bytes
    assert carry == audio[whole:]
    assert len(carry) < FRAME_BYTES
    if audio:
        assert carry is not audio
        assert all(frame.data is not audio for frame in frames)
        assert sys.getrefcount(audio) == references


@pytest.mark.parametrize(
    "stride", [1, 3, 17, FRAME_BYTES - 1, FRAME_BYTES, FRAME_BYTES + 1, 2 * FRAME_BYTES + 17]
)
def test_arbitrary_splits_preserve_every_byte_including_the_tail(stride: int) -> None:
    splitter = splitter_fixture()
    audio = bytes(i % 251 for i in range(5 * FRAME_BYTES + 23))
    frames: list[OwnedPCM] = []
    for offset in range(0, len(audio), stride):
        frames.extend(splitter.split(audio[offset : offset + stride]))
        pending = splitter._pending  # pyright: ignore[reportPrivateUsage]
        assert pending is None or len(pending.data) < FRAME_BYTES
    assert b"".join(frame.data for frame in frames) == audio[: 5 * FRAME_BYTES]
    frames.extend(splitter.split(bytes(FRAME_BYTES - 23)))
    assert b"".join(frame.data for frame in frames) == audio + bytes(FRAME_BYTES - 23)


def test_overlap_rejection_does_not_change_the_active_iterator() -> None:
    splitter = splitter_fixture()
    assert list(splitter.split(b"prefix")) == []
    audio = b"a" * (2 * FRAME_BYTES)
    frames = splitter.split(audio)
    assert next(frames).data == b"prefix" + audio[: FRAME_BYTES - 6]
    overlap = splitter.split(b"wrong")
    with pytest.raises(RuntimeError, match="consume or close"):
        next(overlap)
    assert [frame.data for frame in frames] == [b"a" * FRAME_BYTES]
    assert list(overlap) == []
    assert [frame.data for frame in splitter.split(b"b" * (FRAME_BYTES - 6))] == [
        b"a" * 6 + b"b" * (FRAME_BYTES - 6)
    ]


@pytest.mark.parametrize("end", ["close", "throw"])
def test_early_exit_discards_unread_input_and_releases_ownership(end: str) -> None:
    splitter = splitter_fixture()
    assert list(splitter.split(b"prefix")) == []
    audio = b"a" * (1024 * FRAME_BYTES + 19)
    references = sys.getrefcount(audio)
    frames = splitter.split(audio)
    first = next(frames)
    assert first.data == b"prefix" + b"a" * (FRAME_BYTES - 6)
    if end == "close":
        frames.close()
    else:
        with pytest.raises(ValueError, match="interrupted"):
            frames.throw(ValueError("interrupted"))
    assert sys.getrefcount(audio) == references
    if end == "close":
        assert [frame.data for frame in splitter.split(b"b" * FRAME_BYTES)] == [b"b" * FRAME_BYTES]
    else:
        with pytest.raises(PCMCapacityError):
            list(splitter.split(b"b" * FRAME_BYTES))
    assert list(frames) == []  # a stale iterator cannot change later chunks
    assert first.data == b"prefix" + b"a" * (FRAME_BYTES - 6)


def test_unstarted_iterators_consume_in_advancement_order_and_close_preserves_carry() -> None:
    splitter = splitter_fixture()
    assert list(splitter.split(b"prefix")) == []
    later = splitter.split(b"b" * (FRAME_BYTES - 7))
    unused = splitter.split(b"discard")
    unused.close()
    assert list(splitter.split(b"a")) == []
    assert [frame.data for frame in later] == [b"prefixa" + b"b" * (FRAME_BYTES - 7)]
    assert list(unused) == []


def assembly_peak(frame_count: int) -> int:
    # Allocate original input before tracing; consume without retaining outputs.
    # This measures assembly overhead, not the input or a caller's output queue.
    audio = bytes(frame_count * FRAME_BYTES + 17)
    splitter = splitter_fixture()
    assert list(splitter.split(b"prefix")) == []
    tracemalloc.start()
    try:
        count = 0
        previous = None
        frame = None
        for frame in splitter.split(audio):
            assert len(frame.data) == FRAME_BYTES
            if previous is not None:
                previous.release()
            previous = frame.receipt
            count += 1
        frame = None
        if previous is not None:
            previous.release()
        assert count == frame_count
        _, peak = tracemalloc.get_traced_memory()
        return peak
    finally:
        tracemalloc.stop()


def test_assembly_allocation_does_not_grow_with_input_size() -> None:
    small = assembly_peak(32)
    large = assembly_peak(4096)
    assert small < 8 * FRAME_BYTES
    assert large < 8 * FRAME_BYTES
    assert large <= small + 2 * FRAME_BYTES


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
        ("audio/l16;rate=48000;channels=2;note=\ud800\u00e9", False),
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
        "unencodable",
    ],
)
def test_matches_its_mime_type_as_mime_compares_them(mime_type: str, *, expected: bool) -> None:
    assert matches_mime_type(mime_type) is expected
