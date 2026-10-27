"""Offline parser/contract checks; no fork, decoder, Docker or provider use."""

import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

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
