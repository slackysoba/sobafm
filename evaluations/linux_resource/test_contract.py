"""Offline parser/contract checks; no fork, decoder, Docker or provider use."""

import struct
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from .decoder_startup import (
    ADDRESS_SPACE_BYTES,
    DATA_BYTES,
    OUTPUT_BYTES,
    STACK_BYTES,
    Child,
    CleanupResult,
    capture,
    cleanup,
    run_startup,
    startup_argv,
    valid_version_output,
    verify_rlimits,
)
from .owned_mp3_fixture import (
    CHUNK_BYTES,
    FIXTURE_BYTES,
    SAMPLE_FRAMES,
    fill_pcm,
    verify_file_limits,
)
from .probe import (
    COPY_BYTES,
    GROUP_BYTES,
    PCM_BYTES,
    Identity,
    ProbeError,
    counters,
    run_tree,
    unified_path,
    validate_identity,
    verify_limits,
)


class ContractTests(unittest.TestCase):
    def test_owned_generator_has_exact_boundary_and_continuous_stereo_signal(self) -> None:
        buffer = bytearray(CHUNK_BYTES)
        self.assertEqual(fill_pcm(0, buffer), CHUNK_BYTES)
        self.assertEqual(struct.unpack_from("<hh", buffer), (-8000, -7500))
        self.assertEqual(struct.unpack_from("<hh", buffer, 100 * 4), (8000, 2500))
        first_frame = CHUNK_BYTES // 4
        self.assertEqual(fill_pcm(first_frame, buffer), CHUNK_BYTES)
        self.assertNotEqual(struct.unpack_from("<hh", buffer), (-8000, -7500))
        self.assertEqual(fill_pcm(SAMPLE_FRAMES - 1, buffer), 4)
        self.assertEqual(struct.unpack_from("<hh", buffer), (-7840, -7400))
        self.assertEqual(fill_pcm(SAMPLE_FRAMES, buffer), 0)
        for frame in (-1, SAMPLE_FRAMES + 1):
            with self.subTest(frame=frame), self.assertRaises(ValueError):
                fill_pcm(frame, buffer)
        with self.assertRaises(ValueError):
            fill_pcm(0, bytearray(CHUNK_BYTES + 1))

    def test_owned_encoder_file_limit_gate_rejects_missing_or_widened_limits(self) -> None:
        valid = (
            f"Max file size             {FIXTURE_BYTES} {FIXTURE_BYTES} bytes\n"
            "Max core file size        0 0 bytes\n"
        )
        self.assertEqual(verify_file_limits(valid)["Max file size"], FIXTURE_BYTES)
        for text in (
            "",
            valid + valid,
            valid.replace(str(FIXTURE_BYTES), "unlimited", 1),
            valid.replace("0 0 bytes", "0 unlimited bytes"),
        ):
            with self.subTest(text=text), self.assertRaises(ProbeError):
                verify_file_limits(text)

    def test_startup_unreviewed_target_fails_before_file_access_or_fork(self) -> None:
        module = "evaluations.linux_resource.decoder_startup"
        with patch(f"{module}.Path") as path, patch(f"{module}.os.fork") as fork:
            with self.assertRaisesRegex(ProbeError, "unsupported fixed"):
                run_startup("0" * 64, "/usr/bin/arbitrary")
            path.assert_not_called()
            fork.assert_not_called()

    def test_startup_fixed_commands_have_no_media_or_device_argument(self) -> None:
        self.assertEqual(startup_argv("ffmpeg"), ("/usr/bin/ffmpeg", "-nostdin", "-version"))
        self.assertEqual(startup_argv("mpg123"), ("/usr/bin/mpg123", "--version"))

    def test_address_space_variant_rejects_other_decoder_before_file_access(self) -> None:
        module = "evaluations.linux_resource.decoder_startup"
        with patch(f"{module}.Path") as path, patch(f"{module}.os.fork") as fork:
            with self.assertRaisesRegex(ProbeError, "requires mpg123"):
                run_startup("0" * 64, "ffmpeg", address_space_limit=True)
            path.assert_not_called()
            fork.assert_not_called()

    def test_address_space_gate_rejects_missing_changed_unlimited_or_duplicate_limit(self) -> None:
        original = (
            f"Max data size             {DATA_BYTES} {DATA_BYTES} bytes\n"
            f"Max stack size            {STACK_BYTES} {STACK_BYTES} bytes\n"
        )
        address_space = (
            f"Max address space         {ADDRESS_SPACE_BYTES} {ADDRESS_SPACE_BYTES} bytes\n"
        )
        self.assertEqual(
            verify_rlimits(original + address_space, True)["Max address space"], ADDRESS_SPACE_BYTES
        )
        for text in (
            original,
            original + address_space.replace(str(ADDRESS_SPACE_BYTES), "unlimited", 1),
            original
            + address_space.replace(str(ADDRESS_SPACE_BYTES), str(ADDRESS_SPACE_BYTES + 1)),
            original + address_space.replace(f"{ADDRESS_SPACE_BYTES} bytes", "unlimited bytes"),
            original + address_space + address_space,
        ):
            with self.subTest(text=text), self.assertRaises(ProbeError):
                verify_rlimits(text, True)

    def test_address_space_failure_never_releases_exec_gate(self) -> None:
        module = "evaluations.linux_resource.decoder_startup"
        original = (
            f"Max data size             {DATA_BYTES} {DATA_BYTES} bytes\n"
            f"Max stack size            {STACK_BYTES} {STACK_BYTES} bytes\n"
        )

        def release(child: Child, deadline: float) -> CleanupResult:
            child.exit_code = 0
            return CleanupResult(True, [])

        for extra in ("", f"Max address space {ADDRESS_SPACE_BYTES} unlimited bytes\n"):

            def verify(
                pid: int, address_space_limit: bool, text: str = original + extra
            ) -> dict[str, int]:
                self.assertEqual(pid, 42)
                self.assertTrue(address_space_limit)
                return verify_rlimits(text, address_space_limit)

            with (
                self.subTest(extra=extra),
                patch(f"{module}.Path") as path,
                patch(f"{module}.hashlib.file_digest") as digest,
                patch(f"{module}.os.pipe", side_effect=[(10, 11), (12, 13), (14, 15), (16, 17)]),
                patch(f"{module}.os.fork", return_value=42),
                patch(f"{module}.os.close"),
                patch(f"{module}.os.write") as write,
                patch(f"{module}.os.read", return_value=b"R"),
                patch(f"{module}.select.select", return_value=([10], [], [])),
                patch(f"{module}.verify_child", side_effect=verify),
                patch(f"{module}.cleanup", side_effect=release),
            ):
                path.return_value.iterdir.return_value = [Path("/unused")]
                digest.return_value.hexdigest.return_value = "0" * 64
                result = run_startup("0" * 64, "mpg123", address_space_limit=True)
                self.assertEqual(result["verdict"], "incomplete")
                self.assertFalse(result["exec_gate_released"])
                self.assertEqual(result["argv"], ["/usr/bin/mpg123", "--version"])
                self.assertEqual(result["limit_profile"], "data-stack-as")
                write.assert_not_called()

    def test_startup_wrong_version_or_decoder_output_never_passes(self) -> None:
        self.assertTrue(valid_version_output("mpg123", b"mpg123 1.33.7\n"))
        for output in (
            b"mpg123 1.32.10\n",
            b"mpg123 1.33.70\n",
            b"prefix mpg123 1.33.7\n",
            b"ffmpeg version 7.1.5-0+deb13u1 configuration: --enable-gpl\n",
            b"",
        ):
            with self.subTest(output=output):
                self.assertFalse(valid_version_output("mpg123", output))
        self.assertFalse(valid_version_output("ffmpeg", b"mpg123 1.33.7\n"))
        self.assertFalse(
            valid_version_output(
                "ffmpeg", b"ffmpeg version 7.1.5-0+deb13u1 configuration: --enable-nonfree\n"
            )
        )

    def test_startup_limit_gate_rejects_missing_changed_or_duplicate_limits(self) -> None:
        valid = (
            f"Max data size             {DATA_BYTES} {DATA_BYTES} bytes\n"
            f"Max stack size            {STACK_BYTES} {STACK_BYTES} bytes\n"
        )
        self.assertEqual(verify_rlimits(valid)["Max data size"], DATA_BYTES)
        for text in (
            "",
            valid.replace(str(DATA_BYTES), "unlimited", 1),
            valid.replace(str(STACK_BYTES), str(STACK_BYTES + 1), 1),
            valid + valid,
        ):
            with self.subTest(text=text), self.assertRaises(ProbeError):
                verify_rlimits(text)

    def test_startup_output_flood_stops_at_the_reserved_capacity(self) -> None:
        module = "evaluations.linux_resource.decoder_startup"
        stream = bytearray(b"a" * OUTPUT_BYTES)
        with (
            patch(f"{module}.time.monotonic", return_value=1),
            patch(f"{module}.select.select", return_value=([10], [], [])),
            patch(f"{module}.os.read", return_value=b"x"),
            self.assertRaisesRegex(ProbeError, "stream bound"),
        ):
            capture(Child(42, 0), {10: stream}, 2)
        self.assertEqual(len(stream), OUTPUT_BYTES)

    def test_startup_eof_does_not_release_a_live_process(self) -> None:
        module = "evaluations.linux_resource.decoder_startup"
        child = Child(42)
        with (
            patch(f"{module}.time.monotonic", side_effect=[1, 3]),
            patch(f"{module}.select.select", return_value=([10], [], [])),
            patch(f"{module}.os.read", return_value=b""),
            patch(f"{module}.os.waitpid", return_value=(0, 0)),
            self.assertRaisesRegex(ProbeError, "deadline"),
        ):
            capture(child, {10: bytearray()}, 2)
        self.assertIsNone(child.exit_code)

    def test_startup_reaped_leader_retains_descendant_debt_at_original_deadline(self) -> None:
        module = "evaluations.linux_resource.decoder_startup"
        with (
            patch(f"{module}.time.monotonic", side_effect=[10, 14]),
            patch(f"{module}.group_members", return_value=[43]),
            patch(f"{module}.os.killpg"),
        ):
            result = cleanup(Child(42, 0), 13)
            self.assertEqual(result.retained_pids, [43])
            self.assertFalse(result.deadline_met)

    def test_startup_late_release_clears_debt_but_never_meets_cleanup_deadline(self) -> None:
        module = "evaluations.linux_resource.decoder_startup"
        for times, members in (([10, 14], [[43], []]), ([10, 12.99, 13.01], [[43], []])):
            with (
                self.subTest(times=times),
                patch(f"{module}.time.monotonic", side_effect=times),
                patch(f"{module}.group_members", side_effect=members),
                patch(f"{module}.os.killpg"),
            ):
                result = cleanup(Child(42, 0), 13)
                self.assertEqual(result.retained_pids, [])
                self.assertFalse(result.deadline_met)

    def test_unlimited_or_changed_kernel_limits_fail_closed(self) -> None:
        values = {
            "memory.max": str(GROUP_BYTES),
            "memory.swap.max": "0",
            "pids.max": "64",
            "cpu.max": "200000 100000",
        }
        verify_limits(values)
        for key, value in (
            ("memory.max", "max"),
            ("memory.max", "90177537"),
            ("memory.swap.max", "1"),
            ("pids.max", "max"),
            ("cpu.max", "max 100000"),
        ):
            with self.subTest(key=key, value=value), self.assertRaises(ProbeError):
                verify_limits(values | {key: value})

    def test_membership_requires_one_unified_group(self) -> None:
        self.assertEqual(unified_path("0::/\n"), "/")
        for text in ("", "1:memory:/\n", "0::/\n0::/elsewhere\n", "0::relative"):
            with self.subTest(text=text), self.assertRaises(ProbeError):
                unified_path(text)

    def test_child_security_and_membership_are_not_inferred_from_parent(self) -> None:
        for item in (
            Identity(2, 1, 0, 1000, "/", 2, 0),
            Identity(2, 1, 1000, 0, "/", 2, 0),
            Identity(2, 1, 1000, 1000, "/", 0, 0),
            Identity(2, 1, 1000, 1000, "/", 2, 1),
            Identity(2, 1, 1000, 1000, "/other", 2, 0),
        ):
            with self.subTest(item=item), self.assertRaises(ProbeError):
                validate_identity(item, "/")

    def test_event_counter_rejects_ambiguous_evidence(self) -> None:
        self.assertEqual(counters("oom 1\noom_kill 1\n"), {"oom": 1, "oom_kill": 1})
        for text in ("oom 1\noom 2\n", "oom -1\n", "oom max\n"):
            with self.subTest(text=text), self.assertRaises(ProbeError):
                counters(text)

    def test_parent_reservations_leave_no_implicit_native_pcm_pass(self) -> None:
        self.assertEqual(GROUP_BYTES + PCM_BYTES + COPY_BYTES, 98_979_840)
        self.assertLess(GROUP_BYTES + PCM_BYTES + COPY_BYTES, 100_000_000)
        self.assertLessEqual(PCM_BYTES + COPY_BYTES, 34_560_000)

    def test_failed_join_retains_debt_and_does_not_reset_deadline(self) -> None:
        module = "evaluations.linux_resource.probe"
        members = [Identity(42, 1, 1000, 1000, "/", 2, 0), Identity(43, 42, 1000, 1000, "/", 2, 0)]
        with ExitStack() as stack:
            for name, value in (
                ("cgroup_root", Path("/unused")),
                ("read_text", "oom 0\noom_kill 0\n"),
                ("receive_identities", members),
                ("emit", None),
                ("os.fork", 42),
                ("os.close", None),
                ("os.killpg", None),
                ("os.write", 2),
            ):
                stack.enter_context(patch(f"{module}.{name}", return_value=value))
            stack.enter_context(patch(f"{module}.os.pipe", side_effect=[(10, 11), (12, 13)]))
            clock = stack.enter_context(patch(f"{module}.time.monotonic", side_effect=[100, 101]))
            joined = stack.enter_context(patch(f"{module}.wait_child", return_value=False))
            stack.enter_context(patch.object(Path, "exists", return_value=False))
            with self.assertRaisesRegex(ProbeError, "retained process debt"):
                run_tree(False)
            self.assertEqual([call.args for call in joined.call_args_list], [(42, 104), (42, 104)])
            self.assertEqual(clock.call_count, 2)

    def test_reaped_leader_with_retained_descendant_fails(self) -> None:
        module = "evaluations.linux_resource.probe"
        members = [Identity(42, 1, 1000, 1000, "/", 2, 0), Identity(43, 42, 1000, 1000, "/", 2, 0)]
        no_ready_descriptors: tuple[list[int], list[int], list[int]] = ([], [], [])
        with ExitStack() as stack:
            for name, value in (
                ("cgroup_root", Path("/unused")),
                ("read_text", "oom 0\noom_kill 0\n"),
                ("receive_identities", members),
                ("emit", None),
                ("os.fork", 42),
                ("os.close", None),
                ("os.write", 2),
                ("wait_child", True),
                ("select.select", no_ready_descriptors),
            ):
                stack.enter_context(patch(f"{module}.{name}", return_value=value))
            stack.enter_context(patch(f"{module}.os.pipe", side_effect=[(10, 11), (12, 13)]))
            stack.enter_context(
                patch(f"{module}.time.monotonic", side_effect=[100, 101, 102, 102.1, 105])
            )
            stack.enter_context(patch.object(Path, "exists", return_value=True))
            with self.assertRaisesRegex(ProbeError, "retained process debt"):
                run_tree(False)


if __name__ == "__main__":
    unittest.main()
