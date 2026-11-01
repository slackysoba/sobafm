"""Opt-in Q4 topology fixtures; native execution needs a separately reviewed release."""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import select
import signal
import stat
import time
from contextlib import suppress
from dataclasses import asdict, dataclass
from pathlib import Path

from .decoder_startup import group_members, verify_rlimits
from .owned_mp3_cancel import PREFIX_BYTES, CancelRun, run_owner
from .owned_mp3_decode import (
    ARGV,
    BANK_BYTES,
    BANNER,
    CHUNK_BYTES,
    FIXTURE_BYTES,
    FIXTURE_SHA256,
    OUTPUT_COPY_OFFSET,
    cbr_fixture,
    load_fixture_snapshot,
    native_processes,
    parse_maps,
    read_start_identity,
    validate_fixture_partitions,
    verify_core_limit,
    verify_native_files,
)
from .probe import (
    COPY_BYTES,
    PCM_BYTES,
    ProbeError,
    counters,
    emit,
    observe,
    proc_fields,
    read_text,
)
from .topology_protocol import (
    RECORD,
    STATUS_LIMIT,
    TERM_BIT,
    StatusReader,
    TopologyCase,
    TopologyPipes,
    launcher_main,
    topology_pipes,
)


@dataclass(frozen=True)
class ProcessIdentity:
    pid: int
    start: int
    parent: int
    group: int
    session: int
    state: str
    ignored: int
    blocked: int


def process_identity(pid: int) -> ProcessIdentity:
    root = Path(f"/proc/{pid}")
    fields = read_text(root / "stat").rsplit(")", 1)[1].split()
    status = proc_fields(read_text(root / "status"))
    if len(fields) < 20 or int(status["Pid"]) != pid or int(status["PPid"]) != int(fields[1]):
        raise ProbeError("topology process identity is incomplete or changed")
    if (
        int(status["Uid"].split()[1]) != 1000
        or int(status["Gid"].split()[1]) != 1000
        or status["NoNewPrivs"].strip() != "1"
        or status["Seccomp"].strip() != "2"
        or int(status["CapEff"], 16) != 0
        or read_text(root / "cgroup").strip() != "0::/"
    ):
        raise ProbeError("topology process containment differs")
    return ProcessIdentity(
        pid,
        int(fields[19]),
        int(fields[1]),
        int(fields[2]),
        int(fields[3]),
        fields[0],
        int(status["SigIgn"], 16),
        int(status["SigBlk"], 16),
    )


def executable_identity(pid: int) -> dict[str, object]:
    path = Path(f"/proc/{pid}/exe")
    target = os.readlink(path)
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
        raise ProbeError("interpreter executable is not immutable root-owned image content")
    with path.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    return {"path": target, "device": info.st_dev, "inode": info.st_ino, "sha256": digest}


def descriptor_inventory(
    pid: int, expected: dict[int, tuple[int, int]], null_stdio: bool = False
) -> dict[str, object]:
    root = Path(f"/proc/{pid}")
    allowed = set(expected)
    if null_stdio:
        allowed.update((0, 1, 2))
    actual = {int(path.name) for path in (root / "fd").iterdir()}
    if actual != allowed:
        raise ProbeError("topology descriptor inventory differs from exclusive transfer")
    evidence: dict[str, object] = {}
    null = os.stat("/dev/null") if null_stdio else None
    for fd in sorted(actual):
        info = (root / "fd" / str(fd)).stat()
        fields = proc_fields(read_text(root / "fdinfo" / str(fd)))
        access = int(fields["flags"].strip(), 8) & os.O_ACCMODE
        if fd in expected:
            peer, wanted_access = expected[fd]
            own = os.fstat(peer)
            if (
                not stat.S_ISFIFO(info.st_mode)
                or (info.st_dev, info.st_ino) != (own.st_dev, own.st_ino)
                or access != wanted_access
            ):
                raise ProbeError("topology pipe identity or direction changed")
            kind = "owned_pipe"
        else:
            if (
                null is None
                or not stat.S_ISCHR(info.st_mode)
                or info.st_rdev != null.st_rdev
                or access != os.O_RDWR
            ):
                raise ProbeError("launcher standard descriptor is not /dev/null")
            kind = "dev_null"
        evidence[str(fd)] = {
            "kind": kind,
            "device": info.st_dev,
            "inode": info.st_ino,
            "access": access,
        }
    return evidence


class TopologyRun(CancelRun):
    def __init__(self, case: TopologyCase, interpreter_sha256: str) -> None:
        if re.fullmatch(r"[0-9a-f]{64}", interpreter_sha256) is None:
            raise ProbeError("a reviewed interpreter ELF digest is required")
        super().__init__("feed")
        self.topology_case: TopologyCase = case
        self.expected_interpreter = interpreter_sha256
        self.interpreter: dict[str, object] = {}
        self.topology: TopologyPipes | None = None
        self.protocol = StatusReader()
        self.decoder_pid = self.decoder_start = 0
        self.pending_command: str | None = None
        self.status_eof = False
        self.quit_started = False
        self.orphan: dict[str, object] = {}
        self.survived_term: dict[str, object] = {}
        self.decoder_absent = False
        self.identity_debt = False
        self.launcher_reap_unknown = False
        self.close_after_status_error = False

    def pinned(self, pid: int, start: int) -> ProcessIdentity:
        identity = process_identity(pid)
        if identity.start != start:
            self.identity_debt = True
            raise ProbeError(
                "owned topology PID/start identity changed; ownership remains uncertain"
            )
        return identity

    def launcher_identity(self, live: bool = True) -> ProcessIdentity:
        identity = self.pinned(self.child.pid, self.start_ticks)
        if (
            identity.parent != os.getpid()
            or identity.group != self.child.pid
            or identity.session != self.child.pid
        ):
            self.identity_debt = True
            raise ProbeError("launcher ancestry/session identity differs")
        if live and identity.state not in {"R", "S", "D"}:
            raise ProbeError("launcher is not live")
        if identity.blocked & TERM_BIT or bool(identity.ignored & TERM_BIT) != (
            self.topology_case == "ignoring_launcher"
        ):
            raise ProbeError("launcher TERM disposition differs from the selected fixed case")
        return identity

    def decoder_identity(self, parent: int, stopped: bool = False) -> ProcessIdentity:
        identity = self.pinned(self.decoder_pid, self.decoder_start)
        if identity.group != self.child.pid or identity.session != self.child.pid:
            self.identity_debt = True
            raise ProbeError("decoder group/session identity changed")
        states = {"T"} if stopped else {"R", "S", "D"}
        if (
            identity.parent != parent
            or identity.group != self.child.pid
            or identity.session != self.child.pid
            or identity.state not in states
            or identity.ignored & TERM_BIT
            or identity.blocked & TERM_BIT
        ):
            raise ProbeError("decoder ancestry/session/state or signal policy differs")
        return identity

    def verify_fds(self, before_exec: bool = False) -> dict[str, object]:
        if self.topology is None:
            raise ProbeError("topology descriptors missing")
        t, p = self.topology, self.topology.media
        launcher = descriptor_inventory(
            self.child.pid,
            {
                t.command_read: (t.command_write, os.O_RDONLY),
                t.status_write: (t.status_read, os.O_WRONLY),
            },
            True,
        )
        decoder = {
            0: (p.input_write, os.O_RDONLY),
            1: (p.output_read, os.O_WRONLY),
            2: (p.error_read, os.O_WRONLY),
        }
        if before_exec:
            decoder[p.ready_write] = (p.ready_read, os.O_WRONLY)
            decoder[p.gate_read] = (p.gate_write, os.O_RDONLY)
        return {"launcher": launcher, "decoder": descriptor_inventory(self.decoder_pid, decoder)}

    def queue_command(self, command: str) -> None:
        if self.pending_command is not None or command in self.protocol.commands:
            raise ProbeError("original launcher command remains owned or was already sent")
        expected = (
            "S"
            if not self.protocol.commands
            else ("P" if self.topology_case == "ignoring_launcher" else "Q")
        )
        if command != expected or len(self.protocol.commands) >= 2:
            raise ProbeError("invalid fixed launcher command sequence")
        self.pending_command = command

    def control_step(self, timeout: float = 0.0) -> None:
        if self.topology is None:
            return
        t = self.topology
        readers = (
            [t.status_read]
            if t.status_read in self.fds and t.status_read not in self.uncertain_closes
            else []
        )
        writers = (
            [t.command_write]
            if self.pending_command is not None
            and t.command_write in self.fds
            and t.command_write not in self.uncertain_closes
            else []
        )
        ready, writable, _ = select.select(readers, writers, [], timeout)
        if writable and self.pending_command is not None:
            fd = t.command_write
            self.busy.add(fd)
            failed = False
            try:
                if os.write(fd, self.pending_command.encode("ascii")) != 1:
                    raise ProbeError("short original launcher command write")
                self.protocol.commands.add(self.pending_command)
                self.pending_command = None
            except BlockingIOError, InterruptedError:
                pass
            except Exception:
                failed = True
                self.pending_command = None
                raise
            finally:
                self.busy.remove(fd)
                if failed:
                    self.close(fd)
        if ready:
            fd = t.status_read
            self.busy.add(fd)
            try:
                data = os.read(fd, min(RECORD.size, STATUS_LIMIT - self.protocol.total + 1))
                if not data:
                    self.protocol.eof()
                    self.status_eof = True
                    if self.cancel.started is None and "Q" not in self.protocol.commands:
                        raise ProbeError("unexpected launcher control EOF")
                for record in self.protocol.accept(data):
                    if record.tag == "H":
                        if record.pid in {1, os.getpid(), self.child.pid}:
                            raise ProbeError("launcher supplied a conflicting decoder PID")
                        if self.decoder_pid and (record.pid, record.start) != (
                            self.decoder_pid,
                            self.decoder_start,
                        ):
                            self.identity_debt = True
                            raise ProbeError("HELLO conflicts with recovered decoder identity")
                        self.decoder_pid, self.decoder_start = record.pid, record.start
                    if record.tag == "X" and self.cancel.started is None:
                        raise ProbeError("native decoder exited before the topology trigger")
            except BlockingIOError, InterruptedError:
                pass
            except Exception:
                self.close_after_status_error = True
                raise
            finally:
                self.busy.remove(fd)
                if self.status_eof or self.close_after_status_error:
                    self.close(fd)

    def prepare(self) -> None:
        validate_fixture_partitions(cbr_fixture())
        if (
            os.uname().machine != "x86_64"
            or os.sysconf("SC_PAGE_SIZE") != 4096
            or len(list(Path("/proc/self/task").iterdir())) != 1
            or native_processes()
        ):
            raise ProbeError("fresh single-threaded native amd64 controller required")
        if read_text(Path("/proc/sys/kernel/core_pattern")).strip() != "core":
            raise ProbeError("core routing differs; no host settings may change")
        verify_native_files()
        self.interpreter = executable_identity(os.getpid())
        if self.interpreter["sha256"] != self.expected_interpreter:
            raise ProbeError("controller interpreter differs from the reviewed ELF binding")
        self.memory_before = counters(read_text(Path("/sys/fs/cgroup/memory.events")))
        for fd in (0, 1, 2):
            os.fstat(fd)
        self.preparation_remaining()
        self.topology = t = topology_pipes(self.fds)
        self.pipes = p = t.media
        pid = os.fork()
        if pid == 0:
            try:
                launcher_main(self.topology_case, t, self.fds)
            except BaseException:
                # Never run the controller's exception/cleanup path in its fork copy.
                os._exit(125)
            os._exit(125)
        self.child.pid = pid
        _, self.start_ticks = read_start_identity(pid)
        for fd in tuple(self.fds):
            if fd not in t.parent_ends():
                self.close(fd)
        for fd in (p.output_read, p.error_read, p.input_write, t.command_write, t.status_read):
            os.set_blocking(fd, False)
        while "H" not in self.protocol.seen:
            self.preparation_remaining()
            self.control_step(0.01)
            self.child.reap()
            if self.child.exit_code is not None:
                raise ProbeError("launcher exited during HELLO")
        self.wait_ready(p.ready_read)
        if os.read(p.ready_read, 1) != b"R":
            raise ProbeError("decoder limit setup failed")
        self.launcher_identity()
        self.decoder_identity(pid)
        verify_core_limit(read_text(Path(f"/proc/{pid}/limits")))
        if (
            executable_identity(pid) != self.interpreter
            or executable_identity(self.decoder_pid) != self.interpreter
        ):
            raise ProbeError("pre-exec interpreter inheritance differs")
        self.verify_fds(True)
        root = Path(f"/proc/{self.decoder_pid}")
        verify_rlimits(read_text(root / "limits"), True)
        verify_core_limit(read_text(root / "limits"))
        self.preparation_remaining()
        if os.write(p.gate_write, b"x") != 1:
            raise ProbeError("short decoder exec gate write")
        self.exec_released = True
        self.close(p.gate_write)
        while len(self.stderr) < len(BANNER):
            self.preparation_remaining()
            self.control_step()
            ready, _, _ = select.select([p.output_read, p.error_read], [], [], 0.01)
            if p.output_read in ready:
                raise ProbeError("stdout activity before topology input admission")
            if p.error_read in ready:
                part = os.read(p.error_read, len(BANNER) - len(self.stderr))
                self.stderr.extend(part)
                if not part or not BANNER.startswith(self.stderr):
                    raise ProbeError("native banner absent or different")
        self.queue_command("S")
        while "S" not in self.protocol.seen:
            self.preparation_remaining()
            self.control_step(0.01)
        decoder = self.decoder_identity(pid, True)
        launcher = self.launcher_identity()
        if (
            native_processes() != {pid, self.decoder_pid}
            or group_members(pid) != sorted([pid, self.decoder_pid])
            or os.readlink(root / "exe") != ARGV[0]
            or any(
                len(list(Path(f"/proc/{item}/task").iterdir())) != 1
                for item in (pid, self.decoder_pid)
            )
        ):
            raise ProbeError("native topology executable/thread/inventory differs")
        self.native_gate = {
            "decoder": asdict(decoder),
            "launcher": asdict(launcher),
            "descriptors": self.verify_fds(),
            "mappings": parse_maps(read_text(root / "maps")),
            "rlimits": verify_rlimits(read_text(root / "limits"), True),
            "interpreter": self.interpreter,
        }
        verify_core_limit(read_text(root / "limits"))
        if not select.select([p.ready_read], [], [], 0)[0] or os.read(p.ready_read, 2):
            raise ProbeError("decoder exec acknowledgement remains owned or failed")
        self.close(p.ready_read)
        self.buffers = [bytearray(BANK_BYTES), bytearray(BANK_BYTES), bytearray(COPY_BYTES)]
        for buffer in self.buffers:
            for position in range(0, len(buffer), 4096):
                buffer[position] = 1
        self.views = [memoryview(buffer) for buffer in self.buffers]
        self.bank, _, copy = self.views
        load_fixture_snapshot(copy)
        with copy[:FIXTURE_BYTES] as snapshot:
            self.input_view = snapshot.toreadonly()
        self.output_view = copy[OUTPUT_COPY_OFFSET : OUTPUT_COPY_OFFSET + CHUNK_BYTES]
        self.views.extend((self.input_view, self.output_view))
        self.preparation_remaining()
        self.launcher_identity()
        self.decoder_identity(pid, True)
        if native_processes() != {pid, self.decoder_pid}:
            raise ProbeError("topology changed before input release")
        emit(
            {
                "event": "topology_input_gate",
                "case": self.topology_case,
                "native_gate": self.native_gate,
                "fixture_sha256": FIXTURE_SHA256,
                "prefix_bound": PREFIX_BYTES,
                "pcm_capacity": PCM_BYTES,
                "copy_capacity": COPY_BYTES,
            }
        )
        os.kill(self.decoder_pid, signal.SIGCONT)
        self.input_released = True

    def can_feed(self) -> bool:
        return super().can_feed() and not self.quit_started

    def check_trigger(self) -> None:
        if self.cancel.started is not None or self.pipes is None:
            return
        self.preparation_remaining()
        if self.output_eof or self.error_eof:
            raise ProbeError("decoder streams ended before topology trigger")
        if (
            not self.input_released
            or not 0 < self.input_count <= PREFIX_BYTES < FIXTURE_BYTES
            or self.output_count < 4
        ):
            return
        if self.pipes.input_write not in self.fds:
            raise ProbeError("original topology input no longer open")
        if self.topology_case == "ignoring_launcher":
            self.launcher_identity()
            decoder = self.decoder_identity(self.child.pid)
            if native_processes() != {self.child.pid, self.decoder_pid} or group_members(
                self.child.pid
            ) != sorted([self.child.pid, self.decoder_pid]):
                raise ProbeError("unexpected topology member before cancellation")
        else:
            if not self.quit_started:
                self.launcher_identity()
                self.decoder_identity(self.child.pid)
                if native_processes() != {self.child.pid, self.decoder_pid} or group_members(
                    self.child.pid
                ) != sorted([self.child.pid, self.decoder_pid]):
                    raise ProbeError("unexpected topology member before commanded leader exit")
                self.queue_command("Q")
                self.quit_started = True
                return
            if self.child.exit_code is None or not self.status_eof:
                return
            if self.child.exit_code != 0:
                raise ProbeError("commanded launcher did not exit with actual wait status zero")
            decoder = self.decoder_identity(1)
            if native_processes() != {self.decoder_pid} or group_members(self.child.pid) != [
                self.decoder_pid
            ]:
                raise ProbeError("retained descendant inventory differs")
            p = self.pipes
            descriptor_inventory(
                self.decoder_pid,
                {
                    0: (p.input_write, os.O_RDONLY),
                    1: (p.output_read, os.O_WRONLY),
                    2: (p.error_read, os.O_WRONLY),
                },
            )
            if os.readlink(Path(f"/proc/{self.decoder_pid}/exe")) != ARGV[0]:
                raise ProbeError("retained descendant executable differs")
            self.orphan = {
                "identity": asdict(decoder),
                "at": time.monotonic(),
                "launcher_exit_status": 0,
            }
        self.trigger = {
            "case": self.topology_case,
            "decoder": asdict(decoder),
            "input_bytes": self.input_count,
            "output_bytes": self.output_count,
            "leader_exit_status": self.child.exit_code,
        }
        self.request("controlled_topology_trigger")
        emit({"event": "topology_cancellation_trigger", **self.trigger})

    def io_step(self, timeout: float = 0.01) -> None:
        try:
            self.control_step()
        except Exception as error:
            self.fail(error)
        super().io_step(timeout)

    def reap_child(self) -> None:
        if self.launcher_reap_unknown:
            return
        try:
            self.child.reap()
        except ChildProcessError as error:
            self.launcher_reap_unknown = self.identity_debt = True
            self.fail(error)

    def recover_decoder(self) -> None:
        if self.decoder_pid or self.child.pid <= 0:
            return
        if self.launcher_reap_unknown:
            self.identity_debt = True
            raise ProbeError("cannot recover a descendant from an uncertain launcher reap")
        members = [pid for pid in group_members(self.child.pid) if pid != self.child.pid]
        if len(members) > 1:
            self.identity_debt = True
            raise ProbeError("unknown additional process in original topology group")
        if members:
            item = process_identity(members[0])
            if (
                item.group != self.child.pid
                or item.session != self.child.pid
                or item.parent not in {1, self.child.pid}
            ):
                self.identity_debt = True
                raise ProbeError("cannot recover original descendant identity")
            self.decoder_pid, self.decoder_start = item.pid, item.start

    def signal_group(self, sig: signal.Signals) -> None:
        pid = self.child.pid
        self.recover_decoder()
        direct = False
        if self.child.exit_code is None and not self.launcher_reap_unknown:
            item = process_identity(pid)
            if self.start_ticks == 0 and not self.exec_released:
                self.start_ticks = item.start
            item = self.pinned(pid, self.start_ticks)
            direct = item.group != pid
            if direct and self.exec_released:
                self.identity_debt = True
                self.fail("admitted launcher process group changed")
            if not direct and item.session != pid:
                self.identity_debt = True
                raise ProbeError("owned launcher session changed before signal")
        elif self.decoder_pid:
            try:
                item = self.pinned(self.decoder_pid, self.decoder_start)
            except FileNotFoundError:
                if group_members(pid):
                    self.identity_debt = True
                    raise ProbeError("group remains after pinned descendant absence") from None
                return
            if item.group != pid or item.session != pid:
                self.identity_debt = True
                raise ProbeError("descendant group/session changed before signal")
        elif group_members(pid):
            self.identity_debt = True
            raise ProbeError("no pinned process anchors the remaining topology group")
        members = [pid] if direct else group_members(pid)
        if members:
            with suppress(ProcessLookupError):
                action = os.kill if direct else os.killpg
                action(pid, sig)
                if sig == signal.SIGTERM:
                    action(pid, signal.SIGCONT)
        self.events.append(
            {"event": "signal", "signal": sig.name, "members": members, "at": time.monotonic()}
        )

    def manage_cancel(self) -> None:
        if self.cancel.started is None:
            return
        self.cancel.observe(time.monotonic())
        try:
            if self.child.pid > 0:
                self.recover_decoder()
                self.reap_child()
                if self.term_sent is None:
                    self.signal_group(signal.SIGTERM)
                    self.term_sent = time.monotonic()
                    self.request("repeat_after_term_dispatch")
                    if self.trigger and self.topology_case == "ignoring_launcher":
                        self.queue_command("P")
                if self.kill_sent is None and time.monotonic() >= self.cancel.started + 1.0:
                    if self.trigger and self.topology_case == "ignoring_launcher":
                        try:
                            if "P" not in self.protocol.seen:
                                raise ProbeError("no post-TERM PONG before fixed KILL point")
                            self.survived_term = {
                                "identity": asdict(self.launcher_identity()),
                                "at": time.monotonic(),
                            }
                        except Exception as error:
                            self.fail(error)
                    self.signal_group(signal.SIGKILL)
                    self.kill_sent = time.monotonic()
        finally:
            keep: set[int] = set()
            if self.pipes is not None:
                keep.add(self.pipes.error_read)
                if self.output_view is not None:
                    keep.add(self.pipes.output_read)
            if self.topology is not None:
                keep.add(self.topology.status_read)
                if self.child.pid > 0 and self.child.exit_code is None:
                    keep.add(self.topology.command_write)
                elif self.topology.command_write not in self.busy:
                    self.pending_command = None
            for fd in tuple(self.fds):
                if fd not in keep:
                    self.close(fd)

    def release_if_done(self) -> bool:
        if self.released:
            return True
        if self.identity_debt:
            return False
        if self.decoder_pid:
            try:
                _, start = read_start_identity(self.decoder_pid)
            except FileNotFoundError:
                self.decoder_absent = True
            else:
                if start != self.decoder_start:
                    self.identity_debt = True
                    self.fail("decoder PID reused; original identity release remains uncertain")
                return False  # Includes zombies even when cgroup.procs omits them.
        if self.child.pid > 0 and group_members(self.child.pid):
            return False
        if self.pending_command is not None:
            return False
        return super().release_if_done()

    def release_snapshot(self) -> dict[str, object]:
        return super().release_snapshot() | {
            "decoder_pid": self.decoder_pid,
            "decoder_start": self.decoder_start,
            "decoder_absence_observed": self.decoder_absent,
            "identity_debt": self.identity_debt,
            "pending_control_command": self.pending_command,
        }

    def finish(self) -> dict[str, object]:
        if not self.decoder_absent or not self.status_eof:
            self.fail("topology release evidence incomplete")
        if self.topology_case == "ignoring_launcher":
            if self.child.exit_code != -signal.SIGKILL or not self.survived_term:
                self.fail("ignored-TERM launcher lacks survival and actual SIGKILL reaping")
        elif self.child.exit_code != 0 or not self.orphan:
            self.fail("commanded leader exit lacks a positively observed retained descendant")
        result = super().finish()
        passed = result["verdict"] == "direct_decoder_cancel_pass"
        result.pop("exit_code")
        result.update(
            {
                "case": self.topology_case,
                "verdict": "topology_cancel_pass" if passed else "incomplete",
                "launcher_exit_status": self.child.exit_code,
                "decoder_exit_status": self.protocol.decoder_status,
                "decoder_reaper": "launcher_waitpid"
                if self.protocol.decoder_status is not None
                else ("init_adoption_observed" if self.orphan else "unobserved"),
                "decoder_release_evidence": (
                    "pinned proc stat absent; original group and native inventory empty"
                ),
                "protocol_records": [asdict(record) for record in self.protocol.records],
                "protocol_bytes": self.protocol.total,
                "status_eof": self.status_eof,
                "survived_term": self.survived_term,
                "orphan_observation": self.orphan,
                "controller_interpreter": self.interpreter,
                "q4": "only this topology case; other cases remain unqualified",
            }
        )
        return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute-reviewed-q4-topology", action="store_true", required=True)
    parser.add_argument(
        "--case", choices=("ignoring_launcher", "descendant_after_leader_exit"), required=True
    )
    parser.add_argument("--controller-elf-sha256", required=True)
    args = parser.parse_args()
    case: TopologyCase = (
        "ignoring_launcher" if args.case == "ignoring_launcher" else "descendant_after_leader_exit"
    )
    owner = TopologyRun(case, args.controller_elf_sha256)
    emit({"event": "topology_preflight", "observation": observe()})
    result = run_owner(owner)
    emit(result)
    return 0 if result["verdict"] == "topology_cancel_pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
