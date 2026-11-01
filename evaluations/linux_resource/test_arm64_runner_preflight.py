"""Offline runner-preflight contracts; never invoke Docker or a native workload."""

from __future__ import annotations

import errno
import io
import json
import signal
import subprocess
import unittest
from pathlib import Path
from typing import BinaryIO, cast
from unittest.mock import Mock, patch

from . import arm64_runner_preflight as preflight

SOURCE = "1" * 40
VISIBLE: dict[str, bool] = dict.fromkeys(
    ("stat", "status", "maps", "syscall", "cgroup", "stdio"), True
)
ENGINE: dict[str, object] = {
    "os": "linux",
    "architecture": "aarch64",
    "kernel": "test-kernel",
    "version": "29.8.0",
    "cgroup_version": "2",
    "cgroup_driver": "systemd",
    "security": ["name=seccomp,profile=builtin"],
    "storage_driver": "overlay2",
    "root": "/var/lib/docker",
    "memory": 8_000_000_000,
    "cpus": 4,
    "memory_limit": True,
    "swap_limit": True,
    "pids_limit": True,
}


class Clock:
    now: float = 0.0

    def monotonic(self) -> float:
        return self.now


def outcome(raw: bytes, failure: str | None = None, stderr: int = 0) -> preflight.CommandResult:
    return preflight.CommandResult(raw, 0, failure, True, VISIBLE, len(raw), stderr)


class Observations(unittest.TestCase):
    def test_selected_engine_fields_and_unsupported_configuration(self) -> None:
        facts, issues, root = preflight.engine_facts(json.dumps(ENGINE).encode(), "test-kernel")
        self.assertFalse(issues)
        self.assertEqual(root, "/var/lib/docker")
        self.assertTrue(facts["builtin_seccomp"])
        for key, value in (
            ("architecture", "x86_64"),
            ("kernel", "other-kernel"),
            ("cgroup_version", "1"),
            ("security", ["name=seccomp,profile=custom"]),
            ("swap_limit", False),
            ("memory", 1),
            ("cpus", 1),
        ):
            with self.subTest(key=key):
                _, problems, _ = preflight.engine_facts(
                    json.dumps(ENGINE | {key: value}).encode(), "test-kernel"
                )
                self.assertTrue(problems)

    def test_untrusted_engine_data_never_expands_paths_or_schema(self) -> None:
        for change in (
            {"root": "/home/private"},
            {"extra": "private"},
            {"memory": True},
            {"cpus": float("nan")},
            {"security": "name=seccomp,profile=builtin"},
        ):
            with self.subTest(change=change), self.assertRaises(preflight.Unqualified):
                preflight.engine_facts(json.dumps(ENGINE | change).encode(), "test-kernel")
        with self.assertRaises(preflight.Unqualified):
            preflight.engine_facts(b'{"os":"linux","os":"linux"}', "test-kernel")

    def test_unreadable_proc_reports_only_visibility(self) -> None:
        with (
            patch.object(preflight, "read_bytes", side_effect=PermissionError("private detail")),
            patch.object(Path, "stat", side_effect=PermissionError("private detail")),
        ):
            self.assertEqual(preflight.process_visibility(321), dict.fromkeys(VISIBLE, False))

    def test_read_bound_does_not_consume_unbounded_observations(self) -> None:
        stream = Mock()
        stream.__enter__ = Mock(return_value=stream)
        stream.__exit__ = Mock(return_value=False)
        stream.read.return_value = b"x" * 11
        with (
            patch.object(Path, "open", return_value=stream),
            self.assertRaises(preflight.Unqualified),
        ):
            preflight.read_bytes(Path("/synthetic"), 10)
        stream.read.assert_called_once_with(11)

    def test_exact_revision_is_required_before_target_inspection(self) -> None:
        with (
            patch.object(preflight.sys, "platform", "linux"),
            patch.object(preflight, "command", return_value=outcome(b"2" * 40)) as command,
            patch.object(preflight, "host_facts") as host,
        ):
            result = preflight.inspect_runner(SOURCE, preflight.Cancellation())
        self.assertEqual(result["classification"], "unqualified")
        self.assertEqual(result["issues"], ["source_revision_mismatch"])
        self.assertEqual(command.call_count, 1)
        host.assert_not_called()

    def test_clean_source_success_is_only_observed_prerequisites(self) -> None:
        replies = [outcome(SOURCE.encode()), outcome(b""), outcome(json.dumps(ENGINE).encode())]
        with (
            patch.object(preflight.sys, "platform", "linux"),
            patch.object(preflight, "command", side_effect=replies),
            patch.object(preflight, "host_facts", return_value=({"kernel": "test-kernel"}, [])),
            patch.object(
                preflight, "storage", return_value={"available_bytes": preflight.FREE_FLOOR}
            ),
        ):
            result = preflight.inspect_runner(SOURCE, preflight.Cancellation())
        self.assertEqual(result["classification"], "observed_prerequisites_match")
        self.assertEqual(result["native_runtime_qualification"], "not_run")
        self.assertEqual(result["container_permissions_and_limits"], "not_tested")

    def test_failed_cli_prevents_another_command_and_does_not_leak_stderr(self) -> None:
        with (
            patch.object(preflight.sys, "platform", "linux"),
            patch.object(
                preflight,
                "command",
                return_value=outcome(b"private output", "command_deadline", 100),
            ) as command,
        ):
            result = preflight.inspect_runner(SOURCE, preflight.Cancellation())
        self.assertEqual(command.call_count, 1)
        self.assertNotIn("private output", json.dumps(result))
        self.assertEqual(result["classification"], "unqualified")

    def test_absent_cli_or_denied_storage_is_unqualified_without_host_repair(self) -> None:
        with (
            patch.object(preflight.sys, "platform", "linux"),
            patch.object(
                preflight, "command", side_effect=preflight.Unqualified("cli_unavailable")
            ),
        ):
            result = preflight.inspect_runner(SOURCE, preflight.Cancellation())
        self.assertEqual(result["issues"], ["cli_unavailable"])
        with (
            patch.object(preflight.os, "statvfs", side_effect=PermissionError),
            self.assertRaises(PermissionError),
        ):
            preflight.storage(Path("/var/lib/docker"))

    def test_cancelled_or_unknown_command_cannot_spawn(self) -> None:
        with patch.object(preflight.subprocess, "Popen") as spawn:
            with self.assertRaises(preflight.Unqualified):
                preflight.command("engine", 100, preflight.Cancellation(1.0))
            with self.assertRaises(KeyError):
                preflight.command("arbitrary", 100, preflight.Cancellation())
        spawn.assert_not_called()

    def test_missing_core_permission_preserves_basic_host_facts(self) -> None:
        header = bytearray(64)
        header[:6] = b"\x7fELF\x02\x01"
        header[18:20] = (183).to_bytes(2, "little")

        def read(path: Path, _limit: int = preflight.READ_BYTES) -> bytes:
            if path.name == "core_pattern":
                raise PermissionError()
            return b"memory cpu pids"

        with (
            patch.object(preflight.platform, "machine", return_value="aarch64"),
            patch.object(preflight.platform, "release", return_value="test-kernel"),
            patch.object(preflight.os, "sysconf", return_value=4096),
            patch.object(preflight, "read_bytes", side_effect=read),
            patch.object(preflight, "process_visibility", return_value=VISIBLE),
            patch.object(Path, "open", return_value=io.BytesIO(header)),
            patch.object(Path, "is_file", return_value=True),
        ):
            facts, issues = preflight.host_facts()
        self.assertEqual(facts["architecture"], "aarch64")
        self.assertEqual(facts["kernel"], "test-kernel")
        self.assertEqual(facts["page_bytes"], 4096)
        self.assertIn("core_routing_unavailable", issues)

    def test_cli_launch_is_literal_local_and_does_not_inherit_configuration(self) -> None:
        process = Mock()
        process.stdout.fileno.return_value = 10
        process.stderr.fileno.return_value = 11
        expected = outcome(b"{}")
        with (
            patch.object(preflight.subprocess, "Popen", return_value=process) as spawn,
            patch.object(preflight.os, "set_blocking"),
            patch.object(preflight, "process_visibility", return_value=VISIBLE),
            patch.object(preflight.CommandOwner, "finish", return_value=expected),
        ):
            self.assertIs(
                preflight.command("engine", float("inf"), preflight.Cancellation()), expected
            )
        call = spawn.call_args
        self.assertIsNotNone(call)
        assert call is not None
        argv = call.args[0]
        self.assertIn("--host=unix:///var/run/docker.sock", argv)
        self.assertIn("--config=/nonexistent/sobafm-arm64-preflight", argv)
        self.assertEqual(call.kwargs["env"], preflight.CLEAN_ENV)
        self.assertTrue(call.kwargs["start_new_session"])
        self.assertNotIn("shell", call.kwargs)


class Ownership(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = Clock()
        self.patcher = patch.object(preflight.time, "monotonic", self.clock.monotonic)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.process = Mock()
        self.process.pid = 321
        self.process.poll.return_value = 0
        self.process.returncode = 0
        self.stream = Mock()
        self.owner = preflight.CommandOwner(
            cast(subprocess.Popen[bytes], self.process),
            preflight.Cancellation(),
            100.0,
            0.0,
            {10: cast(BinaryIO, self.stream)},
            10,
            {10: bytearray()},
            {10: 0},
        )

    def test_actual_late_pipe_completion_clears_debt_but_not_deadline(self) -> None:
        def ready(
            _read: list[int], _write: list[int], _error: list[int], _timeout: float
        ) -> tuple[list[int], list[int], list[int]]:
            self.clock.now += 0.25
            return ([10] if self.clock.now >= 4 else [], [], [])

        with (
            patch.object(preflight.select, "select", side_effect=ready),
            patch.object(preflight.os, "read", return_value=b""),
            patch.object(preflight, "group_present", return_value=False),
        ):
            result = self.owner.finish(VISIBLE)
        self.assertEqual(self.owner.cleanup_started, 0)
        self.assertFalse(result.cleanup_deadline_met)
        self.assertFalse(result.evidence()["cleanup_debt"])
        self.process.wait.assert_called_once()
        self.stream.close.assert_called_once()

    def test_repeated_cancel_retains_first_deadline_and_signal_grace(self) -> None:
        self.process.poll.return_value = None
        self.owner.cancel.request(signal.SIGINT, None)
        with (
            patch.object(self.owner, "read_ready"),
            patch.object(preflight, "group_present", return_value=True),
            patch.object(preflight.os, "killpg") as kill,
        ):
            self.assertFalse(self.owner.step())
            self.clock.now = 0.5
            self.owner.cancel.request(signal.SIGTERM, None)
            self.assertFalse(self.owner.step())
            self.clock.now = 1.0
            self.assertFalse(self.owner.step())
        self.assertEqual(self.owner.cleanup_started, 0)
        self.assertEqual(self.owner.cancel.started, 0)
        self.assertEqual(
            [call.args for call in kill.call_args_list],
            [(321, signal.SIGTERM), (321, signal.SIGKILL)],
        )

    def test_command_timeout_then_late_cancel_does_not_restart_cleanup(self) -> None:
        self.owner.end = 5.0
        self.clock.now = 5.0
        self.process.poll.return_value = None
        with (
            patch.object(self.owner, "read_ready"),
            patch.object(preflight, "group_present", return_value=True),
            patch.object(preflight.os, "killpg") as kill,
        ):
            self.assertFalse(self.owner.step())
            self.clock.now = 5.5
            self.owner.cancel.request(signal.SIGTERM, None)
            self.assertFalse(self.owner.step())
            self.clock.now = 6.0
            self.assertFalse(self.owner.step())
        self.assertEqual(self.owner.cleanup_started, 5.0)
        self.assertEqual(self.owner.failure, "command_deadline")
        self.assertEqual(kill.call_count, 2)

    def test_leader_exit_does_not_release_remaining_group_or_signal_unanchored_pid(self) -> None:
        self.owner.streams.clear()
        with (
            patch.object(self.owner, "read_ready"),
            patch.object(preflight, "group_present", return_value=True),
            patch.object(preflight.os, "killpg") as kill,
        ):
            self.assertFalse(self.owner.step())
            self.clock.now = 5
            self.assertFalse(self.owner.step())
        self.assertEqual(self.owner.failure, "retained_cli_group")
        self.assertTrue(self.owner.deadline_missed)
        self.process.wait.assert_not_called()
        kill.assert_not_called()

    def test_unknown_wait_retains_ownership(self) -> None:
        self.process.poll.side_effect = ChildProcessError()
        self.owner.streams.clear()
        with (
            patch.object(self.owner, "read_ready"),
            patch.object(preflight, "group_present", return_value=False),
        ):
            self.assertFalse(self.owner.step())
        self.assertTrue(self.owner.wait_unknown)
        self.assertEqual(self.owner.failure, "wait_outcome_unknown")

    def test_output_overflow_is_bounded_and_failed(self) -> None:
        self.owner.buffers[10] = bytearray(b"a" * preflight.COMMAND_BYTES)
        with (
            patch.object(preflight.select, "select", return_value=([10], [], [])),
            patch.object(preflight.os, "read", return_value=b"extra"),
        ):
            self.owner.read_ready()
        self.assertEqual(len(self.owner.buffers[10]), preflight.COMMAND_BYTES)
        self.assertEqual(self.owner.counts[10], 5)
        self.assertEqual(self.owner.failure, "command_output_limit")

    def test_close_failure_remains_owned_and_is_not_blindly_retried(self) -> None:
        self.stream.close.side_effect = OSError("synthetic")
        with patch.object(preflight.os, "fstat", return_value=Mock()):
            self.owner.close(10)
            self.owner.close(10)
        self.assertIn(10, self.owner.streams)
        self.stream.close.assert_called_once()
        with (
            patch.object(preflight.os, "fstat", side_effect=OSError(errno.EBADF, "closed")),
            patch.object(preflight.select, "select", return_value=([], [], [])),
        ):
            self.owner.read_ready()
        self.assertFalse(self.owner.streams)
        self.assertEqual(self.owner.failure, "pipe_close_failed")

    def test_broken_backstop_output_and_unexpected_step_failure_cannot_abandon_owner(self) -> None:
        self.process.poll.return_value = None
        self.clock.now = 61
        with (
            patch.object(self.owner, "read_ready"),
            patch.object(preflight, "group_present", return_value=True),
            patch.object(preflight.os, "killpg"),
            patch("builtins.print", side_effect=BrokenPipeError),
        ):
            self.assertFalse(self.owner.step())
        self.assertTrue(self.owner.alerted)
        self.assertTrue(self.owner.streams)
        original_step = self.owner.step
        calls = 0

        def interrupted_step() -> bool:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("synthetic")
            self.process.poll.return_value = 0
            self.process.returncode = 0
            return original_step()

        with (
            patch.object(self.owner, "step", side_effect=interrupted_step),
            patch.object(preflight.time, "sleep"),
            patch.object(preflight.select, "select", return_value=([10], [], [])),
            patch.object(preflight.os, "read", return_value=b""),
            patch.object(preflight, "group_present", return_value=False),
        ):
            result = self.owner.finish(VISIBLE)
        self.assertIsNotNone(result.failure)
        self.assertFalse(self.owner.streams)
        self.stream.close.assert_called_once()
        self.process.wait.assert_called_once()


if __name__ == "__main__":
    unittest.main()
