"""Deterministic real-pipe topology schedules; never fork or invoke a native workload."""

from __future__ import annotations

import errno
import os
import select
import signal
import unittest
from contextlib import ExitStack, suppress
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from .decoder_startup import Child
from .owned_mp3_cancel import run_owner
from .owned_mp3_topology import ProcessIdentity, TopologyRun, descriptor_inventory
from .probe import ProbeError
from .test_cancel_contract import DIAGNOSTICS
from .topology_protocol import (
    TERM_BIT,
    Launcher,
    Record,
    StatusReader,
    TopologyCase,
    TopologyPipes,
    topology_decoder,
    topology_pipes,
)

MODULE = "evaluations.linux_resource.owned_mp3_topology"
HELPER = "evaluations.linux_resource.topology_protocol"
BASE = "evaluations.linux_resource.owned_mp3_cancel"
STOP = (signal.SIGSTOP << 8) | 0x7F
HASH = "a" * 64


def close_if_open(fd: int) -> None:
    with suppress(OSError):
        os.close(fd)


def preflight_text(path: Path) -> str:
    if path.name == "core_pattern":
        return "core"
    if path.name == "memory.events":
        return "oom 0\noom_kill 0\n"
    return "0"


def preflight(stack: ExitStack) -> None:
    stack.enter_context(
        patch(
            f"{MODULE}.os.uname",
            return_value=os.uname_result(("Linux", "offline", "", "", "x86_64")),
        )
    )
    stack.enter_context(patch(f"{MODULE}.os.sysconf", return_value=4096))
    stack.enter_context(patch.object(Path, "iterdir", return_value=iter([Path("1")])))
    stack.enter_context(patch(f"{MODULE}.native_processes", return_value=set()))
    stack.enter_context(patch(f"{BASE}.native_processes", return_value=set()))
    stack.enter_context(patch(f"{MODULE}.read_text", side_effect=preflight_text))
    stack.enter_context(patch(f"{BASE}.read_text", side_effect=preflight_text))
    stack.enter_context(patch(f"{MODULE}.verify_native_files"))
    stack.enter_context(patch(f"{MODULE}.executable_identity", return_value={"sha256": HASH}))
    stack.enter_context(patch(f"{MODULE}.emit"))


def owned_topology(
    stack: ExitStack, case: TopologyCase = "ignoring_launcher"
) -> tuple[TopologyRun, TopologyPipes]:
    owner = TopologyRun(case, HASH)
    t = topology_pipes(owner.fds)
    for fd in owner.fds:
        stack.callback(close_if_open, fd)
    owner.topology, owner.pipes = t, t.media
    # Peers belong to the deterministic test fixture, not to fake native processes.
    for fd in tuple(owner.fds):
        if fd not in t.parent_ends():
            owner.fds.remove(fd)
    owner.close(t.media.ready_read)
    owner.close(t.media.gate_write)
    owner.child = Child(42)
    owner.start_ticks = 123
    owner.decoder_pid, owner.decoder_start = 43, 234
    owner.input_released = owner.exec_released = True
    owner.protocol.accept(Record("H", 43, 234, 0).pack())
    owner.protocol.commands.add("S")
    owner.protocol.accept(Record("S", 43, 234, STOP).pack())
    owner.buffers = [bytearray(64), bytearray(64), bytearray(128)]
    owner.views = [memoryview(buffer) for buffer in owner.buffers]
    owner.bank = owner.views[0]
    with owner.views[2][:64] as source:
        owner.input_view = source.toreadonly()
    owner.output_view = owner.views[2][64:]
    owner.views.extend((owner.input_view, owner.output_view))
    for fd in (*owner.fds, t.command_read, t.status_write):
        os.set_blocking(fd, False)
    stack.enter_context(patch(f"{BASE}.CHUNK_BYTES", 64))
    stack.enter_context(patch(f"{MODULE}.native_processes", return_value=set()))
    stack.enter_context(patch(f"{BASE}.native_processes", return_value=set()))
    stack.enter_context(patch(f"{MODULE}.group_members", return_value=[]))
    stack.enter_context(patch(f"{MODULE}.read_start_identity", side_effect=FileNotFoundError))
    return owner, t


class Clock:
    now = 10.0

    def monotonic(self) -> float:
        if self.now > 30.0:
            raise AssertionError("deterministic topology schedule exhausted")
        return self.now


class ProtocolContracts(unittest.TestCase):
    def test_partial_status_requires_actual_commanded_stop_and_keeps_wait_status(self) -> None:
        reader = StatusReader()
        hello = Record("H", 43, 234, 0).pack()
        self.assertEqual(len(hello), 17)
        for byte in hello[:-1]:
            self.assertEqual(reader.accept(bytes([byte])), [])
        self.assertEqual(reader.pid, 0)
        self.assertEqual(reader.accept(hello[-1:])[0].pid, 43)
        reader.commands.add("S")
        reader.accept(Record("S", 43, 234, STOP).pack())
        reader.commands.add("P")
        reader.accept(Record("P", 43, 234, 0).pack() + Record("X", 43, 234, 7 << 8).pack())
        self.assertEqual(reader.decoder_status, 7)
        self.assertEqual(reader.total, 68)
        reader.eof()

    def test_bad_status_lengths_order_identity_wait_words_and_bounds_fail_closed(self) -> None:
        bad = [
            Record("Z", 43, 234, 0),
            Record("H", 1, 234, 0),
            Record("H", 43, 0, 0),
            Record("H", 43, 234, 1),
        ]
        for record in bad:
            with self.subTest(record=record), self.assertRaises(ProbeError):
                StatusReader().accept(record.pack())
        for record in (
            Record("S", 43, 234, STOP),
            Record("P", 43, 234, 0),
            Record("X", 43, 999, 0),
            Record("X", 43, 234, STOP),
            Record("H", 43, 234, 0),
        ):
            reader = StatusReader()
            reader.accept(Record("H", 43, 234, 0).pack())
            with self.subTest(record=record), self.assertRaises(ProbeError):
                reader.accept(record.pack())
        reader = StatusReader()
        reader.accept(b"H")
        with self.assertRaises(ProbeError):
            reader.eof()
        with self.assertRaises(ProbeError):
            StatusReader().accept(b"x" * 86)
        with self.assertRaises(ProbeError):
            StatusReader().accept(Record("E", 0, 0, 2).pack())

    def test_every_partial_pipe_acquisition_remains_in_original_owner(self) -> None:
        original_pipe = os.pipe
        for fail_at in range(1, 8):
            with self.subTest(fail_at=fail_at), ExitStack() as stack:
                owner = TopologyRun("ignoring_launcher", HASH)
                count = 0

                def acquire(at: int = fail_at) -> tuple[int, int]:
                    nonlocal count
                    count += 1
                    if count == at:
                        raise OSError(errno.EMFILE, "pipe acquisition failed")
                    pair = original_pipe()
                    for fd in pair:
                        stack.callback(close_if_open, fd)
                    return pair

                with patch(f"{HELPER}.os.pipe", side_effect=acquire), self.assertRaises(OSError):
                    owner.topology = topology_pipes(owner.fds)
                self.assertEqual(len(owner.fds), 2 * (fail_at - 1))
                owner.fail("partial acquisition")
                owner.manage_cancel()
                self.assertFalse(owner.fds)

    def test_launcher_setup_forks_before_ignoring_term_and_retains_controls_only(self) -> None:
        with ExitStack() as stack:
            fds: list[int] = []
            t = topology_pipes(fds)
            original = list(fds)
            for fd in fds:
                stack.callback(close_if_open, fd)
            launcher = Launcher("ignoring_launcher", t, fds)
            events: list[str] = []

            def fork() -> int:
                events.append("fork")
                self.assertEqual(launcher.owned, original)
                return 43

            def disposition(sig: signal.Signals, handler: signal.Handlers) -> None:
                if handler == signal.SIG_IGN:
                    events.append("ignore")

            stack.enter_context(patch(f"{HELPER}.signal.signal", side_effect=disposition))
            stack.enter_context(patch(f"{HELPER}.os.setsid"))
            stack.enter_context(patch(f"{HELPER}.resource.setrlimit"))
            stack.enter_context(patch(f"{HELPER}.os.fork", side_effect=fork))
            stack.enter_context(patch(f"{HELPER}.read_start_identity", return_value=("S", 234)))
            duplicates = stack.enter_context(patch(f"{HELPER}.os.dup2"))
            launcher.setup()
            self.assertEqual(events, ["fork", "ignore"])
            self.assertEqual(launcher.owned, [t.command_read, t.status_write])
            self.assertEqual([call.args[1] for call in duplicates.call_args_list], [0, 1, 2])
            for fd in original:
                if fd not in launcher.owned:
                    with self.assertRaises(OSError):
                        os.fstat(fd)
            self.assertTrue(launcher.hello)

    def test_second_fork_failure_does_not_discard_any_acquired_descriptor(self) -> None:
        with ExitStack() as stack:
            fds: list[int] = []
            t = topology_pipes(fds)
            for fd in fds:
                stack.callback(close_if_open, fd)
            launcher = Launcher("ignoring_launcher", t, fds)
            stack.enter_context(patch(f"{HELPER}.signal.signal"))
            stack.enter_context(patch(f"{HELPER}.os.setsid"))
            stack.enter_context(patch(f"{HELPER}.resource.setrlimit"))
            stack.enter_context(
                patch(f"{HELPER}.os.fork", side_effect=OSError(errno.EAGAIN, "fork"))
            )
            with self.assertRaises(OSError):
                launcher.setup()
            self.assertEqual(len(fds), 14)
            for fd in fds:
                os.fstat(fd)
            self.assertEqual(launcher.decoder.pid, 0)
            self.assertEqual(launcher.failure, 2)

    def test_decoder_child_does_not_create_a_session_or_keep_control_endpoints(self) -> None:
        class Exited(BaseException):
            pass

        with ExitStack() as stack:
            fds = list(range(10, 24))
            from .owned_mp3_overlap import Pipes

            t = TopologyPipes(Pipes(*fds[:10]), *fds[10:])
            stack.enter_context(patch(f"{HELPER}.signal.signal"))
            sid = stack.enter_context(patch(f"{HELPER}.os.setsid"))
            fork = stack.enter_context(patch(f"{HELPER}.os.fork"))
            stack.enter_context(patch(f"{HELPER}.os.dup2"))
            close = stack.enter_context(patch(f"{HELPER}.os.close"))
            stack.enter_context(patch(f"{HELPER}.os.read", return_value=b"x"))
            stack.enter_context(patch(f"{HELPER}.os.write", return_value=1))
            limits = stack.enter_context(patch(f"{HELPER}.resource.setrlimit"))
            execute = stack.enter_context(
                patch(f"{HELPER}.os.execve", side_effect=OSError("offline"))
            )
            stack.enter_context(patch(f"{HELPER}.os._exit", side_effect=Exited))
            with self.assertRaises(Exited):
                topology_decoder(t, fds)
            sid.assert_not_called()
            fork.assert_not_called()
            self.assertEqual(execute.call_args.args[0], "/usr/bin/mpg123")
            self.assertEqual(len(limits.call_args_list), 4)
            closed = {call.args[0] for call in close.call_args_list}
            self.assertTrue(set(fds[10:]) <= closed)
            self.assertNotIn(t.media.ready_write, closed)

    def test_launcher_actual_control_pipes_report_stop_pong_and_terminal_wait_once(self) -> None:
        with ExitStack() as stack:
            fds: list[int] = []
            t = topology_pipes(fds)
            for fd in fds:
                stack.callback(close_if_open, fd)
            for fd in (t.command_read, t.status_write, t.status_read):
                os.set_blocking(fd, False)
            launcher = Launcher("ignoring_launcher", t, fds)
            launcher.decoder.pid, launcher.start, launcher.hello = 43, 234, True
            wait = stack.enter_context(patch(f"{HELPER}.os.waitpid", return_value=(0, 0)))
            kill = stack.enter_context(patch(f"{HELPER}.os.kill"))
            launcher.step()
            self.assertEqual(os.read(t.status_read, 17), Record("H", 43, 234, 0).pack())
            os.write(t.command_write, b"S")
            launcher.step()
            kill.assert_called_once_with(43, signal.SIGSTOP)
            wait.return_value = (43, STOP)
            launcher.step()
            self.assertEqual(os.read(t.status_read, 17), Record("S", 43, 234, STOP).pack())
            wait.return_value = (0, 0)
            os.write(t.command_write, b"P")
            launcher.step()
            launcher.step()
            self.assertEqual(os.read(t.status_read, 17), Record("P", 43, 234, 0).pack())
            wait.return_value = (43, 0)
            launcher.step()
            self.assertEqual(os.read(t.status_read, 17), Record("X", 43, 234, 0).pack())
            wait.reset_mock()
            launcher.step()
            wait.assert_not_called()
            self.assertFalse(launcher.command(b"Q"))
            self.assertEqual(launcher.failure, 6)

    def test_launcher_broken_status_or_unknown_wait_never_causes_implicit_exit(self) -> None:
        with ExitStack() as stack:
            fds: list[int] = []
            t = topology_pipes(fds)
            for fd in fds:
                stack.callback(close_if_open, fd)
            launcher = Launcher("ignoring_launcher", t, fds)
            launcher.hello, launcher.decoder.pid, launcher.start = True, 43, 234
            os.close(t.status_read)
            stack.enter_context(patch(f"{HELPER}.os.waitpid", side_effect=ChildProcessError))
            self.assertFalse(launcher.step())
            self.assertTrue(launcher.wait_uncertain)
            self.assertIsNone(launcher.exit_status)
            self.assertFalse(launcher.controls)
            self.assertFalse(launcher.step())


class TopologyOwnerContracts(unittest.TestCase):
    def test_first_fork_failure_runs_actual_preparation_cleanup_without_allocating_payload(
        self,
    ) -> None:
        with ExitStack() as stack:
            owner = TopologyRun("ignoring_launcher", HASH)
            preflight(stack)
            loaded = stack.enter_context(patch(f"{MODULE}.load_fixture_snapshot"))

            def fork() -> int:
                self.assertEqual(len(owner.fds), 14)
                self.assertFalse(owner.buffers)
                self.assertFalse(owner.views)
                for fd in owner.fds:
                    stack.callback(close_if_open, fd)
                raise OSError(errno.EAGAIN, "first fork failed")

            stack.enter_context(patch(f"{MODULE}.os.fork", side_effect=fork))
            result = run_owner(owner)
            loaded.assert_not_called()
            self.assertEqual(result["verdict"], "incomplete")
            self.assertIn("first fork failed", str(result["failure"]))
            self.assertFalse(result["cleanup_debt"])
            self.assertEqual(result["retained_fds"], [])
            self.assertEqual(result["input_bytes"], 0)

    def test_fork_copy_cannot_fall_back_into_controller_after_launcher_entry_failure(self) -> None:
        class Exited(BaseException):
            pass

        with ExitStack() as stack:
            owner = TopologyRun("ignoring_launcher", HASH)
            preflight(stack)

            def fork() -> int:
                self.assertFalse(owner.buffers)
                for fd in owner.fds:
                    stack.callback(close_if_open, fd)
                return 0

            stack.enter_context(patch(f"{MODULE}.os.fork", side_effect=fork))
            stack.enter_context(patch(f"{MODULE}.launcher_main", side_effect=MemoryError))
            exited = stack.enter_context(patch(f"{MODULE}.os._exit", side_effect=Exited))
            with self.assertRaises(Exited):
                owner.prepare()
            exited.assert_called_once_with(125)
            self.assertFalse(owner.buffers)
            self.assertEqual(owner.child.pid, 0)

    def test_partial_hello_cancellation_recovers_original_descendant_then_rejects_conflict(
        self,
    ) -> None:
        with ExitStack() as stack:
            owner, t = owned_topology(stack)
            owner.protocol = StatusReader()
            owner.decoder_pid = owner.decoder_start = 0
            hello = Record("H", 43, 999, 0).pack()
            os.write(t.status_write, hello[:8])
            owner.control_step()
            self.assertEqual(owner.decoder_pid, 0)
            owner.request("partial HELLO")
            with (
                patch(f"{MODULE}.group_members", return_value=[42, 43]),
                patch(
                    f"{MODULE}.process_identity",
                    return_value=ProcessIdentity(43, 234, 42, 42, 42, "S", 0, 0),
                ),
            ):
                owner.recover_decoder()
            self.assertEqual((owner.decoder_pid, owner.decoder_start), (43, 234))
            os.write(t.status_write, hello[8:])
            with self.assertRaises(ProbeError):
                owner.control_step()
            self.assertTrue(owner.identity_debt)
            self.assertFalse(owner.release_if_done())

    def test_missing_pong_never_extends_the_fixed_kill_point(self) -> None:
        clock = Clock()
        with ExitStack() as stack:
            stack.enter_context(patch(f"{BASE}.time.monotonic", side_effect=clock.monotonic))
            owner, t = owned_topology(stack)
            owner.trigger = {"case": "ignoring_launcher"}
            owner.request("trigger")
            stack.enter_context(patch.object(Child, "reap"))
            stack.enter_context(patch(f"{MODULE}.group_members", return_value=[42, 43]))
            stack.enter_context(
                patch(
                    f"{MODULE}.process_identity",
                    return_value=ProcessIdentity(42, 123, os.getpid(), 42, 42, "S", TERM_BIT, 0),
                )
            )
            killed = stack.enter_context(patch(f"{MODULE}.os.killpg"))
            owner.manage_cancel()
            self.assertNotIn(t.media.input_write, owner.fds)
            clock.now = 11.0
            owner.manage_cancel()
            self.assertEqual(
                [call.args[1] for call in killed.call_args_list],
                [signal.SIGTERM, signal.SIGCONT, signal.SIGKILL],
            )
            self.assertIn("PONG", str(owner.failure))
            self.assertEqual(owner.cancel.deadline, 13.0)
            self.assertEqual(owner.kill_sent, 11.0)

    def test_uncertain_launcher_reap_cannot_authorize_a_reused_group_signal(self) -> None:
        with ExitStack() as stack:
            owner, _ = owned_topology(stack)
            owner.launcher_reap_unknown = owner.identity_debt = True
            owner.decoder_pid = owner.decoder_start = 0
            stack.enter_context(patch(f"{MODULE}.group_members", return_value=[42]))
            killed = stack.enter_context(patch(f"{MODULE}.os.killpg"))
            with self.assertRaises(ProbeError):
                owner.signal_group(signal.SIGKILL)
            killed.assert_not_called()

    def test_command_delivery_retains_original_io_and_failed_write_is_not_reissued(self) -> None:
        original = os.write
        with ExitStack() as stack:
            owner, t = owned_topology(stack)
            owner.queue_command("P")

            def delivered(fd: int, data: bytes) -> int:
                amount = original(fd, data)
                owner.request("during_control_delivery")
                owner.request("repeat_during_control_delivery")
                owner.close(fd)
                self.assertIn(fd, owner.busy)
                self.assertIn(fd, owner.fds)
                self.assertFalse(owner.release_if_done())
                with self.assertRaises(ProbeError):
                    owner.require_released()
                return amount

            with patch(f"{MODULE}.os.write", side_effect=delivered):
                owner.control_step()
            self.assertEqual(os.read(t.command_read, 1), b"P")
            self.assertIn("P", owner.protocol.commands)
            self.assertEqual(owner.cancel.requests, 2)
        with ExitStack() as stack:
            owner, t = owned_topology(stack)
            owner.queue_command("P")
            with patch(f"{MODULE}.os.write", side_effect=BrokenPipeError) as write:
                with self.assertRaises(BrokenPipeError):
                    owner.control_step()
                owner.control_step()
                self.assertEqual(write.call_count, 1)
            self.assertIsNone(owner.pending_command)
            self.assertNotIn(t.command_write, owner.fds)

    def test_descendant_zombie_group_presence_and_unknown_identity_keep_lease(self) -> None:
        with ExitStack() as stack:
            owner, _ = owned_topology(stack, "descendant_after_leader_exit")
            owner.request("trigger")
            owner.child.exit_code = 0
            for fd in tuple(owner.fds):
                owner.close(fd)
            with patch(f"{MODULE}.read_start_identity", return_value=("Z", 234)):
                self.assertFalse(owner.release_if_done())
                self.assertTrue(owner.views)
            with patch(f"{MODULE}.group_members", return_value=[43]):
                self.assertFalse(owner.release_if_done())
            with patch(f"{MODULE}.read_start_identity", side_effect=PermissionError):
                with self.assertRaises(PermissionError):
                    owner.release_if_done()
                self.assertTrue(owner.views)
            with patch(f"{MODULE}.read_start_identity", return_value=("S", 999)):
                self.assertFalse(owner.release_if_done())
            self.assertTrue(owner.identity_debt)
            self.assertFalse(owner.release_if_done())

    def test_echild_does_not_invent_launcher_reaping_or_release(self) -> None:
        with ExitStack() as stack:
            owner, _ = owned_topology(stack)
            owner.request("trigger")
            with patch.object(Child, "reap", side_effect=ChildProcessError):
                owner.reap_child()
            self.assertIsNone(owner.child.exit_code)
            self.assertTrue(owner.launcher_reap_unknown)
            self.assertTrue(owner.identity_debt)
            self.assertFalse(owner.release_if_done())

    def test_wrong_signal_disposition_and_changed_ancestry_fail_before_admission(self) -> None:
        with ExitStack() as stack:
            owner, _ = owned_topology(stack)
            good = ProcessIdentity(42, 123, os.getpid(), 42, 42, "S", TERM_BIT, 0)
            for bad in (
                replace(good, ignored=0),
                replace(good, blocked=TERM_BIT),
                replace(good, parent=1),
                replace(good, group=99),
                replace(good, session=99),
            ):
                with (
                    self.subTest(bad=bad),
                    patch(f"{MODULE}.process_identity", return_value=bad),
                    self.assertRaises(ProbeError),
                ):
                    owner.launcher_identity()

    def test_descriptor_check_rejects_an_extra_inherited_endpoint(self) -> None:
        with (
            patch.object(Path, "iterdir", return_value=iter([Path("0"), Path("1")])),
            self.assertRaises(ProbeError),
        ):
            descriptor_inventory(43, {0: (99, os.O_RDONLY)})

    def test_descriptor_check_reads_actual_pipe_identity_and_direction(self) -> None:
        with ExitStack() as stack:
            reader, writer = os.pipe()
            wrong_reader, wrong_writer = os.pipe()
            for fd in (reader, writer, wrong_reader, wrong_writer):
                stack.callback(close_if_open, fd)
            for peer, direction, valid in (
                (writer, os.O_RDONLY, True),
                (writer, os.O_WRONLY, False),
                (wrong_writer, os.O_RDONLY, False),
            ):
                with (
                    self.subTest(peer=peer, direction=direction),
                    patch.object(Path, "iterdir", return_value=iter([Path(str(reader))])),
                ):
                    if valid:
                        evidence = descriptor_inventory(os.getpid(), {reader: (peer, direction)})
                        self.assertIn(str(reader), evidence)
                    else:
                        with self.assertRaises(ProbeError):
                            descriptor_inventory(os.getpid(), {reader: (peer, direction)})

    def test_stuck_control_completion_keeps_payload_after_deadline_then_late_release_stays_failed(
        self,
    ) -> None:
        clock = Clock()
        with ExitStack() as stack:
            stack.enter_context(patch(f"{BASE}.time.monotonic", side_effect=clock.monotonic))
            owner, t = owned_topology(stack)
            owner.request("trigger")
            owner.request("repeat")
            owner.child.exit_code = -9
            owner.busy.add(t.command_write)
            for fd in tuple(owner.fds):
                owner.close(fd)
            clock.now = 14.0
            owner.cancel.observe(clock.now)
            self.assertFalse(owner.release_if_done())
            self.assertTrue(owner.views)
            self.assertEqual(owner.cancel.deadline, 13.0)
            owner.busy.clear()
            owner.close(t.command_write)
            self.assertTrue(owner.release_if_done())
            self.assertTrue(owner.cancel.deadline_missed)
            self.assertFalse(owner.views)

    def test_original_owner_loop_completes_both_cases_with_real_control_and_media_pipes(
        self,
    ) -> None:
        original_select = select.select
        for case in ("ignoring_launcher", "descendant_after_leader_exit"):
            with self.subTest(case=case), ExitStack() as stack:
                selected: TopologyCase = (
                    "ignoring_launcher"
                    if case == "ignoring_launcher"
                    else "descendant_after_leader_exit"
                )
                clock = Clock()
                stack.enter_context(patch(f"{BASE}.time.monotonic", side_effect=clock.monotonic))
                owner, t = owned_topology(stack, selected)
                owner.stderr = bytearray(DIAGNOSTICS)
                owner.memory_before = {"oom": 0, "oom_kill": 0}
                alive = {42, 43}
                orphan = [False]
                exit_status = [0]
                self.assertTrue(owner.views)

                def identity(
                    pid: int,
                    alive: set[int] = alive,
                    orphan: list[bool] = orphan,
                    selected: TopologyCase = selected,
                ) -> ProcessIdentity:
                    if pid not in alive:
                        raise FileNotFoundError
                    return ProcessIdentity(
                        pid,
                        123 if pid == 42 else 234,
                        os.getpid() if pid == 42 else (1 if orphan[0] else 42),
                        42,
                        42,
                        "S",
                        TERM_BIT if pid == 42 and selected == "ignoring_launcher" else 0,
                        0,
                    )

                def start(pid: int, alive: set[int] = alive) -> tuple[str, int]:
                    if pid not in alive:
                        raise FileNotFoundError
                    return "S", 123 if pid == 42 else 234

                def waitpid(
                    pid: int,
                    flags: int,
                    alive: set[int] = alive,
                    exit_status: list[int] = exit_status,
                ) -> tuple[int, int]:
                    self.assertEqual(pid, 42)  # A never attempts waitpid on nondirect D.
                    return (0, 0) if 42 in alive else (42, exit_status[0])

                def kill(
                    pid: int,
                    sig: signal.Signals,
                    alive: set[int] = alive,
                    owner: TopologyRun = owner,
                    t: TopologyPipes = t,
                    exit_status: list[int] = exit_status,
                ) -> None:
                    self.assertEqual(pid, 42)
                    if sig == signal.SIGTERM:
                        self.assertIn(t.media.input_write, owner.fds)
                        alive.discard(43)
                        os.close(t.media.output_write)
                        os.close(t.media.error_write)
                    elif sig == signal.SIGKILL and 42 in alive:
                        self.assertTrue(owner.survived_term)
                        alive.remove(42)
                        exit_status[0] = int(signal.SIGKILL)
                        os.close(t.status_write)
                        os.close(t.command_read)

                def ready(
                    reads: list[int],
                    writes: list[int],
                    errors: list[int],
                    timeout: float,
                    alive: set[int] = alive,
                    clock: Clock = clock,
                    t: TopologyPipes = t,
                    orphan: list[bool] = orphan,
                ) -> tuple[list[int], list[int], list[int]]:
                    clock.now += 0.02
                    if 42 in alive:
                        with suppress(BlockingIOError):
                            command = os.read(t.command_read, 1)
                            if command == b"P":
                                os.write(t.status_write, Record("P", 43, 234, 0).pack())
                            elif command == b"Q":
                                alive.remove(42)
                                orphan[0] = True
                                os.close(t.status_write)
                                os.close(t.command_read)
                    return original_select(reads, writes, errors, 0)

                def prepare(owner: TopologyRun = owner, t: TopologyPipes = t) -> None:
                    owner.input_count = 8
                    os.write(t.media.output_write, b"abcd")

                def members(pid: int, alive: set[int] = alive) -> list[int]:
                    return sorted(alive)

                def inventory(alive: set[int] = alive) -> set[int]:
                    return set(alive)

                stack.enter_context(patch.object(owner, "prepare", side_effect=prepare))
                stack.enter_context(patch(f"{MODULE}.process_identity", side_effect=identity))
                stack.enter_context(patch(f"{MODULE}.read_start_identity", side_effect=start))
                stack.enter_context(patch(f"{MODULE}.group_members", side_effect=members))
                stack.enter_context(patch(f"{MODULE}.native_processes", side_effect=inventory))
                stack.enter_context(patch(f"{BASE}.native_processes", side_effect=inventory))
                stack.enter_context(patch(f"{MODULE}.descriptor_inventory", return_value={}))
                stack.enter_context(patch(f"{MODULE}.os.readlink", return_value="/usr/bin/mpg123"))
                stack.enter_context(patch(f"{MODULE}.os.waitpid", side_effect=waitpid))
                stack.enter_context(patch(f"{MODULE}.os.killpg", side_effect=kill))
                stack.enter_context(patch(f"{MODULE}.select.select", side_effect=ready))
                stack.enter_context(patch(f"{MODULE}.emit"))
                stack.enter_context(patch(f"{BASE}.read_text", return_value="0"))
                stack.enter_context(patch(f"{BASE}.counters", return_value=owner.memory_before))
                result = run_owner(owner)
                self.assertEqual(result["verdict"], "topology_cancel_pass")
                self.assertEqual(result["cancel_requests"], 2)
                self.assertFalse(result["cleanup_debt"])
                self.assertTrue(result["parent_payload_views_released"])
                self.assertIsNone(result["decoder_exit_status"])
                self.assertEqual(
                    result["decoder_reaper"],
                    "unobserved" if case == "ignoring_launcher" else "init_adoption_observed",
                )
                self.assertEqual(
                    result["launcher_exit_status"], -9 if case == "ignoring_launcher" else 0
                )


if __name__ == "__main__":
    unittest.main()
