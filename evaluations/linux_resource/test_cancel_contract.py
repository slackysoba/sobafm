"""Offline cancellation ownership schedules; no fork, native codec or Docker."""

from __future__ import annotations

import errno
import fcntl
import hashlib
import os
import select
import signal
import unittest
from contextlib import ExitStack, suppress
from pathlib import Path
from unittest.mock import patch

from .decoder_startup import Child
from .owned_mp3_cancel import (
    PREFIX_BYTES,
    STDERR_BYTES,
    CancelRun,
    backpressure,
    blocked_stdout_write,
    run_case,
    unread_bytes,
    validate_cancel_diagnostics,
)
from .owned_mp3_decode import BANNER
from .owned_mp3_overlap import Pipes, make_pipes
from .probe import ProbeError

MODULE = "evaluations.linux_resource.owned_mp3_cancel"
DIAGNOSTICS = BANNER + (
    b"\nNote: forcing output encoding s16\nPlaying MPEG stream 1 of 1: - ...\n"
    b"MPEG 1.0 L III vbr 48000 j-s\nmpg123: death by SIGTERM\n"
    b"[0:00] Decoding of - finished.\n"
)
WRITE_SYSCALL = "1 0x1 0xabcdef 0x1000 0x0 0x0 0x0 0xabc 0xdef"


class Clock:
    now = 10.0

    def monotonic(self) -> float:
        return self.now


def close_if_open(fd: int) -> None:
    with suppress(OSError):
        os.close(fd)


def setup_text(path: Path) -> str:
    return "core" if path.name == "core_pattern" else "0"


def real_owner(stack: ExitStack, case: str = "feed") -> tuple[CancelRun, Pipes]:
    owner = CancelRun("feed" if case == "feed" else "stdout_backpressure")
    pipes = make_pipes(owner.fds)
    # Test peers are separately owned by ExitStack. They never represent native children.
    for fd in owner.fds:
        stack.callback(close_if_open, fd)
    for fd in (pipes.ready_read, pipes.ready_write, pipes.gate_read, pipes.gate_write):
        owner.close(fd)
    for fd in (pipes.input_read, pipes.output_write, pipes.error_write):
        owner.fds.remove(fd)
    owner.pipes = pipes
    owner.child = Child(42)
    owner.start_ticks = 123
    owner.input_released = owner.exec_released = True
    owner.buffers = [bytearray(32), bytearray(32), bytearray(128)]
    owner.views = [memoryview(buffer) for buffer in owner.buffers]
    owner.bank = owner.views[0]
    with owner.views[2][:64] as source:
        owner.input_view = source.toreadonly()
    owner.output_view = owner.views[2][64:]
    owner.views.extend((owner.input_view, owner.output_view))
    for fd in (pipes.input_write, pipes.output_read, pipes.error_read):
        os.set_blocking(fd, False)
    stack.enter_context(patch(f"{MODULE}.CHUNK_BYTES", 64))
    stack.enter_context(patch(f"{MODULE}.native_processes", return_value=set()))
    stack.enter_context(patch(f"{MODULE}.read_start_identity", return_value=("S", 123)))
    stack.enter_context(patch.object(Child, "reap"))
    return owner, pipes


class CancellationContracts(unittest.TestCase):
    def test_read_delivery_before_completion_retains_original_owner_and_views(self) -> None:
        original_readv = os.readv
        clock = Clock()
        with ExitStack() as stack:
            stack.enter_context(patch(f"{MODULE}.time.monotonic", side_effect=clock.monotonic))
            owner, pipes = real_owner(stack)
            payloads = list(owner.views)
            os.write(pipes.output_write, b"abcdefg")

            def delivered(fd: int, destinations: list[memoryview]) -> int:
                amount = original_readv(fd, destinations)
                owner.request("delivered_before_completion")
                clock.now = 11.0
                owner.request("repeat_inside_original_read")
                owner.close(fd)
                self.assertIn(fd, owner.fds)
                self.assertIn(fd, owner.busy)
                self.assertFalse(owner.release_if_done())
                with self.assertRaises(ProbeError):
                    owner.require_released()
                self.assertEqual(bytes(destinations[0][:amount]), b"abcdefg")
                return amount

            with patch(f"{MODULE}.os.readv", side_effect=delivered):
                owner.read_pcm()
            self.assertEqual((owner.output_count, owner.drained, owner.bank_filled), (7, 7, 0))
            self.assertFalse(owner.output_eof)
            self.assertFalse(owner.busy)
            self.assertEqual((owner.cancel.started, owner.cancel.deadline), (10.0, 13.0))
            owner.read_pcm()  # Live empty peer yields EAGAIN, not EOF or release.
            self.assertFalse(owner.output_eof)
            os.close(pipes.output_write)
            owner.read_pcm()
            self.assertTrue(owner.output_eof)
            for fd in tuple(owner.fds):
                owner.close(fd)
            self.assertFalse(owner.release_if_done())  # EOF is not child reaping.
            owner.child.exit_code = 0
            self.assertTrue(owner.release_if_done())
            digest = owner.pcm_digest
            self.assertTrue(owner.release_if_done())
            self.assertEqual(owner.pcm_digest, digest)
            self.assertEqual(digest, hashlib.sha256(b"abcdefg").hexdigest())
            for view in payloads:
                with self.assertRaises(ValueError):
                    _ = view.obj

    def test_write_delivery_cannot_release_or_close_its_inflight_input(self) -> None:
        original_write = os.write
        with ExitStack() as stack:
            owner, pipes = real_owner(stack)

            def delivered(fd: int, data: memoryview) -> int:
                amount = original_write(fd, data)
                owner.request("input_delivered")
                owner.request("repeat_input_delivered")
                owner.close(fd)
                self.assertIn(fd, owner.busy)
                self.assertIn(fd, owner.fds)
                self.assertFalse(owner.release_if_done())
                return amount

            with patch(f"{MODULE}.os.write", side_effect=delivered):
                owner.write_input()
            self.assertEqual(owner.input_count, 64)
            self.assertEqual(len(os.read(pipes.input_read, 128)), 64)
            self.assertFalse(owner.busy)
            owner.close(pipes.input_write)
            self.assertNotIn(pipes.input_write, owner.fds)

    def test_repeated_cancellation_drain_term_and_kill_keep_original_deadline(self) -> None:
        clock = Clock()
        with ExitStack() as stack:
            stack.enter_context(patch(f"{MODULE}.time.monotonic", side_effect=clock.monotonic))
            owner, pipes = real_owner(stack)
            stack.enter_context(patch(f"{MODULE}.os.getpgid", return_value=42))
            stack.enter_context(patch(f"{MODULE}.group_members", return_value=[42]))

            def signal_before_eof(pid: int, sig: signal.Signals) -> None:
                if sig == signal.SIGTERM:
                    self.assertIn(pipes.input_write, owner.fds)
                    os.fstat(pipes.input_write)

            kill = stack.enter_context(patch(f"{MODULE}.os.killpg", side_effect=signal_before_eof))
            owner.request("test_trigger")
            owner.manage_cancel()
            self.assertNotIn(pipes.input_write, owner.fds)
            self.assertEqual(owner.cancel.requests, 2)
            clock.now = 10.5
            os.write(pipes.output_write, b"abcd")
            owner.io_step(0)
            self.assertEqual(owner.drained, 4)
            owner.request("repeat_during_drain")
            owner.manage_cancel()
            clock.now = 11.0
            owner.manage_cancel()
            owner.manage_cancel()
            self.assertEqual(
                [call.args[1] for call in kill.call_args_list],
                [signal.SIGTERM, signal.SIGCONT, signal.SIGKILL],
            )
            self.assertEqual((owner.cancel.started, owner.cancel.deadline), (10.0, 13.0))
            owner.child.exit_code = -9
            self.assertFalse(owner.release_if_done())  # Exit does not settle original pipes.

    def test_exact_deadline_passes_but_late_release_never_repairs_verdict(self) -> None:
        for released_at, missed in ((13.0, False), (13.0001, True)):
            with self.subTest(released_at=released_at), ExitStack() as stack:
                clock = Clock()
                stack.enter_context(patch(f"{MODULE}.time.monotonic", side_effect=clock.monotonic))
                owner, _ = real_owner(stack)
                owner.request("trigger")
                owner.request("repeat")
                owner.child.exit_code = 0
                owner.trigger = {"case": "feed"}
                owner.term_sent = 10.0
                owner.output_eof = owner.error_eof = True
                owner.stderr = bytearray(DIAGNOSTICS)
                owner.memory_before = {"oom": 0, "oom_kill": 0}
                for fd in tuple(owner.fds):
                    owner.close(fd)
                clock.now = released_at
                self.assertTrue(owner.release_if_done())
                owner.cancel.observe(10.0)
                stack.enter_context(patch(f"{MODULE}.read_text", return_value="0"))
                stack.enter_context(patch(f"{MODULE}.counters", return_value=owner.memory_before))
                result = owner.finish()
                self.assertEqual(owner.cancel.deadline_missed, missed)
                self.assertEqual(
                    result["verdict"], "incomplete" if missed else "direct_decoder_cancel_pass"
                )
                self.assertFalse(result["cleanup_debt"])
                self.assertFalse(result["complete_decode"])

    def test_stuck_original_operation_and_retained_descendant_prevent_reuse(self) -> None:
        with ExitStack() as stack:
            clock = Clock()
            stack.enter_context(patch(f"{MODULE}.time.monotonic", side_effect=clock.monotonic))
            owner, pipes = real_owner(stack)
            owner.request("trigger")
            owner.busy.add(pipes.output_read)
            owner.child.exit_code = 0
            for fd in tuple(owner.fds):
                owner.close(fd)
            clock.now = 14.0
            owner.cancel.observe(clock.now)
            self.assertFalse(owner.release_if_done())
            self.assertTrue(owner.cancel.deadline_missed)
            self.assertTrue(owner.views)
            with self.assertRaises(ProbeError):
                owner.require_released()
            owner.busy.remove(pipes.output_read)
            owner.close(pipes.output_read)
            with patch(f"{MODULE}.native_processes", return_value={43}):
                self.assertFalse(owner.release_if_done())
                self.assertEqual(owner.retained_pids, [43])
            self.assertTrue(owner.release_if_done())
            self.assertTrue(owner.cancel.deadline_missed)

    def test_uncertain_close_is_not_retried_and_actual_release_does_not_erase_failure(self) -> None:
        original_close = os.close
        with ExitStack() as stack:
            owner, pipes = real_owner(stack)

            def released_then_error(fd: int) -> None:
                original_close(fd)
                raise OSError(errno.EINTR, "close result interrupted")

            with patch(f"{MODULE}.os.close", side_effect=released_then_error) as close:
                owner.close(pipes.input_write)
                owner.close(pipes.input_write)
                self.assertEqual(close.call_count, 1)
            self.assertIn(pipes.input_write, owner.fds)
            owner.reconcile_closes()
            self.assertNotIn(pipes.input_write, owner.fds)
            self.assertIn("not confirmed", str(owner.failure))
            with patch(f"{MODULE}.os.close", side_effect=OSError(errno.EIO, "not released")):
                owner.close(pipes.output_read)
            owner.reconcile_closes()
            self.assertIn(pipes.output_read, owner.uncertain_closes)
            self.assertFalse(owner.release_if_done())

    def test_failed_read_does_not_masquerade_as_eof_and_stderr_is_bounded(self) -> None:
        with ExitStack() as stack:
            owner, pipes = real_owner(stack)
            with patch(f"{MODULE}.os.readv", side_effect=InterruptedError):
                owner.read_pcm()
            self.assertFalse(owner.output_eof)
            os.write(pipes.output_write, b"x")
            with patch(f"{MODULE}.os.readv", side_effect=OSError(errno.EIO, "read failed")):
                owner.io_step(0)
            self.assertIn("read failed", str(owner.failure))
            self.assertFalse(owner.output_eof)
            self.assertNotIn(pipes.output_read, owner.fds)
            owner.stderr.extend(b"x" * STDERR_BYTES)
            os.write(pipes.error_write, b"overflow")
            owner.io_step(0)
            self.assertEqual(len(owner.stderr), STDERR_BYTES)
            self.assertNotIn(pipes.error_read, owner.fds)
            self.assertFalse(owner.error_eof)

    def test_actual_pipe_occupancy_and_blocked_write_are_both_required(self) -> None:
        with ExitStack() as stack:
            reader, writer = os.pipe()
            stack.callback(os.close, reader)
            stack.callback(os.close, writer)
            os.set_blocking(writer, False)
            capacity = fcntl.fcntl(reader, fcntl.F_GETPIPE_SZ)
            stack.enter_context(patch(f"{MODULE}.os.stat", return_value=os.fstat(reader)))
            stack.enter_context(patch(f"{MODULE}.read_start_identity", return_value=("S", 123)))
            syscall = stack.enter_context(patch(f"{MODULE}.read_text", return_value=WRITE_SYSCALL))
            self.assertIsNone(backpressure(42, 123, reader))
            syscall.assert_not_called()
            self.assertEqual(os.write(writer, b"x" * capacity), capacity)
            self.assertEqual(unread_bytes(reader), capacity)
            syscall.return_value = "running"
            self.assertIsNone(backpressure(42, 123, reader))
            syscall.return_value = WRITE_SYSCALL
            evidence = backpressure(42, 123, reader)
            self.assertIsNotNone(evidence)
            self.assertEqual(
                evidence,
                {
                    "pipe_capacity": capacity,
                    "unread_bytes": capacity,
                    "syscall": "amd64_write",
                    "stdout_fd": 1,
                    "state": "S",
                },
            )
            syscall.side_effect = PermissionError("access denied")
            with self.assertRaises(PermissionError):
                backpressure(42, 123, reader)

    def test_syscall_parser_rejects_ambiguity_and_other_write_destinations(self) -> None:
        self.assertTrue(blocked_stdout_write(WRITE_SYSCALL))
        for value in (
            "running",
            "-1 0xabc 0xdef",
            WRITE_SYSCALL.replace("0x1 ", "0x2 ", 1),
            WRITE_SYSCALL.replace("1 ", "0 ", 1),
        ):
            self.assertFalse(blocked_stdout_write(value))
        for value in ("", "1 1", "bad " * 9):
            with self.assertRaises(ProbeError):
                blocked_stdout_write(value)

    def test_feed_and_backpressure_trigger_from_observations_and_resume_original_read(self) -> None:
        for case in ("feed", "stdout_backpressure"):
            with self.subTest(case=case), ExitStack() as stack:
                owner, pipes = real_owner(stack, case)
                owner.input_count = 8
                owner.output_count = 4
                stack.enter_context(patch(f"{MODULE}.native_processes", return_value={42}))
                stack.enter_context(patch(f"{MODULE}.emit"))
                observed = stack.enter_context(patch(f"{MODULE}.backpressure", return_value=None))
                os.write(pipes.output_write, b"abcd")
                owner.io_step(0)
                if case == "stdout_backpressure":
                    self.assertIsNone(owner.cancel.started)
                    self.assertEqual(owner.output_count, 4)  # Consumption really paused.
                    observed.return_value = {"pipe_capacity": 4, "unread_bytes": 4}
                    owner.check_trigger()
                else:
                    self.assertEqual(owner.input_count, 8)  # No additional write after PCM trigger.
                self.assertIsNotNone(owner.cancel.started)
                before = owner.output_count
                owner.io_step(0)
                self.assertGreaterEqual(owner.output_count, before)
                if case == "stdout_backpressure":
                    self.assertEqual(owner.output_count, 8)
                    self.assertEqual(owner.drained, 4)
                self.assertLessEqual(owner.input_count, PREFIX_BYTES)

    def test_cancel_during_setup_wakes_without_resetting_deadline(self) -> None:
        clock = Clock()
        owner = CancelRun("feed")
        owner.child = Child(42)
        owner.phase_deadline = 25.0

        def wait(
            reads: list[int], writes: list[int], errors: list[int], timeout: float
        ) -> tuple[list[int], list[int], list[int]]:
            owner.request("external")
            clock.now += timeout
            return [], [], []

        with (
            patch(f"{MODULE}.time.monotonic", side_effect=clock.monotonic),
            patch(f"{MODULE}.select.select", side_effect=wait),
            patch.object(Child, "reap"),
            self.assertRaises(ProbeError),
        ):
            owner.wait_ready(100)
        self.assertLess(clock.now, 10.1)
        self.assertEqual(owner.cancel.deadline, 13.0)

    def test_setup_cleanup_before_setsid_signals_only_owned_direct_child(self) -> None:
        with ExitStack() as stack:
            owner, _ = real_owner(stack)
            owner.exec_released = False
            owner.start_ticks = 0
            owner.request("setup_failed")
            stack.enter_context(patch(f"{MODULE}.os.getpgid", return_value=99))
            group = stack.enter_context(patch(f"{MODULE}.os.killpg"))
            direct = stack.enter_context(patch(f"{MODULE}.os.kill"))
            owner.manage_cancel()
            group.assert_not_called()
            self.assertEqual(
                [call.args for call in direct.call_args_list],
                [(42, signal.SIGTERM), (42, signal.SIGCONT)],
            )
            self.assertEqual(owner.start_ticks, 123)

    def test_partial_pipe_acquisition_is_owned_when_setup_fails(self) -> None:
        original_pipe = os.pipe
        for fail_at in (1, 2, 5):
            with self.subTest(fail_at=fail_at), ExitStack() as stack:
                owner = CancelRun("feed")
                acquired: list[int] = []
                calls = 0

                def failing_pipe(
                    at: int = fail_at, retained: list[int] = acquired
                ) -> tuple[int, int]:
                    nonlocal calls
                    calls += 1
                    if calls == at:
                        raise OSError(errno.EMFILE, "no descriptor")
                    pair = original_pipe()
                    retained.extend(pair)
                    for fd in pair:
                        stack.callback(close_if_open, fd)
                    return pair

                with (
                    patch(f"{MODULE}.os.pipe", side_effect=failing_pipe),
                    self.assertRaises(OSError),
                ):
                    owner.pipes = make_pipes(owner.fds)
                self.assertEqual(owner.fds, acquired)
                owner.fail("setup failed")
                owner.manage_cancel()
                self.assertFalse(owner.fds)
                with patch(f"{MODULE}.native_processes", return_value=set()):
                    self.assertTrue(owner.release_if_done())

    def test_setup_failure_without_pcm_closes_all_acquired_resources(self) -> None:
        with ExitStack() as stack:
            owner = CancelRun("feed")
            owner.pipes = make_pipes(owner.fds)
            acquired = list(owner.fds)
            for fd in acquired:
                stack.callback(close_if_open, fd)
            owner.fail("fork failed")
            owner.manage_cancel()
            owner.io_step(0)
            with patch(f"{MODULE}.native_processes", return_value=set()):
                self.assertTrue(owner.release_if_done())
            self.assertFalse(owner.buffers)
            self.assertFalse(owner.fds)
            for fd in acquired:
                with self.assertRaises(OSError):
                    os.fstat(fd)

    def test_native_diagnostics_do_not_promote_cancel_to_complete_audio(self) -> None:
        validate_cancel_diagnostics(DIAGNOSTICS)
        for data in (
            DIAGNOSTICS.replace(b"mpg123: death by SIGTERM\n", b""),
            DIAGNOSTICS + b"mpg123: death by SIGTERM\n",
            DIAGNOSTICS + b"unknown\n",
        ):
            with self.assertRaises(ProbeError):
                validate_cancel_diagnostics(data)

    def test_actual_prepare_and_pump_recover_fork_and_preexec_limit_failures(self) -> None:
        for failure in ("fork", "limits"):
            with self.subTest(failure=failure), ExitStack() as stack:
                owner = CancelRun("feed")
                stack.enter_context(patch(f"{MODULE}.CancelRun", return_value=owner))
                stack.enter_context(patch(f"{MODULE}.emit"))
                stack.enter_context(
                    patch(
                        f"{MODULE}.os.uname",
                        return_value=os.uname_result(("Linux", "offline", "", "", "x86_64")),
                    )
                )
                stack.enter_context(patch(f"{MODULE}.os.sysconf", return_value=4096))
                stack.enter_context(patch.object(Path, "iterdir", return_value=iter([Path("1")])))
                stack.enter_context(patch(f"{MODULE}.native_processes", return_value=set()))
                stack.enter_context(
                    patch(
                        f"{MODULE}.read_text",
                        side_effect=setup_text,
                    )
                )
                stack.enter_context(
                    patch(f"{MODULE}.counters", return_value={"oom": 0, "oom_kill": 0})
                )
                stack.enter_context(patch(f"{MODULE}.verify_native_files"))
                stack.enter_context(patch(f"{MODULE}.read_start_identity", return_value=("S", 123)))
                stack.enter_context(patch.object(Child, "reap"))
                stack.enter_context(patch(f"{MODULE}.os.getpgid", return_value=42))
                stack.enter_context(patch(f"{MODULE}.group_members", return_value=[42]))

                def fork(owner: CancelRun = owner, failure: str = failure) -> int:
                    self.assertFalse(owner.buffers)
                    self.assertFalse(owner.views)
                    self.assertIsNotNone(owner.pipes)
                    for fd in owner.fds:
                        stack.callback(close_if_open, fd)
                    if failure == "fork":
                        raise OSError(errno.EAGAIN, "fork failed")
                    assert owner.pipes is not None
                    os.write(owner.pipes.ready_write, b"R")
                    return 42

                def signalled(pid: int, sig: signal.Signals, owner: CancelRun = owner) -> None:
                    owner.child.exit_code = -15

                stack.enter_context(patch(f"{MODULE}.os.fork", side_effect=fork))
                stack.enter_context(patch(f"{MODULE}.os.killpg", side_effect=signalled))
                stack.enter_context(
                    patch(f"{MODULE}.verify_child", side_effect=ProbeError("limits failed"))
                )
                result = run_case("feed")
                self.assertEqual(result["verdict"], "incomplete")
                self.assertIn(failure, str(result["failure"]))
                self.assertFalse(result["cleanup_debt"])
                self.assertTrue(result["parent_payload_views_released"])
                self.assertEqual(result["input_bytes"], 0)

    def test_cancellation_during_banner_and_stop_observation_preserves_owner(self) -> None:
        for phase in ("banner", "stop"):
            with self.subTest(phase=phase), ExitStack() as stack:
                owner, pipes = real_owner(stack)
                if phase == "stop":
                    owner.stderr = bytearray(BANNER)
                stack.enter_context(patch(f"{MODULE}.os.kill"))
                stack.enter_context(patch(f"{MODULE}.os.waitpid", return_value=(0, 0)))

                def wait(
                    reads: list[int],
                    writes: list[int],
                    errors: list[int],
                    timeout: float,
                    owner: CancelRun = owner,
                ) -> tuple[list[int], list[int], list[int]]:
                    owner.request("external_during_setup")
                    return [], [], []

                stack.enter_context(patch(f"{MODULE}.select.select", side_effect=wait))
                with self.assertRaises(ProbeError):
                    owner.admit_native(pipes)
                self.assertTrue(owner.fds)
                self.assertTrue(owner.views)
                self.assertFalse(owner.release_if_done())

    def test_deadline_snapshot_distinguishes_debt_unknown_inventory_and_late_release(self) -> None:
        with ExitStack() as stack:
            owner, pipes = real_owner(stack)
            owner.request("test_trigger")
            owner.busy.add(pipes.output_read)
            with patch(
                f"{MODULE}.native_processes", side_effect=PermissionError("unknown inventory")
            ):
                retained = owner.release_snapshot()
            self.assertTrue(retained["cleanup_debt"])
            self.assertTrue(retained["payload_lease_owned"])
            self.assertEqual(retained["original_io_in_flight"], [pipes.output_read])
            self.assertEqual(retained["inventory_status"], "unavailable")
            self.assertNotIn("retained_pids", retained)
            owner.busy.clear()
            owner.child.exit_code = 0
            for fd in tuple(owner.fds):
                owner.close(fd)
            assert owner.cancel.deadline is not None
            with patch(f"{MODULE}.time.monotonic", return_value=owner.cancel.deadline + 0.1):
                self.assertTrue(owner.release_if_done())
            released = owner.release_snapshot()
            self.assertTrue(owner.cancel.deadline_missed)
            self.assertFalse(released["cleanup_debt"])
            self.assertFalse(released["payload_lease_owned"])
            self.assertEqual(released["retained_pids"], [])

    def test_real_pump_releases_after_observed_eof_and_reaping_once(self) -> None:
        with ExitStack() as stack:
            owner, pipes = real_owner(stack)
            owner.stderr = bytearray(DIAGNOSTICS)
            owner.memory_before = {"oom": 0, "oom_kill": 0}
            stack.enter_context(patch(f"{MODULE}.CancelRun", return_value=owner))
            stack.enter_context(patch(f"{MODULE}.emit"))
            stack.enter_context(patch(f"{MODULE}.read_text", return_value="0"))
            stack.enter_context(patch(f"{MODULE}.counters", return_value=owner.memory_before))
            stack.enter_context(patch(f"{MODULE}.os.getpgid", return_value=42))
            stack.enter_context(patch(f"{MODULE}.group_members", return_value=[42]))

            def prepare() -> None:
                owner.trigger = {"case": "feed"}
                owner.request("controlled_trigger")
                os.write(pipes.output_write, b"abcd")

            def signal_sent(pid: int, sig: signal.Signals) -> None:
                if sig == signal.SIGTERM:
                    os.close(pipes.output_write)
                    os.close(pipes.error_write)
                    owner.child.exit_code = 0

            stack.enter_context(patch.object(owner, "prepare", side_effect=prepare))
            stack.enter_context(patch(f"{MODULE}.os.killpg", side_effect=signal_sent))
            result = run_case("feed")
            self.assertEqual(result["verdict"], "direct_decoder_cancel_pass")
            self.assertEqual(result["cancel_requests"], 2)
            self.assertEqual(result["drained_after_cancel_bytes"], 4)
            self.assertEqual(result["retained_fds"], [])
            self.assertTrue(result["parent_payload_views_released"])
            self.assertFalse(result["cleanup_debt"])

    def test_failed_deadline_or_backstop_emit_keeps_actual_loop_until_later_release(self) -> None:
        original_select = select.select
        for event in ("original_cleanup_deadline_failed", "native_retention_backstop_failed"):
            with self.subTest(event=event), ExitStack() as stack:
                clock = Clock()
                stack.enter_context(patch(f"{MODULE}.time.monotonic", side_effect=clock.monotonic))
                owner, pipes = real_owner(stack)
                owner.stderr = bytearray(DIAGNOSTICS)
                owner.memory_before = {"oom": 0, "oom_kill": 0}
                attempts: list[str] = []
                if event == "native_retention_backstop_failed":
                    owner.started = 0.0
                    clock.now = 50.0
                stack.enter_context(patch(f"{MODULE}.CancelRun", return_value=owner))
                stack.enter_context(patch(f"{MODULE}.read_text", return_value="0"))
                stack.enter_context(patch(f"{MODULE}.counters", return_value=owner.memory_before))
                stack.enter_context(patch(f"{MODULE}.os.getpgid", return_value=42))
                stack.enter_context(patch(f"{MODULE}.group_members", return_value=[42]))

                def prepare(owner: CancelRun = owner, pipes: Pipes = pipes) -> None:
                    owner.trigger = {"case": "feed"}
                    owner.request("controlled_trigger")
                    # Represent an original input operation that has not returned yet.
                    owner.busy.add(pipes.input_write)
                    os.close(pipes.output_write)
                    os.close(pipes.error_write)

                def signalled(pid: int, sig: signal.Signals, owner: CancelRun = owner) -> None:
                    owner.child.exit_code = 0

                def broken_sink(
                    record: dict[str, object],
                    owner: CancelRun = owner,
                    attempts: list[str] = attempts,
                    event: str = event,
                ) -> None:
                    self.assertEqual(record["event"], event)
                    attempts.append(str(record["event"]))
                    self.assertTrue(owner.busy)
                    self.assertTrue(owner.fds)
                    self.assertTrue(owner.views)
                    self.assertTrue(record["cleanup_debt"])
                    with self.assertRaises(ProbeError):
                        owner.require_released()
                    raise BrokenPipeError("evidence sink closed while original I/O retained")

                def io_ready(
                    reads: list[int],
                    writes: list[int],
                    errors: list[int],
                    timeout: float,
                    owner: CancelRun = owner,
                    attempts: list[str] = attempts,
                    clock: Clock = clock,
                    event: str = event,
                ) -> tuple[list[int], list[int], list[int]]:
                    if event == "original_cleanup_deadline_failed":
                        clock.now = 14.0
                    if attempts:
                        # A later turn observes actual I/O completion, never the failed emit itself.
                        owner.busy.clear()
                    return original_select(reads, writes, errors, 0)

                stack.enter_context(patch.object(owner, "prepare", side_effect=prepare))
                stack.enter_context(patch(f"{MODULE}.os.killpg", side_effect=signalled))
                stack.enter_context(patch(f"{MODULE}.select.select", side_effect=io_ready))
                stack.enter_context(patch(f"{MODULE}.emit", side_effect=broken_sink))
                result = run_case("feed")
                self.assertEqual(attempts, [event])
                self.assertEqual(result["verdict"], "incomplete")
                self.assertFalse(result["cleanup_debt"])
                self.assertTrue(result["parent_payload_views_released"])
                self.assertEqual(result["retained_fds"], [])
                self.assertEqual(result["cancel_requests"], 2)
                if event == "original_cleanup_deadline_failed":
                    self.assertEqual(result["cancel_deadline"], 13.0)
                    self.assertFalse(result["cleanup_deadline_met"])
                    self.assertIn("evidence sink closed", str(result["failure"]))


if __name__ == "__main__":
    unittest.main()
