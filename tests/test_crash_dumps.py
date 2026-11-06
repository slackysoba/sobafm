"""The main process's crash-dump controls: their order, read-back and refusal.

The real-call cases run ``crash_dump_child.py`` as its own program, because the
controls can't be undone. The fakes below cover only what the kernel won't do on
request, such as refusing or ignoring a control. No host setting changes, and no
case starts a host crash handler: a non-dumpable process never reaches one.
"""

from __future__ import annotations

import json
import os
import resource
import signal
import subprocess
import sys
from pathlib import Path
from typing import cast

import pytest

from sobafm import __main__ as app
from sobafm import crash_dumps
from sobafm.crash_dumps import PR_GET_DUMPABLE, PR_SET_DUMPABLE, CrashDumpError, disable_crash_dumps
from sobafm.decode import MPG123_LIMITS

CHILD = Path(__file__).with_name("crash_dump_child.py")
TOKEN = "placeholder-token-value"  # pragma: allowlist secret - test placeholder
PR_NAMES = {PR_GET_DUMPABLE: "get", PR_SET_DUMPABLE: "set"}


def spawn(*arguments: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    done = subprocess.run(
        [sys.executable, "-I", str(CHILD), *arguments],
        capture_output=True,
        text=True,
        timeout=60,
        env=env if env is not None else {},
    )
    assert done.returncode == 0, done.stdout + done.stderr
    return done


def run_child(*arguments: str, env: dict[str, str] | None = None) -> dict[str, object]:
    done = spawn(*arguments, env=env)
    return cast(dict[str, object], json.loads(done.stdout.splitlines()[-1]))


class FakeKernel:
    """Stands in for prctl, recording each call and what the filter held at that moment."""

    def __init__(
        self,
        path: Path,
        *,
        set_result: int = 0,
        dumpable_after: int = 0,
        filter_after: str | None = None,
    ) -> None:
        self.path = path
        self.set_result = set_result
        self.dumpable_after = dumpable_after
        self.filter_after = filter_after
        self.calls: list[tuple[str, int, str]] = []

    def __call__(self, operation: int, value: int = 0) -> int:
        self.calls.append((PR_NAMES[operation], value, self.path.read_text()))
        if operation == PR_SET_DUMPABLE:
            if self.filter_after is not None:
                self.path.write_text(self.filter_after)
            return self.set_result
        return self.dumpable_after


@pytest.fixture
def filter_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "coredump_filter"
    path.write_text("00000033\n")
    monkeypatch.setattr(crash_dumps, "COREDUMP_FILTER", path)
    return path


def use(monkeypatch: pytest.MonkeyPatch, kernel: FakeKernel) -> FakeKernel:
    monkeypatch.setattr(crash_dumps, "prctl", kernel)
    return kernel


# Order, read-back and refusal against a kernel that won't cooperate on request.


def test_the_filter_is_written_before_the_process_becomes_non_dumpable(
    filter_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kernel = use(monkeypatch, FakeKernel(filter_file))
    disable_crash_dumps()
    assert kernel.calls == [("set", 0, "0"), ("get", 0, "0")]


def test_a_filter_that_cannot_be_written_refuses_before_touching_the_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    missing = tmp_path / "absent" / "coredump_filter"
    monkeypatch.setattr(crash_dumps, "COREDUMP_FILTER", missing)
    called: list[int] = []

    def record(operation: int, value: int = 0) -> int:
        called.append(operation)
        return 0

    monkeypatch.setattr(crash_dumps, "prctl", record)
    with pytest.raises(CrashDumpError, match="could not be written") as refused:
        disable_crash_dumps()
    assert called == [] and str(tmp_path) not in str(refused.value)


def test_a_refused_prctl_refuses(filter_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    use(monkeypatch, FakeKernel(filter_file, set_result=-1))
    with pytest.raises(CrashDumpError, match="non-dumpable"):
        disable_crash_dumps()


def test_a_process_that_is_still_dumpable_is_refused(
    filter_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    use(monkeypatch, FakeKernel(filter_file, dumpable_after=1))
    with pytest.raises(CrashDumpError, match="still dumpable"):
        disable_crash_dumps()


@pytest.mark.parametrize("held", ["00000033\n", "not hexadecimal\n", ""])
def test_a_filter_that_reads_back_wrong_is_refused(
    filter_file: Path, monkeypatch: pytest.MonkeyPatch, held: str
) -> None:
    use(monkeypatch, FakeKernel(filter_file, filter_after=held))
    with pytest.raises(CrashDumpError, match="did not take effect"):
        disable_crash_dumps()


def test_importing_the_entry_point_changes_nothing_in_this_process() -> None:
    assert app.__name__ == "sobafm.__main__"
    assert crash_dumps.prctl(PR_GET_DUMPABLE) == 1


# Real calls, each in its own process.


def test_the_real_controls_take_effect_for_a_non_root_process() -> None:
    facts = run_child("controls")
    assert facts["uid"] != 0
    assert facts["dumpable"] == 0 and facts["filter"] == 0
    # The kernel's own sign of a non-dumpable process.
    assert facts["filter_owner"] == 0


def test_what_the_service_reads_still_works_after_the_controls() -> None:
    facts = run_child("controls")
    assert cast(int, facts["open_descriptors"]) > 0
    assert cast(int, facts["maps_bytes"]) > 0
    assert cast(int, facts["limits_bytes"]) > 0
    assert cast(int, facts["status_bytes"]) > 0
    assert facts["cgroup_unchanged"] is True
    # The owner-only file a same-user process could read credentials from.
    assert facts["environ_readable"] is False


def test_the_wrong_order_is_refused_by_the_real_kernel() -> None:
    facts = run_child("wrong_order")
    assert facts["uid"] != 0
    assert facts["refused"] == "coredump_filter could not be written"


def test_a_real_crash_after_the_controls_leaves_no_dump(tmp_path: Path) -> None:
    pipe_read, pipe_write = os.pipe()
    actions = [(os.POSIX_SPAWN_DUP2, pipe_write, 1)]
    pid = os.posix_spawn(
        sys.executable,
        [sys.executable, "-I", str(CHILD), "crash", str(tmp_path)],
        {"SOBAFM_DISCORD_TOKEN": TOKEN},
        file_actions=actions,
    )
    os.close(pipe_write)
    with os.fdopen(pipe_read) as output:
        facts = json.loads(output.readline())
    _, status = os.waitpid(pid, 0)
    if facts["core_limit"] == 0:
        pytest.skip("this environment doesn't allow a core limit above zero")
    assert os.WIFSIGNALED(status) and os.WTERMSIG(status) == signal.SIGSEGV
    assert not os.WCOREDUMP(status)
    assert list(tmp_path.iterdir()) == []


def test_the_reviewed_decoder_limits_are_unchanged() -> None:
    assert MPG123_LIMITS == (
        (resource.RLIMIT_CORE, 0),
        (resource.RLIMIT_STACK, 524_288),
        (resource.RLIMIT_DATA, 12_000_000),
        (resource.RLIMIT_AS, 12_524_288),
    )


def test_a_decoder_launches_normally_and_inherits_only_the_filter() -> None:
    facts = run_child("decoder")
    assert facts["uid"] != 0
    assert facts["failure"] is None and facts["cleanup"] == "clean" and facts["settled"] is True
    assert facts["returncode"] == 0 and facts["decoder_complete"] is True
    assert facts["released"] is True
    assert facts["parent_dumpable"] == 0
    assert facts["decoder_filter"] == 0
    # Exec resets the attribute, so only the filter reaches the decoder.
    assert facts["decoder_dumpable"] is True
    # The parent's prlimit still reaches it, soft and hard.
    limits = cast(dict[str, list[str]], facts["decoder_limits"])
    assert limits["Max core file size"] == ["0", "0"]
    assert limits["Max data size"] == ["12000000", "12000000"]
    assert limits["Max address space"] == ["12524288", "12524288"]
    # The stack limit can't be asserted exactly yet, with or without these controls. A prlimit
    # that lands while the child is still in execve is overwritten: the kernel's finalize_exec
    # restores the value it pinned at the start of exec. Tighten this when that race is fixed.
    parent = cast(dict[str, list[str]], facts["parent_limits"])
    assert limits["Max stack size"] in (["524288", "524288"], parent["Max stack size"])


def test_running_the_program_applies_the_controls_and_reaches_the_limit_gate() -> None:
    facts = run_child("entry", env={"SOBAFM_DISCORD_TOKEN": TOKEN})
    assert facts["status"] == app.EXIT_LIMITS
    assert facts["native_loaded"] is True
    assert facts["dumpable"] == 0 and facts["filter"] == 0


def test_running_the_program_refuses_before_native_code_or_configuration(
    tmp_path: Path,
) -> None:
    database = tmp_path / "settings.db"
    done = spawn(
        "entry",
        str(tmp_path / "absent" / "filter"),
        env={"SOBAFM_DISCORD_TOKEN": TOKEN, "SOBAFM_DATABASE": str(database)},
    )
    lines = done.stdout.splitlines()
    facts = cast(dict[str, object], json.loads(lines[-1]))
    assert facts["status"] == app.EXIT_CRASH_DUMPS == 5
    assert facts["native_loaded"] is False
    assert lines[:-1] == [
        "ERROR sobafm crash dump controls refused: coredump_filter could not be written"
    ]
    assert TOKEN not in done.stdout + done.stderr and str(tmp_path) not in done.stdout
    assert not database.exists()
