"""Synthetic frame/tag/sample boundaries; no native decoding or source activity."""

from __future__ import annotations

import struct
from dataclasses import replace

import pytest

from sobafm.jamendo_journal import Outcome
from sobafm.pcm import FRAME_BYTES, READY_BYTES, SAMPLE_RATE, PcmBankPool
from sobafm.stream_checks import Envelope, PcmScreen, StreamCheckError, mp3_frame
from tests.test_jamendo_journal import candidate


def mp3(
    *, version: int = 3, bitrate: int = 9, rate: int = 0, padding: int = 0, mono: bool = False
) -> bytes:
    bits = (
        (0x7FF << 21)
        | (version << 19)
        | (1 << 17)
        | (1 << 16)
        | (bitrate << 12)
        | (rate << 10)
        | (padding << 9)
        | ((3 if mono else 0) << 6)
    )
    header = bits.to_bytes(4)
    size = mp3_frame(memoryview(header)).length
    return header + bytes(size - 4)


def synchsafe(value: int) -> bytes:
    return bytes((value >> 21 & 127, value >> 14 & 127, value >> 7 & 127, value & 127))


def tag(name: bytes, value: bytes, *, version: int = 4, footer: bool = False) -> bytes:
    frame = (
        name + (synchsafe(len(value)) if version == 4 else len(value).to_bytes(4)) + b"\0\0" + value
    )
    header = b"ID3" + bytes((version, 0, 16 if footer else 0)) + synchsafe(len(frame))
    return header + frame + (b"3DI" + header[3:] if footer else b"")


def consume(parser: Envelope, payload: bytes, chunk: int = 7) -> bytes:
    output = bytearray()
    for start in range(0, len(payload), chunk):
        with memoryview(payload)[start : start + chunk] as data:
            for checked in parser.feed(data):
                output.extend(checked)
    return bytes(output)


@pytest.mark.parametrize("version", [0, 2, 3])
@pytest.mark.parametrize("rate", [0, 1, 2])
def test_standard_frames_fragmented_vbr_padding(version: int, rate: int) -> None:
    payload = mp3(version=version, rate=rate) + mp3(
        version=version, rate=rate, bitrate=10, padding=1
    )
    parser = Envelope(candidate().recording)
    assert consume(parser, payload, 1) == payload
    parser.finish()
    assert parser.frames == 2 and parser.complete


@pytest.mark.parametrize("suffix", [b"x", mp3()[:-1], mp3(version=2), mp3(rate=1), mp3(mono=True)])
def test_junk_partial_and_incompatible_frames(suffix: bytes) -> None:
    parser = Envelope(candidate().recording)
    with pytest.raises(StreamCheckError) as error:
        consume(parser, mp3() + suffix)
        parser.finish()
    assert error.value.outcome == Outcome.FRAMING


@pytest.mark.parametrize("bits", [0xFFFB0064, 0xFFFBF064, 0xFFEB9064, 0xFFFD9064, 0xFFFB9C64])
def test_free_reserved_and_wrong_layer(bits: int) -> None:
    with pytest.raises(StreamCheckError):
        mp3_frame(memoryview(bits.to_bytes(4)))


@pytest.mark.parametrize("version", [3, 4])
def test_equivalent_leading_and_later_notices(version: int) -> None:
    record = candidate().recording
    assert record.credits is not None
    text = record.credits.notices[-1]
    raw = b"\1" + text.encode("utf-16")
    tags = tag(b"TCOP", raw, version=version, footer=version == 4)
    payload = tags + mp3() + tags + mp3()
    parser = Envelope(record)
    assert consume(parser, payload) == payload
    parser.finish()
    assert parser.notices.frames == 2


@pytest.mark.parametrize(
    "field,value",
    [
        (b"TCOP", b"\3new notice"),
        (b"TPE1", b"\3someone else"),
        (b"TIT2", b"\3another title"),
        (b"WCOP", b"https://creativecommons.org/licenses/by/3.0/us/"),
        (b"SYLT", b"\3unknown"),
        (b"XXXX", b"unknown"),
        (b"COMM", b"\3engunterminated"),
        (b"TXXX", b"\3unterminated"),
        (b"TCOP", b"\3\xff"),
        (b"TCOP", b"\3" + b"a" * 8192),
    ],
)
def test_notice_changes_and_unreadable_structures(field: bytes, value: bytes) -> None:
    for prefix in (b"", mp3()):
        parser = Envelope(candidate().recording)
        with pytest.raises(StreamCheckError) as error:
            consume(parser, prefix + tag(field, value))
        assert error.value.outcome == Outcome.NOTICE
        assert error.value.digest is not None


def test_tag_cumulative_encoded_limit_and_padding() -> None:
    frame = tag(b"TIT2", b"\3" + candidate().recording.title.encode())[10:]
    padding = b"ID3\4\0\0" + synchsafe(131_062) + frame + bytes(131_062 - len(frame))
    parser = Envelope(candidate().recording)
    consume(parser, padding + mp3(), 16384)
    parser.finish()
    parser = Envelope(candidate().recording)
    with pytest.raises(StreamCheckError):
        consume(parser, padding + b"ID3\4\0\0" + synchsafe(0), 16384)


def test_empty_tag_footer_padding_and_malformed_notice_link() -> None:
    frame = tag(b"TIT2", b"\3" + candidate().recording.title.encode())[10:]
    padded_header = b"ID3\4\0\x10" + synchsafe(len(frame) + 1)
    for payload in (
        b"ID3\4\0\0" + synchsafe(0),
        b"ID3\3\0\0" + synchsafe(16) + bytes(16),
        padded_header + frame + b"\0" + b"3DI" + padded_header[3:],
        tag(b"TCOP", b"\3https://[invalid"),
    ):
        with pytest.raises(StreamCheckError) as error:
            consume(Envelope(candidate().recording), payload)
        assert error.value.outcome == Outcome.NOTICE
        assert error.value.digest is not None


def test_short_id3v1_only_at_end() -> None:
    record = replace(
        candidate().recording, title="Title", primary_artist="Artist", creators=("Artist",)
    )
    trailer = b"TAG" + b"Title".ljust(30, b"\0") + b"Artist".ljust(30, b"\0") + bytes(65)
    assert len(trailer) == 128
    parser = Envelope(record)
    consume(parser, mp3() + trailer)
    parser.finish()
    parser = Envelope(record)
    with pytest.raises(StreamCheckError):
        consume(parser, mp3() + trailer + mp3())


def test_picture_description_checked_before_discarding_image() -> None:
    good = tag(b"APIC", b"\3image/png\0\3\0" + b"synthetic-image" * 3000)
    parser = Envelope(candidate().recording)
    consume(parser, good + mp3(), 16384)
    parser.finish()
    with pytest.raises(StreamCheckError):
        consume(Envelope(candidate().recording), tag(b"APIC", b"\3image/png\0\3new credit\0image"))


def test_1500_1501_silence_and_reset() -> None:
    screen = PcmScreen(100)
    silent = memoryview(bytes(FRAME_BYTES))
    for _ in range(1500):
        screen.add(silent)
    with pytest.raises(StreamCheckError) as error:
        screen.add(silent)
    assert error.value.outcome == Outcome.DEFECT
    screen.add(memoryview(b"\x20\0" * (FRAME_BYTES // 2)))
    assert screen.silence == 0


def test_50_51_clipping_exact_sample_threshold() -> None:
    screen = PcmScreen(100)
    clipped = memoryview(struct.pack("<19h", *([32767] * 19)) + bytes(FRAME_BYTES - 38))
    for _ in range(50):
        screen.add(clipped)
    with pytest.raises(StreamCheckError):
        screen.add(clipped)
    screen.add(memoryview(struct.pack("<18h", *([-32768] * 18)) + bytes(FRAME_BYTES - 36)))
    assert screen.clipping == 0


@pytest.mark.parametrize("duration", [1, 5, 6, 180, 86400])
def test_duration_exact_tolerance_and_one_sample(duration: int) -> None:
    screen = PcmScreen(duration)
    tolerance = int(screen.tolerance * SAMPLE_RATE)
    for samples in (duration * SAMPLE_RATE - tolerance, duration * SAMPLE_RATE + tolerance):
        screen.bytes = samples * 4 // FRAME_BYTES * FRAME_BYTES
        screen.finish(samples * 4)
    for samples in (duration * SAMPLE_RATE - tolerance - 1, duration * SAMPLE_RATE + tolerance + 1):
        screen.bytes = samples * 4 // FRAME_BYTES * FRAME_BYTES
        with pytest.raises(StreamCheckError):
            screen.finish(samples * 4)


def test_pcm_alignment_no_padding_and_exact_cap() -> None:
    screen = PcmScreen(1)
    screen.bytes = 50 * FRAME_BYTES
    screen.finish(screen.bytes + 4)
    with pytest.raises(StreamCheckError):
        screen.finish(screen.bytes + 1)
    screen.bytes = screen.limit
    screen.finish(screen.limit)
    with pytest.raises(StreamCheckError):
        screen.finish(screen.limit + 1)


def test_readiness_hold_is_explicit_and_does_not_change_ordinary_banks() -> None:
    pool = PcmBankPool()
    held, ordinary = pool.acquire(), pool.acquire()
    held.hold_readiness()
    for bank in (held, ordinary):
        bank.write(bytes(READY_BYTES))
    assert ordinary.ready_now() and not held.ready_now()
    assert held.read_frame() is None
    held.end_pcm()
    assert not held.ready_now() and held.facts().pcm_valid
    held.allow_readiness()
    assert held.read_frame() == bytes(FRAME_BYTES)
    assert held.release() and ordinary.release()


@pytest.mark.parametrize("duration", [1, 2, 8, 205, 86400])
def test_pcm_caps_use_exact_arithmetic(duration: int) -> None:
    screen = PcmScreen(duration)
    hundredths = min(3000, max(200, duration), 10 * duration)
    assert screen.limit == ((100 * duration + hundredths + 1) // 2) * FRAME_BYTES
    samples = duration * SAMPLE_RATE + hundredths * SAMPLE_RATE // 100
    screen.bytes = samples * 4 // FRAME_BYTES * FRAME_BYTES
    screen.finish(samples * 4)
    with pytest.raises(StreamCheckError):
        screen.finish((samples + 1) * 4)


def test_frame_count_text_cap_and_unsupported_tag_flags() -> None:
    parser = Envelope(candidate().recording)
    notice = tag(b"TIT2", b"\3" + candidate().recording.title.encode())
    consume(parser, notice * 256 + mp3(), 16384)
    parser.finish()
    with pytest.raises(StreamCheckError):
        consume(Envelope(candidate().recording), notice * 257, 16384)
    for header in (b"ID3\2\0\0", b"ID3\4\1\0", b"ID3\4\0\x80", b"ID3\3\0\x40"):
        with pytest.raises(StreamCheckError):
            consume(Envelope(candidate().recording), header + synchsafe(0))


def test_unknown_ape_and_incomplete_notice_fail_closed() -> None:
    for payload in (b"APETAGEX", tag(b"TCOP", b"\3new")[:-1]):
        parser = Envelope(candidate().recording)
        with pytest.raises(StreamCheckError) as error:
            consume(parser, payload)
            parser.finish()
        assert error.value.outcome == Outcome.NOTICE


def test_total_decoded_text_cap_and_one_byte() -> None:
    record = candidate().recording
    assert record.credits is not None
    record = replace(record, credits=replace(record.credits, notices=("a" * 1024, "b")))
    notice = tag(b"TCOP", b"\3" + b"a" * 1024)
    parser = Envelope(record)
    consume(parser, notice * 32 + mp3(), 16384)
    parser.finish()
    assert parser.notices.text_bytes == 32768
    with pytest.raises(StreamCheckError):
        consume(Envelope(record), notice * 32 + tag(b"TCOP", b"\3b"), 16384)


def test_tag_and_frame_handoff_alias_one_bounded_backing() -> None:
    frame = tag(b"TIT2", b"\3" + candidate().recording.title.encode())[10:]
    padding = b"ID3\4\0\0" + synchsafe(131_062) + frame + bytes(131_062 - len(frame))
    payload = padding + mp3() * 500
    parser = Envelope(candidate().recording)
    backing_ids: set[int] = set()
    largest = 0
    for start in range(0, len(payload), 16384):
        for output in parser.feed(memoryview(payload)[start : start + 16384]):
            assert isinstance(output.obj, bytearray)
            assert len(output.obj) == 131072
            backing_ids.add(id(output.obj))
            largest = max(largest, len(output))
    parser.finish()
    assert largest == 131072 and len(backing_ids) == 1
