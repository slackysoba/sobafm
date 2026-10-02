from discord.opus import Encoder

from sobafm.pcm import BYTES_PER_SECOND, FRAME_BYTES, SILENCE, FrameSplitter


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
