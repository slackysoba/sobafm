"""Deterministic ownership/pacing regressions; no decoder, fork, Docker or clock sleeps."""

import errno
import signal
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from .decoder_startup import Child, CleanupResult
from .owned_mp3_decode import BANNER, decoder_child
from .owned_mp3_overlap import (
    FRAME_BYTES,
    OVERLAP_BYTES,
    OVERLAP_NS,
    SECOND_NS,
    Bank,
    Decoder,
    PacedOverlap,
    Pipes,
    admit_pair,
    cleanup_pair,
    fork_pair,
    require_pair_alive,
    run_overlap,
)
from .probe import ProbeError

MODULE = "evaluations.linux_resource.owned_mp3_overlap"


def owners() -> list[Decoder]:
    return [Decoder(Pipes(*range(10, 20))), Decoder(Pipes(*range(20, 30)))]


def readable_now(
    reads: list[int], writes: list[int], errors: list[int], timeout: float
) -> tuple[list[int], list[int], list[int]]:
    return reads, [], []


def one_member(pid: int) -> list[int]:
    return [pid]


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def select(
        self, reads: list[int], writes: list[int], errors: list[int], timeout: float
    ) -> tuple[list[int], list[int], list[int]]:
        self.now += timeout
        return [], [], []


def staged_run(stack: ExitStack, clock: FakeClock) -> tuple[list[Decoder], list[memoryview]]:
    """Owned in-memory pipe outcomes; the real orchestration and pump remain available."""
    launched: list[Decoder] = []
    payload_views: list[memoryview] = []
    pipe_sets = iter([item.pipes for item in owners()])

    def pipes(fds: list[int]) -> Pipes:
        item = next(pipe_sets)
        fds.extend(
            [item.input_write, item.output_read, item.error_read, item.ready_read, item.gate_write]
        )
        return item

    def fork(actual: list[Decoder], fds: list[int]) -> None:
        for index, item in enumerate(actual):
            item.child.pid = 42 + index
            item.started = 0.0
            item.exec_released = True
        launched.extend(actual)

    def allocate(buffers: list[bytearray], views: list[memoryview]) -> None:
        for _ in range(3):
            buffers.append(bytearray(16))
            views.append(memoryview(buffers[-1]))
        payload_views.extend(views)

    def text(path: Path) -> str:
        return "core" if path.name == "core_pattern" else "0"

    def processes() -> set[int]:
        return {item.child.pid for item in launched}

    for name, value in (
        ("BANK_BYTES", 16),
        ("PCM_BYTES", 32),
        ("COPY_BYTES", 16),
        ("CHUNK_BYTES", 4),
        ("OUTPUT_COPY_OFFSET", 8),
        ("FIXTURE_BYTES", 4),
        ("READY_BYTES", 8),
        ("PCM_OUTPUT_BYTES", 24),
        ("PREPARE_SECONDS", 0.1),
    ):
        stack.enter_context(patch(f"{MODULE}.{name}", value))
    stack.enter_context(patch(f"{MODULE}.time.monotonic", side_effect=clock.monotonic))
    stack.enter_context(patch(f"{MODULE}.os.sysconf", return_value=4096))
    stack.enter_context(patch.object(Path, "iterdir", return_value=iter([Path("1")])))
    stack.enter_context(patch(f"{MODULE}.native_processes", side_effect=processes))
    stack.enter_context(patch(f"{MODULE}.read_text", side_effect=text))
    stack.enter_context(patch(f"{MODULE}.counters", return_value={"oom": 0, "oom_kill": 0}))
    stack.enter_context(patch(f"{MODULE}.verify_native_files"))
    stack.enter_context(patch(f"{MODULE}.make_pipes", side_effect=pipes))
    stack.enter_context(patch(f"{MODULE}.fork_pair", side_effect=fork))
    stack.enter_context(patch(f"{MODULE}.admit_pair"))
    stack.enter_context(patch(f"{MODULE}.require_pair_alive"))
    stack.enter_context(patch(f"{MODULE}.allocate_payloads", side_effect=allocate))
    stack.enter_context(patch(f"{MODULE}.load_fixture_snapshot"))
    stack.enter_context(patch(f"{MODULE}.os.close"))
    return launched, payload_views


class OverlapContracts(unittest.TestCase):
    def test_one_ready_one_stalled_uses_original_deadline_and_never_starts_overlap(self) -> None:
        clock = FakeClock()
        delivered = 0

        def io_ready(
            reads: list[int], writes: list[int], errors: list[int], timeout: float
        ) -> tuple[list[int], list[int], list[int]]:
            nonlocal delivered
            clock.select(reads, writes, errors, timeout)
            if 12 in reads and delivered < 2:
                delivered += 1
                return [12], writes, []
            return [], writes, []

        def readv(fd: int, destinations: list[memoryview]) -> int:
            self.assertEqual(fd, 12)
            destinations[0][:4] = b"abcd"
            return 4

        with ExitStack() as stack:
            launched, payloads = staged_run(stack, clock)
            stack.enter_context(patch.object(Child, "reap"))
            stack.enter_context(patch(f"{MODULE}.os.set_blocking"))
            stack.enter_context(patch(f"{MODULE}.os.kill"))
            stack.enter_context(patch(f"{MODULE}.select.select", side_effect=io_ready))
            stack.enter_context(patch(f"{MODULE}.os.readv", side_effect=readv))
            stack.enter_context(patch(f"{MODULE}.os.write", return_value=4))
            emitted = stack.enter_context(patch(f"{MODULE}.emit"))
            cleanup = stack.enter_context(
                patch(f"{MODULE}.cleanup_pair", return_value=CleanupResult(False, [42, 43]))
            )
            result = run_overlap()
        self.assertEqual(launched[0].ready_buffered, 8)
        self.assertIsNone(launched[1].ready_seconds)
        self.assertEqual(launched[1].output_count, 0)
        self.assertEqual(clock.now, 0.1)
        self.assertEqual(result["overlap"], {})
        self.assertEqual(result["verdict"], "incomplete")
        self.assertIn("readiness", str(result["failure"]))
        self.assertEqual([child.pid for child in cleanup.call_args.args[0]], [42, 43])
        self.assertEqual(cleanup.call_args.args[1], 3.1)
        self.assertTrue(result["cleanup_debt"])
        self.assertNotIn(
            "overlap_interval_start", [call.args[0]["event"] for call in emitted.call_args_list]
        )
        for view in payloads:
            with self.assertRaises(ValueError):
                _ = view.obj

    def test_interruption_during_dual_completion_keeps_both_cleanup_owners(self) -> None:
        clock = FakeClock()

        def interrupted(
            pair: list[Decoder],
            fds: list[int],
            banks: list[Bank],
            views: list[memoryview],
            deadline: float,
        ) -> dict[str, object]:
            for item in pair:
                item.input_released = True
                item.input_count, item.output_count = 4, 24
            pair[0].child.exit_code = 0
            clock.now = 0.05
            raise InterruptedError(errno.EINTR, "dual completion interrupted")

        with ExitStack() as stack:
            launched, payloads = staged_run(stack, clock)
            stack.enter_context(patch(f"{MODULE}.emit"))
            stack.enter_context(patch(f"{MODULE}.pump_pair", side_effect=interrupted))
            cleanup = stack.enter_context(
                patch(f"{MODULE}.cleanup_pair", return_value=CleanupResult(False, [43]))
            )
            result = run_overlap()
        self.assertEqual([item.child.exit_code for item in launched], [0, None])
        self.assertEqual([child.pid for child in cleanup.call_args.args[0]], [42, 43])
        self.assertEqual(cleanup.call_args.args[1], 3.05)
        self.assertEqual(result["retained_pids"], [43])
        self.assertTrue(result["cleanup_debt"])
        self.assertFalse(result["cleanup_deadline_met"])
        self.assertEqual(result["verdict"], "incomplete")
        self.assertIn("dual completion interrupted", str(result["failure"]))
        for view in payloads:
            with self.assertRaises(ValueError):
                _ = view.obj

    def test_failed_second_fork_retains_first_owner_and_every_descriptor(self) -> None:
        pair = owners()
        fds = list(range(10, 30))
        with (
            patch(f"{MODULE}.os.fork", side_effect=[42, OSError(errno.EAGAIN, "bounded failure")]),
            patch(f"{MODULE}.os.close") as close,
            self.assertRaises(OSError),
        ):
            fork_pair(pair, fds)
        self.assertEqual([owner.child.pid for owner in pair], [42, 0])
        self.assertEqual(fds, list(range(10, 30)))
        close.assert_not_called()

    def test_forks_precede_parent_descriptor_release_and_children_close_cross_ends(self) -> None:
        pair = owners()
        fds = list(range(10, 30))
        events: list[str] = []

        def fork() -> int:
            events.append("fork")
            return 40 + len(events)

        def close(fd: int) -> None:
            events.append(f"close {fd}")

        with (
            patch(f"{MODULE}.os.fork", side_effect=fork),
            patch(f"{MODULE}.os.close", side_effect=close),
        ):
            fork_pair(pair, fds)
        self.assertEqual(events[:2], ["fork", "fork"])
        self.assertEqual(fds, [11, 12, 14, 16, 19, 21, 22, 24, 26, 29])
        for item in pair:
            pipes = item.pipes
            with ExitStack() as stack:
                base = "evaluations.linux_resource.owned_mp3_decode"
                for name in ("setsid", "dup2", "write", "execve"):
                    stack.enter_context(patch(f"{base}.os.{name}"))
                stack.enter_context(patch(f"{base}.resource.setrlimit"))
                stack.enter_context(patch(f"{base}.os.read", return_value=b"x"))
                closed = stack.enter_context(patch(f"{base}.os.close"))
                decoder_child(
                    pipes.input_read,
                    pipes.output_write,
                    pipes.error_write,
                    pipes.ready_write,
                    pipes.gate_read,
                    list(range(10, 30)),
                )
                self.assertEqual(
                    {call.args[0] for call in closed.call_args_list},
                    set(range(10, 30)) - {pipes.ready_write},
                )

    def test_second_gate_failure_retains_both_owners_without_payload_allocation(self) -> None:
        pair = owners()
        for index, item in enumerate(pair):
            item.child.pid = 42 + index
        fds = [11, 12, 14, 16, 19, 21, 22, 24, 26, 29]
        with ExitStack() as stack:
            stack.enter_context(patch(f"{MODULE}.time.monotonic", return_value=0.0))
            stack.enter_context(patch(f"{MODULE}.select.select", side_effect=readable_now))
            stack.enter_context(patch(f"{MODULE}.os.read", return_value=b"R"))
            stack.enter_context(patch(f"{MODULE}.verify_child", return_value={}))
            stack.enter_context(patch(f"{MODULE}.verify_core_limit"))
            stack.enter_context(patch(f"{MODULE}.read_text", return_value=""))
            stack.enter_context(patch(f"{MODULE}.read_start_identity", return_value=("S", 17)))
            stack.enter_context(patch(f"{MODULE}.emit"))
            stack.enter_context(patch(f"{MODULE}.os.write", return_value=1))
            stack.enter_context(patch(f"{MODULE}.os.close"))
            stack.enter_context(patch(f"{MODULE}.receive_banner", return_value=bytearray(BANNER)))
            gates = stack.enter_context(
                patch(
                    f"{MODULE}.stopped_native_gate",
                    side_effect=[{"state": "stopped"}, ProbeError("second gate failed")],
                )
            )
            with self.assertRaisesRegex(ProbeError, "second gate"):
                admit_pair(pair, fds)
        self.assertEqual(gates.call_args_list[1].args[-1], frozenset({42, 43}))
        self.assertEqual(pair[0].native_gate, {"state": "stopped"})
        self.assertEqual(pair[1].native_gate, {})
        self.assertTrue(all(item.exec_released and not item.input_released for item in pair))
        self.assertEqual([item.child.pid for item in pair], [42, 43])

        # Exercise the real orchestration edge: a failed admission never reaches allocation/pump.
        with ExitStack() as stack:
            stack.enter_context(patch(f"{MODULE}.os.sysconf", return_value=4096))
            stack.enter_context(patch.object(Path, "iterdir", return_value=iter([Path("1")])))
            stack.enter_context(patch(f"{MODULE}.native_processes", return_value=set()))
            stack.enter_context(patch(f"{MODULE}.read_text", return_value="core"))
            stack.enter_context(patch(f"{MODULE}.counters", return_value={"oom": 0, "oom_kill": 0}))
            stack.enter_context(patch(f"{MODULE}.verify_native_files"))
            stack.enter_context(
                patch(f"{MODULE}.make_pipes", side_effect=[item.pipes for item in pair])
            )

            def launched(actual: list[Decoder], _: list[int]) -> None:
                for index, item in enumerate(actual):
                    item.child.pid = 42 + index

            stack.enter_context(patch(f"{MODULE}.fork_pair", side_effect=launched))
            stack.enter_context(
                patch(f"{MODULE}.admit_pair", side_effect=ProbeError("second gate"))
            )
            allocate = stack.enter_context(patch(f"{MODULE}.allocate_payloads"))
            pump = stack.enter_context(patch(f"{MODULE}.pump_pair"))
            clean = stack.enter_context(
                patch(f"{MODULE}.cleanup_pair", return_value=CleanupResult(False, [43]))
            )
            result = run_overlap()
        allocate.assert_not_called()
        pump.assert_not_called()
        self.assertEqual([child.pid for child in clean.call_args.args[0]], [42, 43])
        self.assertTrue(result["cleanup_debt"])
        self.assertEqual(result["retained_pids"], [43])
        self.assertEqual(result["verdict"], "incomplete")

    def test_bank_wrap_and_partial_frames_never_share_or_overfill_capacity(self) -> None:
        bank = Bank(capacity=16)
        other = Bank(capacity=16)
        storage = bytearray(16)
        with memoryview(storage) as target, memoryview(b"abcdefghijkl") as first:
            bank.put(target, first)
            bank.drain(8)
            with memoryview(b"MNOPQRSTUV") as second:
                bank.put(target, second)
            self.assertEqual(storage, b"QRSTUVghijklMNOP")
            self.assertEqual((bank.occupied, bank.free, bank.high_water), (14, 2, 14))
            with self.assertRaises(ProbeError):
                bank.put(target, first)
        self.assertEqual((other.occupied, other.drained), (0, 0))
        with self.assertRaises(ProbeError):
            bank.drain(14)
        bank.drain(12)
        self.assertEqual((bank.occupied, bank.drained), (2, 20))

    def test_pacing_requires_both_banks_and_real_full_interval(self) -> None:
        banks = [Bank(occupied=FRAME_BYTES), Bank()]
        pace = PacedOverlap(SECOND_NS)
        with self.assertRaisesRegex(ProbeError, "underflow"):
            pace.drain_due(banks, SECOND_NS + 20_000_000)
        self.assertEqual((banks[0].occupied, pace.drained_per_bank), (FRAME_BYTES, 0))
        banks[1].occupied = FRAME_BYTES
        self.assertFalse(pace.drain_due(banks, SECOND_NS + 19_999_999))
        self.assertEqual(pace.drained_per_bank, 0)
        for tick in range(1, 1501):
            for bank in banks:
                bank.occupied = FRAME_BYTES
            done = pace.drain_due(banks, SECOND_NS + tick * 20_000_000)
            self.assertEqual(done, tick == 1500)
        self.assertEqual(pace.drained_per_bank, OVERLAP_BYTES)
        self.assertEqual([bank.drained for bank in banks], [OVERLAP_BYTES, OVERLAP_BYTES])
        self.assertTrue(pace.drain_due(banks, SECOND_NS + OVERLAP_NS + 1))
        with self.assertRaises(ProbeError):
            pace.drain_due([banks[0], banks[0]], SECOND_NS + OVERLAP_NS)

    def test_second_identity_exit_cannot_be_counted_as_simultaneous_ownership(self) -> None:
        pair = owners()
        for index, item in enumerate(pair):
            item.child.pid, item.start_ticks = 42 + index, 100 + index
        pair[1].child.exit_code = 0
        with (
            patch.object(Child, "reap"),
            patch(f"{MODULE}.native_processes", return_value={42, 43}),
            patch(f"{MODULE}.read_start_identity", side_effect=[("S", 100), ("Z", 101)]),
            patch(f"{MODULE}.group_members", side_effect=one_member),
            self.assertRaisesRegex(ProbeError, "live native identity"),
        ):
            require_pair_alive(pair, stopped=False)

    def test_cleanup_keeps_second_slot_until_kill_and_uses_original_deadline(self) -> None:
        for deadline, expected_pass in ((3.0, True), (0.25, False)):
            with self.subTest(deadline=deadline):
                clock = FakeClock()
                children = [Child(42, 0), Child(43)]
                signals: list[tuple[int, signal.Signals]] = []

                def kill(
                    pid: int,
                    sig: signal.Signals,
                    current: list[Child] = children,
                    calls: list[tuple[int, signal.Signals]] = signals,
                ) -> None:
                    calls.append((pid, sig))
                    if pid == 43 and sig == signal.SIGKILL:
                        current[1].exit_code = -9

                def remaining(current: list[Child] = children) -> set[int]:
                    return {43} if current[1].exit_code is None else set()

                with (
                    patch.object(Child, "reap"),
                    patch(f"{MODULE}.time.monotonic", side_effect=clock.monotonic),
                    patch(f"{MODULE}.select.select", side_effect=clock.select),
                    patch(f"{MODULE}.native_processes", side_effect=remaining),
                    patch(f"{MODULE}.group_members", return_value=[]),
                    patch(f"{MODULE}.os.kill", side_effect=kill),
                    patch(f"{MODULE}.os.killpg", side_effect=kill),
                ):
                    result = cleanup_pair(children, deadline)
                self.assertEqual(result.deadline_met, expected_pass)
                self.assertEqual(result.retained_pids, [])
                self.assertIn((43, signal.SIGKILL), signals)
                self.assertLessEqual(clock.now, deadline + 0.000001)
