"""Offline synthetic header and owned-generator tests; never run a codec."""

from __future__ import annotations

import hashlib
import io
import struct
import unittest
from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock, patch

from . import owned_mp3_decode as decoder
from . import owned_mp3_fixture as generator
from .decoder_startup import Child, CleanupResult
from .mp3_vbr_contract import (
    INPUT_SAMPLES,
    STEP,
    MonoPcmCheck,
    converted_endpoint,
    gapless_output_frames,
    inspect_vbr_fixture,
)
from .owned_mp3_fixture import CBR, CHUNK_BYTES, VBR, encoder_argv, fill_pcm
from .probe import ProbeError

FORMAT_LINE = "MPEG 1.0 L III vbr 44100 mono"
DIAGNOSTICS = (
    b"Note: forcing output encoding s16\nPlaying MPEG stream 1 of 1: - ...\n"
    + FORMAT_LINE.encode()
    + b"\n[1:59] Decoding of - finished.\n"
)


class DecodeSchedule:
    """Deterministic native pipe outcomes; the actual decode/cleanup orchestration runs."""

    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = iter(chunks)
        self.now = 0.0
        self.launched = self.released = self.output_eof = self.error_eof = False
        self.error_sent = False
        self.gate_read = False
        self.checks: list[MonoPcmCheck] = []

    def fork(self) -> int:
        self.launched = True
        return 42

    def processes(self) -> set[int]:
        return {42} if self.launched and not self.released else set()

    def select(
        self, reads: list[int], writes: list[int], errors: list[int], timeout: float
    ) -> tuple[list[int], list[int], list[int]]:
        self.now += 0.01
        return reads, writes, []

    def read(self, fd: int, size: int) -> bytes:
        if fd == 16:
            first = not self.gate_read
            self.gate_read = True
            return b"R" if first else b""
        if fd != 14:
            raise AssertionError("unowned descriptor")
        if self.error_sent:
            self.error_eof = True
            return b""
        self.error_sent = True
        return DIAGNOSTICS[:size]

    def readv(self, fd: int, destinations: list[memoryview]) -> int:
        if fd != 12:
            raise AssertionError("unowned PCM descriptor")
        chunk = next(self.chunks, b"")
        self.output_eof = not chunk
        destinations[0][: len(chunk)] = chunk
        return len(chunk)

    def reap(self, child: Child) -> None:
        if self.output_eof and self.error_eof:
            child.exit_code = 0

    def cleanup(self, child: Child, deadline: float) -> CleanupResult:
        child.exit_code = 0
        self.released = True
        return CleanupResult(self.now <= deadline, [])

    def check(self, scratch: memoryview) -> MonoPcmCheck:
        result = MonoPcmCheck(scratch)
        self.checks.append(result)
        return result

    def clock(self) -> float:
        return self.now

    @staticmethod
    def write(fd: int, data: bytes | memoryview) -> int:
        return len(data)

    @staticmethod
    def text(path: Path) -> str:
        return "core" if path.name == "core_pattern" else "0"

    def install(self, stack: ExitStack) -> decoder.DecodeFixture:
        def reap(child: Child) -> None:
            self.reap(child)

        fixture = decoder.DecodeFixture(
            Path("/fixtures/synthetic.mp3"),
            4,
            hashlib.sha256(b"data").hexdigest(),
            12,
            16,
            FORMAT_LINE,
            True,
        )
        for name, value in (
            ("BANK_BYTES", 16),
            ("PCM_BYTES", 32),
            ("COPY_BYTES", 40),
            ("CHUNK_BYTES", 8),
            ("READY_BYTES", 8),
        ):
            stack.enter_context(patch.object(decoder, name, value))
        for target, name, effect in (
            (decoder.os, "fork", self.fork),
            (decoder.os, "read", self.read),
            (decoder.os, "readv", self.readv),
            (decoder.os, "write", self.write),
            (decoder.select, "select", self.select),
            (decoder.time, "monotonic", self.clock),
            (decoder, "native_processes", self.processes),
            (decoder, "cleanup", self.cleanup),
            (decoder, "MonoPcmCheck", self.check),
            (decoder, "read_text", self.text),
        ):
            stack.enter_context(patch.object(target, name, side_effect=effect))
        for name in ("close", "kill", "set_blocking"):
            stack.enter_context(patch.object(decoder.os, name))
        stack.enter_context(patch.object(Child, "reap", reap))
        stack.enter_context(patch.object(decoder.os, "sysconf", return_value=4096))
        stack.enter_context(
            patch.object(decoder.os, "pipe", side_effect=[(n, n + 1) for n in range(10, 20, 2)])
        )
        stack.enter_context(patch.object(Path, "iterdir", return_value=iter([Path("1")])))
        stack.enter_context(patch.object(Path, "open", return_value=io.BytesIO(b"data")))
        for name, value in (
            ("verify_native_files", None),
            ("verify_child", {}),
            ("verify_core_limit", None),
            ("read_start_identity", ("T", 123)),
            ("receive_banner", bytearray(decoder.BANNER)),
            ("stopped_native_gate", {}),
            ("counters", {"oom": 0, "oom_kill": 0}),
            ("emit", None),
        ):
            stack.enter_context(patch.object(decoder, name, return_value=value))
        return fixture


def framed_input(varying_audio: bool = True) -> bytearray:
    """Structural fixture with zero compressed bodies: header parser data, never audio."""
    frames: list[bytearray] = []
    for index in range(4596):
        bitrate = 80 if index == 0 or varying_audio and index % 2 else 64
        bits = 0xFFE00000 | 3 << 19 | 1 << 17 | 1 << 16 | (5 if bitrate == 64 else 6) << 12 | 3 << 6
        frame = bytearray(144_000 * bitrate // 44_100)
        struct.pack_into(">I", frame, 0, bits)
        frames.append(frame)
    first = frames[0]
    first[21:25] = b"Xing"
    struct.pack_into(">III", first, 25, 15, 4595, sum(map(len, frames)))
    first[141:150] = b"Lavf lame"
    first[162:165] = ((576 << 12) | 864).to_bytes(3, "big")
    return bytearray().join(frames)


class VbrContracts(unittest.TestCase):
    def test_bound_snapshot_and_source_diagnostics_do_not_accept_original_cbr_evidence(
        self,
    ) -> None:
        binding = decoder.DecodeFixture(
            Path("/fixtures/synthetic.mp3"),
            3,
            hashlib.sha256(b"abc").hexdigest(),
            23_039_448,
            3_000_000,
            FORMAT_LINE,
            True,
        )
        decoder.validate_fixture_partitions(binding)
        with (
            patch.object(Path, "open", return_value=io.BytesIO(b"abc")),
            memoryview(bytearray(16)) as reserve,
        ):
            decoder.load_fixture_snapshot(reserve, binding)
        decoder.validate_diagnostics(decoder.BANNER + DIAGNOSTICS, FORMAT_LINE)
        with self.assertRaises(ProbeError):
            decoder.validate_diagnostics(decoder.BANNER + DIAGNOSTICS)
        for changed in (
            replace(binding, file_bytes=3_000_001),
            replace(binding, output_copy_offset=decoder.COPY_BYTES - decoder.CHUNK_BYTES - 3),
            replace(binding, sha256="unbound"),
            replace(binding, pcm_bytes=23_039_449),
        ):
            with self.assertRaises(ProbeError):
                decoder.validate_fixture_partitions(changed)

    def test_actual_decode_requires_every_mono_sample_and_releases_carry_on_failure(self) -> None:
        valid = b"\x01\0\x01\0" * 3
        for pcm, accepted in ((valid, True), (b"\0" * 12, False), (b"\x01\0\x02\0" * 3, False)):
            schedule = DecodeSchedule([pcm[:3], pcm[3:8], pcm[8:]])
            with self.subTest(pcm=pcm), ExitStack() as stack:
                fixture = schedule.install(stack)
                result = decoder.decode_fixture(fixture)
            self.assertEqual(result["verdict"] == "owned_fixture_decode_pass", accepted)
            self.assertEqual(result["mono_output_verified"], accepted)
            self.assertEqual(result["cleanup_debt"], False)
            self.assertEqual(result["retained_fds"], [])
            self.assertTrue(result["parent_payload_views_released"])
            self.assertTrue(schedule.released)
            if accepted:
                self.assertEqual(result["mono_frames_checked"], 3)
                self.assertEqual(result["pcm_output_sha256"], hashlib.sha256(valid).hexdigest())
                self.assertEqual(result["ready_buffered_bytes"], 8)
            with self.assertRaises(ValueError):
                _ = schedule.checks[0].scratch.obj

    def test_decoder_partial_pipe_failure_does_not_fork_or_forget_unreleased_descriptor(
        self,
    ) -> None:
        schedule = DecodeSchedule([])
        with ExitStack() as stack:
            fixture = schedule.install(stack)
            stack.enter_context(
                patch.object(decoder.os, "pipe", side_effect=[(10, 11), OSError("cap")])
            )
            close = stack.enter_context(
                patch.object(decoder.os, "close", side_effect=[None, OSError("busy")])
            )
            result = decoder.decode_fixture(fixture)
        self.assertFalse(schedule.launched)
        self.assertEqual([call.args[0] for call in close.call_args_list], [10, 11])
        self.assertEqual(result["verdict"], "incomplete")
        self.assertEqual(result["retained_fds"], [11])
        self.assertTrue(result["cleanup_debt"])
        self.assertFalse(result["cleanup_deadline_met"])

    def test_recipe_keeps_fixed_encoder_and_changes_only_owned_format_controls(self) -> None:
        cbr, vbr = encoder_argv(CBR), encoder_argv(VBR)
        self.assertEqual(CBR.raw_bytes, 23_040_000)
        self.assertEqual(VBR.raw_bytes, 10_584_000)
        self.assertEqual(VBR.sample_frames, INPUT_SAMPLES)
        self.assertEqual(vbr[vbr.index("-ar") + 1], "44100")
        self.assertEqual(vbr[vbr.index("-ac") + 1], "1")
        self.assertEqual(vbr[vbr.index("-q:a") + 1], "4")
        self.assertNotIn("-b:a", vbr)
        self.assertEqual(cbr[cbr.index("-b:a") + 1], "128000")
        self.assertEqual(vbr[-1], str(VBR.path))
        with self.assertRaises(ValueError):
            encoder_argv(replace(VBR, rate=22050))

    def test_generator_chunk_boundaries_tone_segments_and_final_mono_sample(self) -> None:
        buffer = bytearray(CHUNK_BYTES)
        for first in (0, 220499, 220500, 440999, 441000, 661499, 661500, INPUT_SAMPLES - 2):
            count = fill_pcm(first, buffer, VBR)
            self.assertEqual(count, min(CHUNK_BYTES, (INPUT_SAMPLES - first) * 2))
            for offset in (0, count // 2 - 1):
                frame = first + offset
                period = (100, 137, 200, 333)[frame // 220500 % 4]
                phase = frame % period
                expected = min(phase, period - phase) * 32000 // period - 8000
                self.assertEqual(struct.unpack_from("<h", buffer, offset * 2)[0], expected)
        self.assertEqual(fill_pcm(INPUT_SAMPLES, buffer, VBR), 0)
        with self.assertRaises(ValueError):
            fill_pcm(INPUT_SAMPLES + 1, buffer, VBR)

    def test_converter_counts_each_gapless_endpoint_with_integer_phase(self) -> None:
        self.assertEqual(STEP, 35665)
        self.assertEqual(converted_endpoint(1105), 1203)
        self.assertEqual(converted_endpoint(5293105), 5761065)
        self.assertEqual(gapless_output_frames(4595, 576, 864), 5759862)
        self.assertEqual(gapless_output_frames(4595, 576, 864) * 4, 23039448)
        self.assertNotEqual(gapless_output_frames(4595, 576, 864), 48000 * 120)
        for args in ((4594, 576, 864), (4595, 575, 864), (4595, 576, 863), (0, 0, 0)):
            with self.assertRaises(ProbeError):
                gapless_output_frames(*args)
        with self.assertRaises(ValueError):
            converted_endpoint(-1)

    def test_complete_varying_frames_and_gapless_count_are_all_required(self) -> None:
        data = framed_input()
        shape = inspect_vbr_fixture(bytes(data))
        self.assertEqual(shape.audio_frames, 4595)
        self.assertEqual(shape.audio_bitrates_kbps, (64, 80))
        self.assertEqual(shape.encoder_delay, 576)
        self.assertEqual(shape.encoder_padding, 864)
        self.assertEqual(shape.pcm_bytes, 23039448)
        for offset, replacement in (
            (21, b"Info"),
            (25, b"\x00\x00\x00\x07"),
            (29, (4594).to_bytes(4, "big")),
            (141, b"Not owned"),
            (162, ((576 << 12) | 863).to_bytes(3, "big")),
        ):
            altered = data.copy()
            altered[offset : offset + len(replacement)] = replacement
            with self.subTest(offset=offset), self.assertRaises(ProbeError):
                inspect_vbr_fixture(bytes(altered))
        for value in (b"", bytes(data[:-1]), bytes(data) + b"x", b"ID3" + bytes(data)):
            with self.assertRaises(ProbeError):
                inspect_vbr_fixture(value)

    def test_other_rate_channel_crc_layer_or_free_format_is_rejected(self) -> None:
        data = framed_input()
        original = int.from_bytes(data[261:265], "big")
        for changed in (
            original | 1 << 10,
            original & ~(3 << 6),
            original & ~(1 << 16),
            original & ~(3 << 17),
            original & ~(15 << 12),
            original | 15 << 12,
        ):
            altered = data.copy()
            altered[261:265] = changed.to_bytes(4, "big")
            with self.subTest(header=changed), self.assertRaises(ProbeError):
                inspect_vbr_fixture(bytes(altered))

    def test_xing_frame_bitrate_cannot_disguise_constant_audio_bitrates(self) -> None:
        with self.assertRaisesRegex(ProbeError, "varying audio-frame"):
            inspect_vbr_fixture(bytes(framed_input(varying_audio=False)))

    def test_mono_check_accepts_every_partial_frame_boundary_without_pcm_copy(self) -> None:
        data = struct.pack("<hhhhhhhh", -32768, -32768, 0, 0, 1234, 1234, 32767, 32767)
        for split in range(len(data) + 1):
            reserve = bytearray(12)
            with (
                memoryview(reserve) as pool,
                memoryview(data) as payload,
                pool[4:8] as scratch,
            ):
                check = MonoPcmCheck(scratch)
                with payload[:split] as first, payload[split:] as second:
                    check.feed(first)
                    check.feed(second)
                check.finish(4)
                self.assertEqual(check.frames, 4)
                self.assertEqual(check.pending, 0)
                self.assertIs(check.scratch.obj, reserve)
            # A retained, unreleased view would prevent resizing the original reservation.
            reserve.extend(b"x")

    def test_mono_mismatch_is_sticky_across_split_sample_and_later_completion(self) -> None:
        with memoryview(bytearray(4)) as scratch, memoryview(b"\x01\x02\x01\x03") as data:
            check = MonoPcmCheck(scratch)
            with data[:3] as first, data[3:] as final:
                check.feed(first)
                with self.assertRaisesRegex(ProbeError, "identical stereo"):
                    check.feed(final)
                with self.assertRaisesRegex(ProbeError, "already terminal"):
                    check.feed(first)
                with self.assertRaises(ProbeError):
                    check.finish(1)
            self.assertEqual(check.frames, 0)

    def test_mono_output_rejects_silence_partial_frame_and_wrong_complete_count(self) -> None:
        for raw, frames in ((b"\0" * 8, 2), (b"\x01\0\x01", 1), (b"\x01\0\x01\0", 2)):
            with memoryview(bytearray(4)) as scratch, memoryview(raw) as data:
                check = MonoPcmCheck(scratch)
                check.feed(data)
                with self.assertRaises(ProbeError):
                    check.finish(frames)
        with memoryview(b"xxxx") as readonly, self.assertRaises(ValueError):
            MonoPcmCheck(readonly)

    def test_generator_pipe_setup_failure_releases_prior_descriptors_or_retains_debt(self) -> None:
        for failed_close in (False, True):
            with self.subTest(failed_close=failed_close), ExitStack() as stack:
                stack.enter_context(patch.object(Path, "exists", return_value=False))
                stack.enter_context(patch.object(Path, "is_dir", return_value=True))
                stack.enter_context(
                    patch.object(Path, "iterdir", return_value=iter([Path("task")]))
                )
                stack.enter_context(patch.object(Path, "open", return_value=MagicMock()))
                fingerprint = MagicMock()
                fingerprint.hexdigest.return_value = generator.FFMPEG_SHA256
                stack.enter_context(
                    patch.object(generator.hashlib, "file_digest", return_value=fingerprint)
                )
                stack.enter_context(
                    patch.object(generator.os, "pipe", side_effect=[(11, 12), OSError("pipe cap")])
                )
                fork = stack.enter_context(patch.object(generator.os, "fork"))
                close = stack.enter_context(
                    patch.object(
                        generator.os,
                        "close",
                        side_effect=[None, OSError("busy")] if failed_close else None,
                    )
                )
                result = generator.generate_fixture(VBR)
                fork.assert_not_called()
                self.assertEqual([call.args[0] for call in close.call_args_list], [11, 12])
                self.assertEqual(result["verdict"], "incomplete")
                self.assertEqual(result["cleanup_debt"], failed_close)
                self.assertEqual(result["retained_fds"], [12] if failed_close else [])
                self.assertTrue(result["parent_payload_views_released"])


if __name__ == "__main__":
    unittest.main()
