"""Opt-in read-only arm64 runner observations; never native runtime qualification."""

from __future__ import annotations

import argparse
import errno
import json
import os
import platform
import re
import select
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from types import FrameType
from typing import BinaryIO, cast

READ_BYTES = 65_536
COMMAND_BYTES = 32_768
RESULT_BYTES = 32_768
OPERATION_SECONDS = 30.0
COMMAND_SECONDS = 5.0
CLEANUP_SECONDS = 3.0
RETENTION_SECONDS = 60.0
FREE_FLOOR = 25_000_000_000
CLEAN_ENV = {
    "PATH": "/usr/bin:/bin",
    "HOME": "/nonexistent",
    "LANG": "C",
    "LC_ALL": "C",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_TERMINAL_PROMPT": "0",
}
GIT = ("/usr/bin/git", "--no-optional-locks", "-c", "core.fsmonitor=false")
ENGINE_FORMAT = (
    '{"os":{{json .OSType}},"architecture":{{json .Architecture}},'
    '"kernel":{{json .KernelVersion}},"version":{{json .ServerVersion}},'
    '"cgroup_version":{{json .CgroupVersion}},"cgroup_driver":{{json .CgroupDriver}},'
    '"security":{{json .SecurityOptions}},"storage_driver":{{json .Driver}},'
    '"root":{{json .DockerRootDir}},"memory":{{json .MemTotal}},"cpus":{{json .NCPU}},'
    '"memory_limit":{{json .MemoryLimit}},"swap_limit":{{json .SwapLimit}},'
    '"pids_limit":{{json .PidsLimit}}}'
)
COMMANDS = {
    "revision": (*GIT, "rev-parse", "--verify", "HEAD"),
    "tracked_status": (*GIT, "status", "--porcelain", "--untracked-files=no"),
    "engine": (
        "/usr/bin/docker",
        "--host=unix:///var/run/docker.sock",
        "--config=/nonexistent/sobafm-arm64-preflight",
        "info",
        "--format",
        ENGINE_FORMAT,
    ),
}


class Unqualified(RuntimeError):
    """A bounded observation cannot establish the requested prerequisite."""


def read_bytes(path: Path, limit: int = READ_BYTES) -> bytes:
    with path.open("rb") as stream:
        value = stream.read(limit + 1)
    if len(value) > limit:
        raise Unqualified("observation_size")
    return value


def text_field(value: object) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9._+:/-]{1,128}", value):
        raise Unqualified("unexpected_text_field")
    return value


def process_visibility(pid: int) -> dict[str, bool]:
    """Read only this owned process; never return addresses, paths or raw proc text."""
    root = Path(f"/proc/{pid}")
    result: dict[str, bool] = {}
    for name in ("stat", "status", "maps", "syscall", "cgroup"):
        try:
            result[name] = bool(read_bytes(root / name))
        except OSError, Unqualified:
            result[name] = False
    try:
        result["stdio"] = all((root / "fd" / str(fd)).stat() for fd in (0, 1, 2))
    except OSError:
        result["stdio"] = False
    return result


def group_present(pid: int) -> bool:
    try:
        os.killpg(pid, 0)
    except ProcessLookupError:
        return False
    return True  # Includes permission uncertainty, which the owner handles as debt.


def emit(value: dict[str, object]) -> bool:
    """A dead output destination cannot unwind an owner retaining resources."""
    try:
        raw = json.dumps(value, sort_keys=True, allow_nan=False)
        if len(raw.encode()) > RESULT_BYTES:
            return False
        print(raw, flush=True)
        return True
    except OSError, ValueError:
        return False


@dataclass
class Cancellation:
    started: float | None = None

    def request(self, _signum: int, _frame: FrameType | None) -> None:
        if self.started is None:
            self.started = time.monotonic()


@dataclass
class CommandResult:
    stdout: bytes
    exit_code: int | None
    failure: str | None
    cleanup_deadline_met: bool
    visibility: dict[str, bool]
    stdout_bytes: int
    stderr_bytes: int

    def evidence(self) -> dict[str, object]:
        return {
            "exit_code": self.exit_code,
            "failure": self.failure,
            "cleanup_deadline_met": self.cleanup_deadline_met,
            "cleanup_debt": False,  # Constructed only after actual wait/group/pipe release.
            "owned_cli_proc_visibility": self.visibility,
            "stdout_bytes": self.stdout_bytes,
            "stderr_bytes": self.stderr_bytes,
        }


@dataclass
class CommandOwner:
    process: subprocess.Popen[bytes]
    cancel: Cancellation
    end: float
    started: float
    streams: dict[int, BinaryIO]
    stdout_fd: int
    buffers: dict[int, bytearray] = field(default_factory=dict[int, bytearray])
    counts: dict[int, int] = field(default_factory=dict[int, int])
    cleanup_started: float | None = None
    failure: str | None = None
    deadline_missed: bool = False
    term_sent: bool = False
    kill_sent: bool = False
    alerted: bool = False
    wait_unknown: bool = False
    uncertain_closes: set[int] = field(default_factory=set[int])

    def begin(self, reason: str | None, now: float) -> None:
        if reason is not None:
            self.failure = self.failure or reason
        if self.cleanup_started is None:
            self.cleanup_started = now

    def close(self, fd: int) -> None:
        if fd in self.uncertain_closes:
            return
        stream = self.streams[fd]
        try:
            stream.close()
        except OSError:
            self.begin("pipe_close_failed", time.monotonic())
            self.uncertain_closes.add(fd)
            try:
                os.fstat(fd)
            except OSError as error:
                if error.errno == errno.EBADF:
                    del self.streams[fd]
                    self.uncertain_closes.discard(fd)
            return
        del self.streams[fd]

    def poll(self) -> int | None:
        try:
            return self.process.poll()
        except OSError, ChildProcessError:
            self.wait_unknown = True
            self.begin("wait_outcome_unknown", time.monotonic())
            return None

    def signal(self, sig: signal.Signals) -> None:
        # A still-unreaped direct child anchors the new session/group. Never signal
        # an unanchored group after leader reaping or an uncertain wait outcome.
        if self.wait_unknown or self.poll() is not None:
            return
        try:
            os.killpg(self.process.pid, sig)
        except OSError:
            self.begin("group_signal_failed", time.monotonic())

    def read_ready(self) -> None:
        for fd in tuple(self.uncertain_closes):
            try:
                os.fstat(fd)
            except OSError as error:
                if error.errno == errno.EBADF:
                    del self.streams[fd]
                    self.uncertain_closes.remove(fd)
        try:
            ready, _, _ = select.select(
                [fd for fd in self.streams if fd not in self.uncertain_closes], [], [], 0.01
            )
        except OSError, ValueError:
            self.begin("pipe_select_failed", time.monotonic())
            for fd in tuple(self.streams):
                self.close(fd)
            return
        for fd in ready:
            try:
                part = os.read(fd, 4096)
            except BlockingIOError:
                continue
            except OSError:
                self.begin("pipe_read_failed", time.monotonic())
                self.close(fd)
                continue
            if not part:
                self.close(fd)
                continue
            self.counts[fd] += len(part)
            room = max(0, COMMAND_BYTES - sum(len(value) for value in self.buffers.values()))
            self.buffers[fd].extend(part[:room])
            if len(part) > room:
                self.begin("command_output_limit", time.monotonic())

    def step(self) -> bool:
        now = time.monotonic()
        if self.cancel.started is not None:
            self.begin("cancelled", self.cancel.started)
        if now >= self.end:
            self.begin("command_deadline", self.end)
        code = self.poll()
        if code is not None:
            self.begin(None if code == 0 else "command_exit", now)
        if self.cleanup_started is not None:
            self.deadline_missed |= now > self.cleanup_started + CLEANUP_SECONDS
            if self.failure and not self.term_sent:
                self.signal(signal.SIGTERM)
                self.term_sent = True
            if self.failure and not self.kill_sent and now >= self.cleanup_started + 1.0:
                self.signal(signal.SIGKILL)
                self.kill_sent = True
        self.read_ready()
        try:
            group_empty = not group_present(self.process.pid)
        except OSError:
            group_empty = False
            self.begin("group_visibility_unknown", time.monotonic())
        if code is not None and not self.wait_unknown and not self.streams and group_empty:
            try:
                self.process.wait()
            except OSError, ChildProcessError:
                self.wait_unknown = True
                self.begin("wait_outcome_unknown", time.monotonic())
                return False
            return True
        if code is not None and not group_empty:
            self.begin("retained_cli_group", now)
        if now >= self.started + RETENTION_SECONDS and not self.alerted:
            self.begin("retention_backstop", now)
            self.alerted = True
            if not emit({"event": "preflight_cleanup_debt", "cleanup_debt": True}):
                self.begin("diagnostic_write_failed", now)
        return False

    def finish(self, visibility: dict[str, bool]) -> CommandResult:
        while True:
            try:
                if self.step():
                    break
            except BaseException:
                self.begin("owner_step_failed", time.monotonic())
                # Unexpected failures cannot unwind a live process/pipe owner.
                # Only these original handles remain; no replacement is launched.
                time.sleep(0.01)
        self.deadline_missed |= (
            self.cleanup_started is not None
            and time.monotonic() > self.cleanup_started + CLEANUP_SECONDS
        )
        return CommandResult(
            bytes(self.buffers[self.stdout_fd]),
            self.process.returncode,
            self.failure,
            not self.deadline_missed,
            visibility,
            self.counts[self.stdout_fd],
            sum(value for fd, value in self.counts.items() if fd != self.stdout_fd),
        )


def command(name: str, deadline: float, cancel: Cancellation) -> CommandResult:
    argv = COMMANDS[name]  # Closed allowlist; no shell or caller-provided command.
    start = time.monotonic()
    if cancel.started is not None or start >= deadline:
        raise Unqualified("operation_cancelled_or_expired")
    try:
        process = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=CLEAN_ENV,
            start_new_session=True,
        )
    except OSError as error:
        raise Unqualified("cli_unavailable") from error
    assert process.stdout is not None and process.stderr is not None
    streams = {
        process.stdout.fileno(): cast(BinaryIO, process.stdout),
        process.stderr.fileno(): cast(BinaryIO, process.stderr),
    }
    owner = CommandOwner(
        process,
        cancel,
        min(deadline, start + COMMAND_SECONDS),
        start,
        streams,
        process.stdout.fileno(),
        {fd: bytearray() for fd in streams},
        {fd: 0 for fd in streams},
    )
    try:
        for fd in streams:
            os.set_blocking(fd, False)
        visibility = process_visibility(process.pid)
    except BaseException:
        owner.begin("cli_setup_failed", time.monotonic())
        visibility = {}
        # A failed nonblocking setup must not enter a potentially blocking read.
        for fd in tuple(owner.streams):
            owner.close(fd)
    return owner.finish(visibility)


def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise Unqualified("duplicate_engine_field")
        result[key] = value
    return result


def engine_facts(raw: bytes, kernel: str) -> tuple[dict[str, object], list[str], str]:
    value: object = json.loads(raw, object_pairs_hook=unique_object)
    if not isinstance(value, dict):
        raise Unqualified("engine_object_required")
    data = cast(dict[str, object], value)
    expected = {
        "os",
        "architecture",
        "kernel",
        "version",
        "cgroup_version",
        "cgroup_driver",
        "security",
        "storage_driver",
        "root",
        "memory",
        "cpus",
        "memory_limit",
        "swap_limit",
        "pids_limit",
    }
    if set(data) != expected:
        raise Unqualified("engine_fields_differ")
    facts: dict[str, object] = {
        key: text_field(data[key])
        for key in (
            "os",
            "architecture",
            "kernel",
            "version",
            "cgroup_version",
            "cgroup_driver",
            "storage_driver",
        )
    }
    for key in ("memory", "cpus"):
        number = data[key]
        if type(number) is not int or number <= 0:
            raise Unqualified("engine_integer_required")
        facts[key] = number
    for key in ("memory_limit", "swap_limit", "pids_limit"):
        if type(data[key]) is not bool:
            raise Unqualified("engine_boolean_required")
        facts[key] = data[key]
    security = data["security"]
    if not isinstance(security, list) or not all(
        isinstance(item, str) for item in cast(list[object], security)
    ):
        raise Unqualified("engine_security_list_required")
    facts["builtin_seccomp"] = "name=seccomp,profile=builtin" in security
    facts["rootless"] = "name=rootless" in security
    issues: list[str] = []
    for key, wanted in (
        ("os", "linux"),
        ("architecture", "aarch64"),
        ("kernel", kernel),
        ("cgroup_version", "2"),
        ("builtin_seccomp", True),
        ("memory_limit", True),
        ("swap_limit", True),
        ("pids_limit", True),
    ):
        if facts[key] != wanted:
            issues.append("engine_" + key)
    if cast(int, facts["memory"]) < 90_177_536 or cast(int, facts["cpus"]) < 2:
        issues.append("engine_resources")
    root = data["root"]
    if root not in ("/var/lib/docker", "/mnt/docker"):
        raise Unqualified("engine_storage_location_unreviewed")
    return facts, issues, cast(str, root)


def storage(path: Path) -> dict[str, int]:
    info = os.statvfs(path)
    if min(info.f_frsize, info.f_blocks, info.f_bavail) <= 0 or info.f_bavail > info.f_blocks:
        raise Unqualified("storage_observation_invalid")
    return {
        "total_bytes": info.f_blocks * info.f_frsize,
        "available_bytes": info.f_bavail * info.f_frsize,
        "required_free_bytes": FREE_FLOOR,
    }


def host_facts() -> tuple[dict[str, object], list[str]]:
    if sys.platform != "linux":
        raise Unqualified("linux_required")
    kernel = text_field(platform.release())
    machine = text_field(platform.machine())
    page_size = os.sysconf("SC_PAGE_SIZE")
    issues: list[str] = []
    try:
        core = read_bytes(Path("/proc/sys/kernel/core_pattern"), 4096).strip()
    except OSError, Unqualified:
        core = b""
        issues.append("core_routing_unavailable")
    try:
        with Path("/proc/self/exe").open("rb") as stream:
            header = stream.read(64)  # Header only, not a new executable inventory.
        native_elf = (
            len(header) == 64
            and header[:6] == b"\x7fELF\x02\x01"
            and int.from_bytes(header[18:20], "little") == 183
        )
    except OSError:
        native_elf = False
        issues.append("interpreter_elf_unavailable")
    own_proc = process_visibility(os.getpid())
    try:
        controllers = read_bytes(Path("/sys/fs/cgroup/cgroup.controllers"), 4096).split()
    except OSError, Unqualified:
        controllers = []
        issues.append("controllers_unavailable")
    unified = Path("/sys/fs/cgroup/cgroup.controllers").is_file()
    facts: dict[str, object] = {
        "architecture": machine,
        "byteorder": sys.byteorder,
        "page_bytes": page_size,
        "kernel": kernel,
        "python": platform.python_version(),
        "python_elf64_little_aarch64": native_elf,
        "core_routing": "core" if core == b"core" else "pipe" if core.startswith(b"|") else "other",
        "self_proc_visibility": own_proc,
        "cgroup_v2": unified,
        "memory_cpu_pids_controllers": all(
            key in controllers for key in (b"memory", b"cpu", b"pids")
        ),
    }
    for condition, code in (
        (sys.version_info[:2] == (3, 14), "python_314_required"),
        (machine == "aarch64" and native_elf, "native_arm64_identity"),
        (sys.byteorder == "little", "little_endian_required"),
        (page_size == 4096, "page_size"),
        (core == b"core", "core_routing"),
        (all(own_proc.values()), "self_proc_visibility"),
        (unified and facts["memory_cpu_pids_controllers"] is True, "host_cgroup_v2"),
    ):
        if not condition:
            issues.append(code)
    return facts, issues


def inspect_runner(expected_source: str, cancel: Cancellation) -> dict[str, object]:
    started = time.monotonic()
    deadline = started + OPERATION_SECONDS
    facts: dict[str, object] = {}
    issues: list[str] = []
    commands: dict[str, object] = {}
    result: dict[str, object] = {
        "schema": "sobafm-arm64-runner-preflight-1",
        "expected_source": expected_source,
        "classification": "unqualified",
        "facts": facts,
        "issues": issues,
        "commands": commands,
        "native_runtime_qualification": "not_run",
        "container_permissions_and_limits": "not_tested",
        "cleanup_debt": False,
    }
    try:
        if re.fullmatch(r"[0-9a-f]{40}", expected_source) is None:
            raise Unqualified("full_expected_source_required")
        if sys.platform != "linux":
            raise Unqualified("linux_required")
        if signal.getsignal(signal.SIGCHLD) != signal.SIG_DFL:
            raise Unqualified("direct_child_wait_disposition")
        for name in ("revision", "tracked_status"):
            outcome = command(name, deadline, cancel)
            commands[name] = outcome.evidence()
            if outcome.failure or not outcome.cleanup_deadline_met or outcome.stderr_bytes:
                raise Unqualified("source_command_failed")
            if name == "revision":
                if outcome.stdout.decode("ascii").strip() != expected_source:
                    raise Unqualified("source_revision_mismatch")
                result["source"] = expected_source
            elif outcome.stdout.strip():
                raise Unqualified("tracked_tree_dirty")
        result["source_binding"] = "verified_git_head_and_clean_tracked_files"
        host, host_issues = host_facts()
        facts["host"] = host
        issues.extend(host_issues)
        workspace = storage(Path.cwd())
        facts["workspace_storage"] = workspace
        if workspace["available_bytes"] < FREE_FLOOR:
            issues.append("workspace_free_space")
        outcome = command("engine", deadline, cancel)
        commands["engine"] = outcome.evidence()
        if outcome.failure or not outcome.cleanup_deadline_met or outcome.stderr_bytes:
            raise Unqualified("engine_command_failed")
        if not outcome.visibility or not all(outcome.visibility.values()):
            issues.append("owned_cli_proc_visibility")
        engine, engine_issues, root = engine_facts(outcome.stdout, cast(str, host["kernel"]))
        facts["engine"] = engine
        issues.extend(engine_issues)
        docker_storage = storage(Path(root))
        facts["engine_storage"] = docker_storage
        if docker_storage["available_bytes"] < FREE_FLOOR:
            issues.append("engine_free_space")
        if cancel.started is not None or time.monotonic() > deadline:
            issues.append("operation_cancelled_or_expired")
        if not issues:
            result["classification"] = "observed_prerequisites_match"
    except (OSError, ValueError, Unqualified) as error:
        # Never serialize paths, arbitrary CLI output or exception strings.
        issues.append(str(error) if isinstance(error, Unqualified) else type(error).__name__)
    result["elapsed_seconds"] = time.monotonic() - started
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inspect-arm64-runner", action="store_true", required=True)
    parser.add_argument("--expected-source", required=True)
    args = parser.parse_args()
    cancel = Cancellation()
    signal.signal(signal.SIGTERM, cancel.request)
    signal.signal(signal.SIGINT, cancel.request)
    result = inspect_runner(args.expected_source, cancel)
    written = emit(result)
    return 0 if written and result["classification"] == "observed_prerequisites_match" else 1


if __name__ == "__main__":
    raise SystemExit(main())
