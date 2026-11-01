"""Closed header/count contract for the owned MPEG1 Layer III 44.1 kHz mono case."""

from __future__ import annotations

from dataclasses import dataclass

from .probe import ProbeError

INPUT_SAMPLES = 44_100 * 120
MAX_FILE_BYTES = 2_500_000
SCALE = 32_768
STEP = 48_000 * SCALE // 44_100
SYNTHESIS_DELAY = 529
BITRATES = (0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320)


@dataclass
class MonoPcmCheck:
    """Check forced-stereo output; carry bytes stay in the prepaid copy reservation."""

    scratch: memoryview
    pending: int = 0
    frames: int = 0
    nonzero: bool = False
    failed: bool = False
    finished: bool = False

    def __post_init__(self) -> None:
        if self.scratch.readonly or self.scratch.format != "B" or self.scratch.shape != (4,):
            raise ValueError("four writable reserved byte positions are required")

    def check_frame(self, payload: memoryview, offset: int) -> None:
        if payload[offset] != payload[offset + 2] or payload[offset + 1] != payload[offset + 3]:
            self.failed = True
            raise ProbeError("mono source did not produce identical stereo channels")
        self.nonzero = self.nonzero or payload[offset] != 0 or payload[offset + 1] != 0
        self.frames += 1

    def feed(self, payload: memoryview) -> None:
        if self.failed or self.finished:
            raise ProbeError("PCM predicate is already terminal")
        if payload.format != "B" or payload.ndim != 1:
            raise ValueError("PCM observation must be a byte view")
        position = 0
        if self.pending:
            amount = min(4 - self.pending, len(payload))
            with payload[:amount] as source:
                self.scratch[self.pending : self.pending + amount] = source
            self.pending += amount
            position += amount
            if self.pending == 4:
                self.check_frame(self.scratch, 0)
                self.pending = 0
        whole_end = position + (len(payload) - position) // 4 * 4
        for offset in range(position, whole_end, 4):
            self.check_frame(payload, offset)
        if whole_end < len(payload):
            self.pending = len(payload) - whole_end
            with payload[whole_end:] as source:
                self.scratch[: self.pending] = source

    def finish(self, expected_frames: int) -> None:
        if (
            self.failed
            or self.finished
            or self.pending
            or not self.nonzero
            or expected_frames <= 0
            or self.frames != expected_frames
        ):
            self.failed = True
            raise ProbeError("incomplete, silent or mismatched owned mono PCM output")
        self.finished = True


@dataclass(frozen=True)
class VbrShape:
    file_bytes: int
    audio_frames: int
    audio_bitrates_kbps: tuple[int, ...]
    encoder_delay: int
    encoder_padding: int
    input_samples: int
    output_frames: int

    @property
    def pcm_bytes(self) -> int:
        return self.output_frames * 4


def converted_endpoint(samples: int) -> int:
    """mpg123 1.33.7 ntom.c: initial half-scale phase and floored integer step."""
    if samples < 0:
        raise ValueError("negative gapless endpoint")
    return (SCALE // 2 + samples * STEP) // SCALE


def gapless_output_frames(frames: int, delay: int, padding: int) -> int:
    if frames <= 0 or not 0 <= delay <= 4095 or not 0 <= padding <= 4095:
        raise ProbeError("invalid owned gapless frame/trim values")
    if frames * 1152 - delay - padding != INPUT_SAMPLES:
        raise ProbeError("gapless header differs from the known owned input count")
    # frame.c adds the synthesis delay before converting each endpoint separately.
    begin = converted_endpoint(delay + SYNTHESIS_DELAY)
    end = converted_endpoint(frames * 1152 - padding + SYNTHESIS_DELAY)
    return end - begin


def inspect_vbr_fixture(data: bytes) -> VbrShape:
    """Reject tags, other formats, truncation, false VBR and inconsistent gapless counts."""
    if not 0 < len(data) <= MAX_FILE_BYTES:
        raise ProbeError("owned VBR fixture size exceeds the reviewed bound")
    offset = count = advertised_frames = advertised_bytes = delay = padding = 0
    bitrates: set[int] = set()
    while offset < len(data):
        if len(data) - offset < 4:
            raise ProbeError("incomplete owned MPEG frame header")
        bits = int.from_bytes(data[offset : offset + 4], "big")
        bitrate_index = bits >> 12 & 15
        if (
            bits >> 21 != 2047
            or bits >> 19 & 3 != 3
            or bits >> 17 & 3 != 1
            or bits >> 16 & 1 != 1
            or not 1 <= bitrate_index <= 14
            or bits >> 10 & 3 != 0
            or bits >> 6 & 3 != 3
        ):
            raise ProbeError("expected unprotected MPEG1 Layer III 44.1 kHz mono headers")
        bitrate = BITRATES[bitrate_index]
        frame_bytes = 144_000 * bitrate // 44_100 + (bits >> 9 & 1)
        if offset + frame_bytes > len(data):
            raise ProbeError("truncated owned MPEG frame")
        if count == 0:
            # Four-byte header and seventeen-byte MPEG1 mono side information.
            xing = 21
            lame = xing + 120
            if frame_bytes < lame + 24 or data[xing : xing + 4] != b"Xing":
                raise ProbeError("complete owned Xing/LAME header required")
            flags = int.from_bytes(data[xing + 4 : xing + 8], "big")
            if flags != 15 or data[lame : lame + 9] != b"Lavf lame":
                raise ProbeError("owned encoder header identity or fields differ")
            advertised_frames = int.from_bytes(data[xing + 8 : xing + 12], "big")
            advertised_bytes = int.from_bytes(data[xing + 12 : xing + 16], "big")
            trim = int.from_bytes(data[lame + 21 : lame + 24], "big")
            delay, padding = trim >> 12, trim & 4095
        else:
            bitrates.add(bitrate)
        count += 1
        offset += frame_bytes
    if count != advertised_frames + 1 or advertised_bytes != len(data):
        raise ProbeError("owned Xing frame/byte totals differ from the complete file")
    if len(bitrates) < 2:
        raise ProbeError("owned VBR case needs varying audio-frame bitrates")
    return VbrShape(
        len(data),
        advertised_frames,
        tuple(sorted(bitrates)),
        delay,
        padding,
        INPUT_SAMPLES,
        gapless_output_frames(advertised_frames, delay, padding),
    )
