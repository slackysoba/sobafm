"""Offline FLAC metadata/admission and real pump failure schedules; no codec or fork."""

from __future__ import annotations

import hashlib
import signal
import unittest
from contextlib import ExitStack
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from unittest.mock import patch

from . import flac_contract as flac
from .decoder_startup import Child, CleanupResult
from .owned_flac_decode import FlacBinding, run_bound_decode
from .owned_mp3_decode import (
    DecodeFixture,
    NativeDecoder,
    decode_fixture,
    parse_maps,
    receive_banner,
    stopped_native_gate,
)
from .owned_mp3_fixture import CBR, CHUNK_BYTES, FLAC, encoder_argv, fill_pcm, generate_fixture
from .probe import ProbeError

MODULE = "evaluations.linux_resource.owned_mp3_decode"
MD5 = "12" * 16  # Synthetic parser fixture; never an execution binding.
DECODER = NativeDecoder(
    flac.ARGV, flac.BANNER, ((flac.ARGV[0], "ab" * 32),), flac.validate_diagnostics
)
DONE = b"-: done         \n"


def block(kind: int, data: bytes, *, last: bool = False) -> bytes:
    return bytes([kind | (128 if last else 0)]) + len(data).to_bytes(3, "big") + data


def streaminfo(
    rate: int = 48000,
    channels: int = 2,
    bits: int = 16,
    frames: int = flac.SAMPLE_FRAMES,
    md5: str = MD5,
) -> bytes:
    packed = (rate << 44) | ((channels - 1) << 41) | ((bits - 1) << 36) | frames
    return b"\x10\x00\x10\x00" + bytes(6) + packed.to_bytes(8, "big") + bytes.fromhex(md5)


def comment(value: bytes | None = None) -> bytes:
    vendor = b"ffmpeg"
    data = len(vendor).to_bytes(4, "little") + vendor + int(value is not None).to_bytes(4, "little")
    return data if value is None else data + len(value).to_bytes(4, "little") + value


def inspect(data: bytes, md5: str = MD5) -> flac.FlacHeader:
    with memoryview(data) as payload:
        return flac.inspect_fixture(payload, md5)


class Pump:
    """Inject syscall outcomes around the production pump and its actual finally block."""

    def __init__(self, *, output: bytes = b"abcdefgh", failure: str = "") -> None:
        self.output = output
        self.failure = failure
        self.now = 1.0
        self.accepted = self.output_position = self.error_reads = self.ready_reads = 0
        self.forked = self.cleaned = self.resumed = False
        self.cleaned_deadlines: list[float] = []
        self.read_views: list[memoryview] = []
        self.diagnostics = DONE

    def fork(self) -> int:
        self.forked = True
        return 42

    def members(self) -> set[int]:
        return {42} if self.forked and not self.cleaned else set()

    def reap(self, child: Child) -> None:
        if self.accepted == 3 or self.failure == "early_exit":
            child.exit_code = -signal.SIGTERM if self.failure == "signal" else 0

    def read(self, fd: int, size: int) -> bytes:
        if fd == 16:
            self.ready_reads += 1
            return b"R" if self.ready_reads == 1 else b""
        if fd != 14:
            raise AssertionError("unowned read")
        if self.failure == "stderr_error":
            raise OSError("injected reader failure")
        self.error_reads += 1
        return self.diagnostics if self.error_reads == 1 else b""

    def readv(self, fd: int, views: list[memoryview]) -> int:
        assert fd == 12 and len(views) == 1
        view = views[0]
        self.read_views.append(view)
        count = min(3, len(view), len(self.output) - self.output_position)
        view[:count] = self.output[self.output_position : self.output_position + count]
        self.output_position += count
        return count

    def write(self, fd: int, payload: bytes | memoryview) -> int:
        if fd == 19:
            return 1
        assert fd == 11 and self.resumed
        self.accepted += 1  # Exercise retained partial-write offsets on every input byte.
        return 1

    def select(
        self, reads: list[int], writes: list[int], errors: list[int], timeout: float
    ) -> tuple[list[int], list[int], list[int]]:
        if self.failure == "stdout_stall" and self.accepted == 3:
            self.now = 200.0
            return [], [], []
        return reads, writes, []

    def close(self, fd: int) -> None:
        if self.failure == "fd_debt" and fd == 12:
            raise OSError("injected retained descriptor")

    def kill(self, pid: int, sig: int) -> None:
        assert pid == 42 and sig == signal.SIGCONT
        self.resumed = True

    def cleanup(self, child: Child, deadline: float) -> CleanupResult:
        self.cleaned_deadlines.append(deadline)
        self.cleaned = True
        child.exit_code = child.exit_code if child.exit_code is not None else -signal.SIGTERM
        return CleanupResult(
            self.failure != "cleanup_late", [43] if self.failure == "pid_debt" else []
        )

    def snapshot(self, view: memoryview, fixture: DecodeFixture) -> None:
        assert not self.resumed and self.accepted == 0
        view[:3] = b"abc"

    def validate_input(self, view: memoryview) -> None:
        assert not self.resumed and self.accepted == 0 and bytes(view) == b"abc"
        if self.failure == "metadata":
            raise ProbeError("injected metadata rejection")

    def run(self) -> dict[str, object]:
        def reap(child: Child) -> None:
            self.reap(child)

        def read_text(path: Path) -> str:
            if path.name == "core_pattern":
                return "core"
            return "oom 0\noom_kill 0\n" if path.name == "memory.events" else "0"

        fixture = DecodeFixture(
            Path("/synthetic"),
            3,
            hashlib.sha256(b"abc").hexdigest(),
            8,
            64,
            "",
            pcm_sha256=hashlib.sha256(b"abcdefgh").hexdigest(),
        )
        pipes = iter(tuple(range(i, i + 2)) for i in range(10, 20, 2))
        with ExitStack() as stack:
            values: dict[str, object] = {
                "READY_BYTES": 4,
                "BANK_BYTES": 16,
                "PCM_BYTES": 32,
                "COPY_BYTES": 128,
                "CHUNK_BYTES": 8,
            }
            for name, value in values.items():
                stack.enter_context(patch(f"{MODULE}.{name}", value))
            calls = {
                "os.fork": self.fork,
                "os.read": self.read,
                "os.readv": self.readv,
                "os.write": self.write,
                "os.close": self.close,
                "os.kill": self.kill,
                "select.select": self.select,
                "native_processes": self.members,
                "cleanup": self.cleanup,
                "load_fixture_snapshot": self.snapshot,
                "time.monotonic": lambda: self.now,
            }
            for name, call in calls.items():
                stack.enter_context(patch(f"{MODULE}.{name}", side_effect=call))
            stack.enter_context(patch(f"{MODULE}.os.pipe", side_effect=lambda: next(pipes)))
            stack.enter_context(patch(f"{MODULE}.os.sysconf", return_value=4096))
            stack.enter_context(patch(f"{MODULE}.os.set_blocking"))
            stack.enter_context(patch.object(Path, "iterdir", return_value=iter([Path("1")])))
            stack.enter_context(patch.object(Child, "reap", reap))
            stack.enter_context(patch(f"{MODULE}.verify_native_files"))
            stack.enter_context(patch(f"{MODULE}.verify_core_limit"))
            stack.enter_context(patch(f"{MODULE}.verify_child", return_value={}))
            stack.enter_context(patch(f"{MODULE}.read_start_identity", return_value=("T", 123)))
            stack.enter_context(patch(f"{MODULE}.stopped_native_gate", return_value={}))
            stack.enter_context(
                patch(f"{MODULE}.receive_banner", return_value=bytearray(flac.BANNER))
            )
            stack.enter_context(patch(f"{MODULE}.emit"))
            stack.enter_context(
                patch(
                    f"{MODULE}.read_text",
                    side_effect=read_text,
                )
            )
            return decode_fixture(fixture, decoder=DECODER, validate_input=self.validate_input)


class FlacContractTests(unittest.TestCase):
    def test_fixture_recipe_reuses_cbr_pcm_and_has_no_mp3_options(self) -> None:
        left, right = bytearray(CHUNK_BYTES), bytearray(CHUNK_BYTES)
        for start in (0, 16384, flac.SAMPLE_FRAMES - 1, flac.SAMPLE_FRAMES):
            with self.subTest(start=start):
                self.assertEqual(fill_pcm(start, left, CBR), fill_pcm(start, right, FLAC))
                self.assertEqual(left, right)
        argv = encoder_argv(FLAC)
        input_at = argv.index("-i")
        self.assertEqual(argv[:input_at].count("-fflags"), 1)
        self.assertEqual(argv[input_at + 2 : input_at + 4], ("-fflags", "+bitexact"))
        self.assertEqual(argv.count("-fflags"), 2)
        self.assertEqual(encoder_argv(CBR).count("-fflags"), 1)
        self.assertEqual(argv[argv.index("-c:a") + 1], "flac")
        self.assertEqual(argv[-3:], ("-f", "flac", str(FLAC.path)))
        for option in ("-b:a", "-q:a", "-write_xing", "-id3v2_version"):
            self.assertNotIn(option, argv)

    def test_unreleased_entrypoints_fail_before_observation_files_or_fork(self) -> None:
        with (
            patch("evaluations.linux_resource.owned_flac_decode.observe") as observe,
            patch("evaluations.linux_resource.owned_mp3_fixture.FLAC_ENCODER_REVIEW", None),
            patch.object(Path, "exists") as exists,
            patch("os.fork") as fork,
        ):
            with self.assertRaisesRegex(ProbeError, "binding is not available"):
                run_bound_decode()
            with self.assertRaisesRegex(ProbeError, "review is not bound"):
                generate_fixture(FLAC)
            observe.assert_not_called()
            exists.assert_not_called()
            fork.assert_not_called()

    def test_metadata_accepts_owned_shape_and_records_exact_metadata(self) -> None:
        data = b"fLaC" + block(0, streaminfo()) + block(4, comment())
        data += block(1, bytes(8192), last=True) + b"synthetic frame"
        result = inspect(data)
        self.assertEqual(
            (result.sample_rate, result.channels, result.bits_per_sample), (48000, 2, 16)
        )
        self.assertEqual(result.sample_frames, flac.SAMPLE_FRAMES)
        self.assertEqual(result.block_types, (0, 4, 1))
        self.assertEqual(
            result.metadata_sha256, hashlib.sha256(data[: result.metadata_bytes]).hexdigest()
        )

    def test_metadata_rejects_each_format_count_and_md5_mismatch(self) -> None:
        variants = (
            streaminfo(rate=44100),
            streaminfo(channels=1),
            streaminfo(bits=24),
            streaminfo(frames=0),
            streaminfo(frames=flac.SAMPLE_FRAMES - 1),
            streaminfo(md5="00" * 16),
            streaminfo(md5="34" * 16),
            bytes(4) + streaminfo()[4:],
            streaminfo()[:-1],
        )
        for info in variants:
            with self.subTest(info=info), self.assertRaises(ProbeError):
                inspect(b"fLaC" + block(0, info, last=True) + b"x")

    def test_output_bitexact_metadata_rejects_default_vendor_comments_or_padding(self) -> None:
        prefix = b"fLaC" + block(0, streaminfo())
        suffix = block(1, bytes(8192), last=True) + b"x"
        for value in (comment(b"encoder=Lavf61"), comment().replace(b"ffmpeg", b"Lavf61")):
            with self.subTest(value=value), self.assertRaises(ProbeError):
                inspect(prefix + block(4, value) + suffix)
        for size in (0, 8191, 8193):
            with self.subTest(size=size), self.assertRaises(ProbeError):
                inspect(prefix + block(4, comment()) + block(1, bytes(size), last=True) + b"x")

    def test_metadata_rejects_malformed_foreign_or_transforming_blocks(self) -> None:
        first = b"fLaC" + block(0, streaminfo())
        variants = [
            b"OggS" + block(0, streaminfo(), last=True) + b"x",
            first,
            first + b"\x81\xff\xff\xffx",
            first + block(0, streaminfo(), last=True) + b"x",
            first + block(1, b"x", last=True) + b"x",
            first + block(1, bytes(flac.MAX_METADATA_BYTES), last=True) + b"x",
            b"fLaC" + block(1, bytes(34), last=True) + b"x",
            b"fLaC" + block(0, streaminfo(), last=True),
        ]
        variants.extend(
            first + block(kind, b"x", last=True) + b"x" for kind in (2, 3, 5, 6, 7, 127)
        )
        variants.extend(
            first + block(4, payload, last=True) + b"x"
            for payload in (
                comment(b"REPLAYGAIN_TRACK_GAIN=1"),
                comment(b"WAVEFORMATEXTENSIBLE_CHANNEL_MASK=3"),
                comment(b"title=x"),
                comment(b"encoder="),
                comment(b"encoder=\x00"),
                comment()[:-1],
                comment() + b"x",
                b"\xff\xff\xff\xff",
            )
        )
        for data in variants:
            with self.subTest(data=data[:64]), self.assertRaises(ProbeError):
                inspect(data)

    def test_exact_progress_grammar_rejects_warning_and_erasure_bypasses(self) -> None:
        updates = [f"{number}% complete".encode() for number in (0, 9, 10, 99)]
        valid = flac.BANNER + b"-: " + b"".join(part + b"\b" * len(part) for part in updates)
        valid += b"done         \n"
        flac.validate_diagnostics(valid)
        flac.validate_diagnostics(flac.BANNER + DONE)
        for data in (
            valid.replace(b"1.5.0", b"1.5.1"),
            valid + DONE,
            valid[:-1],
            valid.replace(b"\b", b"", 1),
            valid.replace(b"99%", b"100%"),
            valid.replace(b"9% complete", b"09% complete"),
            valid.replace(b"99%", b"01%"),
            valid.replace(b"done", b"WARNING: done"),
            flac.BANNER + b"-: wrote 5760000 samples" + DONE,
            flac.BANNER + b"-: WARNING\b\b\b\b\b\b\bdone         \n",
            valid + bytes(flac.MAX_DIAGNOSTIC_BYTES),
        ):
            with self.subTest(data=data[-64:]), self.assertRaises(ProbeError):
                flac.validate_diagnostics(data)

    def test_native_banner_fragments_use_bound_decoder_and_never_overread(self) -> None:
        for split in range(1, len(flac.BANNER)):
            chunks = iter((flac.BANNER[:split], flac.BANNER[split:]))
            with (
                patch.object(Child, "reap"),
                patch(f"{MODULE}.time.monotonic", return_value=1),
                patch(f"{MODULE}.select.select", return_value=([11], [], [])),
                patch(f"{MODULE}.os.read", side_effect=chunks),
            ):
                self.assertEqual(receive_banner(Child(42), 10, 11, 2, DECODER), flac.BANNER)
        for data in (b"", b"\nflac other", flac.BANNER[:-1] + b"x"):
            with (
                patch.object(Child, "reap"),
                patch(f"{MODULE}.time.monotonic", return_value=1),
                patch(f"{MODULE}.select.select", return_value=([11], [], [])),
                patch(f"{MODULE}.os.read", return_value=data),
                self.assertRaises(ProbeError),
            ):
                receive_banner(Child(42), 10, 11, 2, DECODER)

    def test_changed_native_identity_and_unreviewed_mapping_fail(self) -> None:
        with (
            patch(f"{MODULE}.os.kill"),
            patch(f"{MODULE}.time.monotonic", return_value=1),
            patch(f"{MODULE}.os.waitpid", return_value=(42, 0)),
            patch(f"{MODULE}.os.WIFSTOPPED", return_value=True),
            patch(f"{MODULE}.read_start_identity", return_value=("T", 123)),
            patch(f"{MODULE}.os.readlink", return_value="/usr/bin/mpg123"),
            patch(f"{MODULE}.parse_maps") as maps,
            self.assertRaisesRegex(ProbeError, "executable"),
        ):
            stopped_native_gate(Child(42), 123, 2, decoder=DECODER)
        maps.assert_not_called()
        text = "10000-11000 r-xp 00000000 08:01 1 /usr/bin/flac"
        parse_maps(text, DECODER)
        with self.assertRaises(ProbeError):
            parse_maps(text.replace("flac", "mpg123"), DECODER)
        with self.assertRaises(ProbeError):
            NativeDecoder(flac.ARGV, flac.BANNER, (), flac.validate_diagnostics)
        with self.assertRaises(FrozenInstanceError):
            DECODER.__setattr__("banner", b"different")

    def test_bound_receipt_requires_exact_known_lossless_expectations(self) -> None:
        fixture = DecodeFixture(
            FLAC.path,
            100,
            "ab" * 32,
            flac.RAW_BYTES,
            flac.MAX_FILE_BYTES,
            "",
            pcm_sha256=flac.RAW_SHA256,
        )
        binding = FlacBinding(
            "sha256:" + "cd" * 32,
            "https://github.com/slackysoba/sobafm/issues/7",
            DECODER,
            fixture,
            MD5,
            "ef" * 32,
        )
        for changed in (
            replace(fixture, pcm_sha256=None),
            replace(fixture, pcm_bytes=4),
            replace(fixture, sha256=""),
            replace(fixture, output_copy_offset=1),
        ):
            with self.assertRaises(ProbeError):
                replace(binding, fixture=changed)
        with (
            memoryview(
                b"fLaC"
                + block(0, streaminfo())
                + block(4, comment())
                + block(1, bytes(8192), last=True)
                + b"x"
            ) as payload,
            self.assertRaisesRegex(ProbeError, "metadata differs"),
        ):
            binding.validate_input(payload)

    def test_real_pump_partial_io_requires_exact_hash_eofs_and_releases_views(self) -> None:
        pump = Pump()
        result = pump.run()
        self.assertEqual(result["verdict"], "owned_fixture_decode_pass", result)
        self.assertEqual(result["fixture_bytes_accepted"], 3)
        self.assertEqual(result["pcm_output_bytes"], 8)
        self.assertTrue(all(result[key] for key in ("input_complete", "stdout_eof", "stderr_eof")))
        self.assertFalse(result["cleanup_debt"])
        self.assertEqual(pump.cleaned_deadlines, [4.0])
        for view in pump.read_views:
            with self.assertRaises(ValueError):
                len(view)

    def test_real_pump_rejects_zero_exit_short_flush_hash_change_and_extra_pcm(self) -> None:
        for output in (b"abcd", b"abcdefgX", b"abcdefghi"):
            with self.subTest(output=output):
                result = Pump(output=output).run()
                self.assertEqual(result["verdict"], "incomplete", result)
                self.assertIsNotNone(result["failure"])

    def test_real_pump_failures_and_cleanup_debt_never_pass(self) -> None:
        for failure in (
            "early_exit",
            "signal",
            "stderr_error",
            "stdout_stall",
            "fd_debt",
            "pid_debt",
            "cleanup_late",
        ):
            with self.subTest(failure=failure):
                pump = Pump(failure=failure)
                result = pump.run()
                self.assertEqual(result["verdict"], "incomplete", result)
                self.assertEqual(len(pump.cleaned_deadlines), 1)
                if failure.endswith("debt"):
                    self.assertTrue(result["cleanup_debt"])
                if failure == "stdout_stall":
                    self.assertFalse(result["stdout_eof"])

    def test_metadata_rejection_never_releases_input_and_still_cleans_owner(self) -> None:
        pump = Pump(failure="metadata")
        result = pump.run()
        self.assertEqual(result["verdict"], "incomplete")
        self.assertFalse(pump.resumed)
        self.assertEqual(pump.accepted, 0)
        self.assertTrue(pump.cleaned)
        self.assertEqual(pump.cleaned_deadlines, [4.0])
