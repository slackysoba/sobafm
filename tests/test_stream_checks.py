"""Synthetic frame/tag/sample boundaries; no native decoding or source activity."""

from __future__ import annotations

import struct
from collections.abc import Callable
from dataclasses import replace

import pytest

from sobafm.catalog import Recording
from sobafm.jamendo_admission import parse_listing
from sobafm.jamendo_journal import Outcome
from sobafm.pcm import FRAME_BYTES, READY_BYTES, SAMPLE_RATE, PcmBankPool
from sobafm.stream_checks import (
    Envelope,
    Notices,
    PcmScreen,
    Reason,
    StreamCheckError,
    mp3_frame,
)
from tests.test_jamendo_catalog import row
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


# Issue #237: every refusal names a fixed reason and, inside a frame, its validated ID.


def frames(*items: tuple[bytes, bytes], version: int = 4) -> bytes:
    """One ID3 tag holding several frames."""
    body = b"".join(
        name + (synchsafe(len(value)) if version == 4 else len(value).to_bytes(4)) + b"\0\0" + value
        for name, value in items
    )
    return b"ID3" + bytes((version, 0, 0)) + synchsafe(len(body)) + body


def bound_recording() -> Recording:
    """Admission's own projection of an album track with genre tags."""
    listing = parse_listing(
        row(
            "123",
            album_name="Night Songs",
            album_id="9",
            musicinfo={"tags": {"genres": ["jazz", "lounge"], "instruments": ["piano"]}},
        )
    )
    return listing.recording


def refusal(payload: bytes, record: Recording | None = None) -> StreamCheckError:
    parser = Envelope(record or candidate().recording)
    with pytest.raises(StreamCheckError) as error:
        consume(parser, payload, 16384)
        parser.finish()
    return error.value


def text(value: str) -> bytes:
    return b"\3" + value.encode()


def id3(body: bytes) -> bytes:
    return b"ID3\4\0\0" + synchsafe(len(body)) + body


SIZE_HIGH_BIT = id3(b"TIT2\x80\0\0\1\0\0x")
BAD_NAME = id3(b"tit2" + synchsafe(1) + b"\0\0x")
BAD_FLAGS = id3(b"TIT2" + synchsafe(1) + b"\x80\0x")
BAD_SIZE = id3(b"TIT2" + synchsafe(9) + b"\0\0x")
SHORT_FRAME = id3(b"TIT2\0")
LONG_TEXT = b"\3" + b"a" * 8192
TRAILER = b"TAG" + bytes(125)


@pytest.mark.parametrize(
    "payload,outcome,reason,frame_id",
    [
        # Tag and frame header structure.
        (b"ID3\4\0\0\x80\0\0\0", Outcome.NOTICE, Reason.STRUCTURE, None),
        (SIZE_HIGH_BIT, Outcome.NOTICE, Reason.STRUCTURE, "TIT2"),
        (b"ID3\4\1\0" + synchsafe(0), Outcome.NOTICE, Reason.STRUCTURE, None),
        (b"APETAGEX", Outcome.NOTICE, Reason.STRUCTURE, None),
        (b"ID3\4\0\0" + synchsafe(0), Outcome.NOTICE, Reason.STRUCTURE, None),
        (b"ID3\3\0\0" + synchsafe(16) + bytes(16), Outcome.NOTICE, Reason.STRUCTURE, None),
        (SHORT_FRAME, Outcome.NOTICE, Reason.STRUCTURE, None),
        (BAD_NAME, Outcome.NOTICE, Reason.STRUCTURE, None),
        (BAD_FLAGS, Outcome.NOTICE, Reason.STRUCTURE, "TIT2"),
        (BAD_SIZE, Outcome.NOTICE, Reason.STRUCTURE, "TIT2"),
        (tag(b"TIT2", b"\3x")[:-1], Outcome.NOTICE, Reason.TRUNCATED, None),
        # Frame identifiers.
        (tag(b"TZZZ", text("x")), Outcome.NOTICE, Reason.FRAME_ID, "TZZZ"),
        (tag(b"WZZZ", b"x"), Outcome.NOTICE, Reason.FRAME_ID, "WZZZ"),
        (tag(b"SYLT", text("x")), Outcome.NOTICE, Reason.FRAME_ID, "SYLT"),
        # Frame bodies.
        (tag(b"TCOP", LONG_TEXT), Outcome.NOTICE, Reason.BUDGET, "TCOP"),
        (tag(b"TXXX", text("unterminated")), Outcome.NOTICE, Reason.STRUCTURE, "TXXX"),
        (tag(b"COMM", b"\3e1gx\0y"), Outcome.NOTICE, Reason.STRUCTURE, "COMM"),
        (tag(b"COMM", text("engunterminated")), Outcome.NOTICE, Reason.STRUCTURE, "COMM"),
        (tag(b"TIPL", text("role")), Outcome.NOTICE, Reason.STRUCTURE, "TIPL"),
        (tag(b"TCOP", b"\5x"), Outcome.NOTICE, Reason.ENCODING, "TCOP"),
        (tag(b"TCOP", b"\3x", version=3), Outcome.NOTICE, Reason.ENCODING, "TCOP"),
        (tag(b"TCOP", b"\3\xff"), Outcome.NOTICE, Reason.ENCODING, "TCOP"),
        (tag(b"APIC", b"\3im"), Outcome.NOTICE, Reason.PICTURE, "APIC"),
        (tag(b"APIC", b"\3image/png"), Outcome.NOTICE, Reason.PICTURE, "APIC"),
        (tag(b"APIC", b"\3image/gif\0\3\0x"), Outcome.NOTICE, Reason.PICTURE, "APIC"),
        # Text reconciliation.
        (tag(b"TCOP", text("a\x07b")), Outcome.NOTICE, Reason.CONTROL_CHAR, "TCOP"),
        (tag(b"TCOP", text("All rights reserved")), Outcome.NOTICE, Reason.CONTRADICTION, "TCOP"),
        (tag(b"TCOP", text("https://[invalid")), Outcome.NOTICE, Reason.MALFORMED_LINK, "TCOP"),
        (
            tag(b"WCOP", b"https://creativecommons.org/about"),
            Outcome.NOTICE,
            Reason.LICENSE_LINK,
            "WCOP",
        ),
        (tag(b"TCOP", text("new notice")), Outcome.NOTICE, Reason.UNBOUND_TEXT, "TCOP"),
        (tag(b"TIT2", text("another title")), Outcome.NOTICE, Reason.UNBOUND_TEXT, "TIT2"),
        (tag(b"TALB", text("Copyright 2019 Label")), Outcome.NOTICE, Reason.NOTICE_TEXT, "TALB"),
        (tag(b"TCON", text("(CR)")), Outcome.NOTICE, Reason.NOTICE_TEXT, "TCON"),
        (tag(b"TALB", text("a" * 161)), Outcome.NOTICE, Reason.BUDGET, "TALB"),
        # MP3 envelope.
        (b"x" * 4, Outcome.FRAMING, Reason.MP3_HEADER, None),
        (mp3() + mp3(rate=1), Outcome.FRAMING, Reason.FORMAT_CHANGE, None),
        (TRAILER, Outcome.FRAMING, Reason.TAG_POSITION, None),
        (mp3() + TRAILER + mp3(), Outcome.FRAMING, Reason.AFTER_END, None),
        (mp3()[:-1], Outcome.FRAMING, Reason.TRUNCATED, None),
        (b"", Outcome.FRAMING, Reason.TRUNCATED, None),
    ],
)
def test_each_envelope_refusal_names_its_reason_and_frame(
    payload: bytes, outcome: Outcome, reason: Reason, frame_id: str | None
) -> None:
    error = refusal(payload)
    assert (error.outcome, error.reason, error.frame_id) == (outcome, reason, frame_id)
    assert (error.digest is not None) == (outcome == Outcome.NOTICE)


def test_budget_refusals_name_their_reason() -> None:
    title = tag(b"TIT2", text(candidate().recording.title))
    error = refusal(title * 257)
    assert (error.reason, error.frame_id) == (Reason.BUDGET, "TIT2")
    error = refusal(b"ID3\4\0\0" + synchsafe(131_063))
    assert (error.reason, error.frame_id) == (Reason.BUDGET, None)
    record = candidate().recording
    assert record.credits is not None
    record = replace(record, credits=replace(record.credits, notices=("a" * 1024, "b")))
    notice = tag(b"TCOP", b"\3" + b"a" * 1024)
    error = refusal(notice * 32 + tag(b"TCOP", text("b")), record)
    assert (error.reason, error.frame_id) == (Reason.BUDGET, "TCOP")


def test_notices_refuse_unbindable_recordings_and_empty_frames() -> None:
    record = candidate().recording
    assert record.credits is not None
    with pytest.raises(StreamCheckError) as error:
        Notices(replace(record, credits=replace(record.credits, license_url=None)))
    assert error.value.reason == Reason.MISSING_LICENSE
    with pytest.raises(StreamCheckError) as error:
        Notices(replace(record, credits=replace(record.credits, notices=("a" * 40_000,))))
    assert error.value.reason == Reason.BUDGET
    # The envelope refuses a zero-size frame first; the notice check keeps its own.
    with pytest.raises(StreamCheckError) as error:
        Notices(record).frame("TIT2", memoryview(b""), 4)
    assert (error.value.reason, error.value.frame_id) == (Reason.STRUCTURE, "TIT2")
    # A name that is not a validated frame ID is never reported.
    with pytest.raises(StreamCheckError) as error:
        Notices(record).frame("ti\n2", memoryview(b"x"), 4)
    assert (error.value.reason, error.value.frame_id) == (Reason.FRAME_ID, None)


def test_feed_after_finish_and_short_header_name_their_reason() -> None:
    parser = Envelope(candidate().recording)
    consume(parser, mp3())
    parser.finish()
    with pytest.raises(StreamCheckError) as error:
        consume(parser, mp3())
    assert (error.value.outcome, error.value.reason) == (Outcome.FRAMING, Reason.AFTER_END)
    with pytest.raises(StreamCheckError) as error:
        mp3_frame(memoryview(b"xyz"))
    assert error.value.reason == Reason.MP3_HEADER


def test_pcm_refusals_name_their_reason() -> None:
    def reason(action: Callable[[PcmScreen], None], duration: int = 100) -> Reason:
        with pytest.raises(StreamCheckError) as error:
            action(PcmScreen(duration))
        return error.value.reason

    def repeat(frame: memoryview, count: int) -> Callable[[PcmScreen], None]:
        def add(screen: PcmScreen) -> None:
            for _ in range(count):
                screen.add(frame)

        return add

    def over(screen: PcmScreen) -> None:
        screen.bytes = screen.limit
        screen.add(memoryview(b"\x20\0" * (FRAME_BYTES // 2)))

    silent = memoryview(bytes(FRAME_BYTES))
    clipped = memoryview(struct.pack("<19h", *([32767] * 19)) + bytes(FRAME_BYTES - 38))
    assert reason(lambda s: s.add(memoryview(bytes(4)))) == Reason.PCM_FRAME
    assert reason(repeat(silent, 1501)) == Reason.SILENCE
    assert reason(repeat(clipped, 51)) == Reason.CLIPPING
    assert reason(over) == Reason.PCM_LIMIT
    assert reason(lambda s: s.finish(FRAME_BYTES - 4)) == Reason.PCM_ALIGNMENT
    assert reason(lambda s: s.finish(FRAME_BYTES + 1)) == Reason.PCM_ALIGNMENT
    assert reason(lambda s: s.finish(s.limit + 4)) == Reason.PCM_LIMIT
    assert reason(lambda s: s.finish(FRAME_BYTES)) == Reason.DURATION
    assert reason(lambda s: s.finish(SAMPLE_RATE * 4), 1) == Reason.PCM_COUNT


DESCRIPTIVE_V4 = (
    (b"TALB", text("Night Songs")),
    (b"TCON", text("Jazz\0Lounge")),
    (b"TDRC", text("2019-05-01")),
    (b"TRCK", text("3/12")),
    (b"TLEN", text("180000")),
    (b"TENC", text("Jamendo encoder")),
    (b"TSSE", text("LAME 3.100")),
)
DESCRIPTIVE_V3 = (
    (b"TALB", b"\1" + "Night Songs".encode("utf-16")),
    (b"TCON", b"\0(8)Jazz"),
    (b"TYER", b"\x002019"),
    (b"TRCK", b"\x003"),
    (b"TLEN", b"\x00180000"),
    (b"TENC", b"\x00Lavf58.29.100"),
)


@pytest.mark.parametrize(
    "items,version", [(DESCRIPTIVE_V4, 4), (DESCRIPTIVE_V3, 3)], ids=["v2.4", "v2.3"]
)
def test_descriptive_frames_matching_bound_data_pass(
    items: tuple[tuple[bytes, bytes], ...], version: int
) -> None:
    record = bound_recording()
    assert "album: Night Songs" in record.details and "genres: jazz" in record.details
    credits = record.credits
    payload = frames(*items, version=version)
    stream = payload + mp3() + payload + mp3()
    parser = Envelope(record)
    assert consume(parser, stream) == stream
    parser.finish()
    # Still counted against the frame budget; heard credits are untouched.
    assert parser.notices.frames == 2 * len(items)
    assert parser.notices.text_bytes > 0
    assert record.credits is credits


def test_descriptive_frames_count_toward_existing_budgets() -> None:
    album = tag(b"TALB", text("Night Songs"))
    record = bound_recording()
    parser = Envelope(record)
    consume(parser, album * 256 + mp3(), 16384)
    parser.finish()
    assert parser.notices.frames == 256
    assert parser.notices.text_bytes == 256 * len("Night Songs")
    error = refusal(album * 257, record)
    assert (error.reason, error.frame_id) == (Reason.BUDGET, "TALB")


def test_neutral_unbound_descriptive_data_passes() -> None:
    # The fixture binds no album or genres; neutral descriptive values still pass.
    payload = frames(
        (b"TALB", text("Some Other Album")),
        (b"TCON", text("Ambient")),
        (b"TPOS", text("1/2")),
        (b"TBPM", text("96")),
    )
    parser = Envelope(candidate().recording)
    consume(parser, payload + mp3())
    parser.finish()
    trailer = b"TAG" + bytes(60) + b"Neutral Album".ljust(30, b"\0") + b"2019" + bytes(31)
    assert len(trailer) == 128
    parser = Envelope(candidate().recording)
    consume(parser, mp3() + trailer)
    parser.finish()


@pytest.mark.parametrize(
    "name,value,reason",
    [
        (b"TALB", "All Rights Reserved", Reason.CONTRADICTION),
        (b"TCON", "https://creativecommons.org/licenses/by-nc/4.0/", Reason.CONTRADICTION),
        (b"TENC", "FMA-Limited", Reason.CONTRADICTION),
        (b"TYER", "creativecommons.org/licenses/by-nd/3.0", Reason.CONTRADICTION),
        (b"TALB", "Night\x07Songs", Reason.CONTROL_CHAR),
        (b"TRCK", "3\x00\x1b/12", Reason.CONTROL_CHAR),
        (b"TSSE", "https://creativecommons.org/about", Reason.LICENSE_LINK),
        (b"TALB", "Night Songs (c) Label", Reason.NOTICE_TEXT),
        (b"TCON", "Jazz © Label", Reason.NOTICE_TEXT),
        (b"TENC", "www.example.com", Reason.NOTICE_TEXT),
        (b"TLEN", "licensed via label", Reason.NOTICE_TEXT),
        (b"TALB", "Preview", Reason.NOTICE_TEXT),
        (b"TCON", "CR", Reason.NOTICE_TEXT),
    ],
)
def test_descriptive_frames_with_contradictions_still_refuse(
    name: bytes, value: str, reason: Reason
) -> None:
    for record in (bound_recording(), candidate().recording):
        for prefix in (b"", mp3()):
            error = refusal(prefix + tag(name, text(value)), record)
            assert (error.outcome, error.reason, error.frame_id) == (
                Outcome.NOTICE,
                reason,
                name.decode(),
            )
            assert error.digest is not None


def test_id3v1_descriptive_fields_still_check_contradictions() -> None:
    def trailer(album: bytes) -> bytes:
        return b"TAG" + bytes(60) + album.ljust(30, b"\0") + b"2019" + bytes(31)

    error = refusal(mp3() + trailer(b"All rights reserved"))
    assert (error.reason, error.frame_id) == (Reason.CONTRADICTION, None)
    error = refusal(mp3() + trailer(b"Copyright Label"))
    assert (error.reason, error.frame_id) == (Reason.NOTICE_TEXT, None)
