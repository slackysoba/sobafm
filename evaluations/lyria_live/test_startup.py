"""Exercise the real startup source with synthetic imports, gates and credentials."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from evaluations.lyria_live import probe as evaluator

STARTUP = """
import builtins, runpy, sys, types
from pathlib import Path
mode, source = sys.argv[1:]
sys.argv = [source, '--send-to-lyria', '--confirm-free-tier']
original = builtins.__import__
controlled = False
def crash_controls():
    global controlled
    if mode == 'crash_controls': raise OSError('synthetic-private-error')
    controlled = True
def request(*args, **kwargs):
    raise ValueError('synthetic-private-error')
def forbidden_key(*args, **kwargs):
    raise AssertionError('key read before startup finished')
Path.open = forbidden_key
def imports(name, *args, **kwargs):
    if name == 'sobafm.crash_dumps':
        if mode == 'first_import': raise ModuleNotFoundError('synthetic-private-error')
        return types.SimpleNamespace(disable_crash_dumps=crash_controls)
    if name == 'sobafm' or name.startswith('sobafm.'):
        assert controlled, 'native import before crash controls'
        if mode == 'native_import': raise ImportError('synthetic-private-error')
        if name == 'sobafm': return types.SimpleNamespace(lyria=types.SimpleNamespace())
        if name == 'sobafm.lyria':
            return types.SimpleNamespace(CLEANUP_SECONDS=3, ENDPOINT='synthetic',
                MAX_MESSAGE_PCM_BYTES=384000, LyriaSession=object,
                MusicRequest=request, Prompt=lambda text: None)
        if name == 'sobafm.pcm':
            return types.SimpleNamespace(BANK_BYTES=2304000, FRAME_BYTES=3840,
                PcmBankPool=object)
        raise AssertionError('unexpected application import')
    return original(name, *args, **kwargs)
builtins.__import__ = imports
runpy.run_path(source, run_name='__main__')
"""


@pytest.mark.parametrize(
    ("mode", "reason"),
    [
        ("first_import", "startup_import"),
        ("crash_controls", "crash_controls"),
        ("native_import", "startup_import"),
        ("initialization", "startup_internal"),
    ],
)
def test_startup_failures_have_fixed_output_before_main(mode: str, reason: str) -> None:
    result = subprocess.run(
        [sys.executable, "-I", "-c", STARTUP, mode, str(Path(evaluator.__file__))],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 1
    assert json.loads(result.stdout) == {"result": "failed", "reason": reason}
    assert result.stderr == ""


@pytest.fixture
def preflight(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    values = {
        "memory.max": "90177536",
        "memory.swap.max": "0",
        "pids.max": "64",
        "cpu.max": "200000 100000",
    }
    monkeypatch.setattr(evaluator.platform, "system", lambda: "Linux")
    monkeypatch.setattr(evaluator.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(evaluator.os, "getuid", lambda: 1000)

    def alarm(_seconds: int) -> int:
        return 0

    def signal(*_args: object) -> None:
        pass

    def read_text(path: Path) -> str:
        return values[path.name]

    monkeypatch.setattr(evaluator.signal, "alarm", alarm)
    monkeypatch.setattr(evaluator.signal, "signal", signal)
    monkeypatch.setattr(Path, "read_text", read_text)

    async def forbidden_probe(*_args: object, **_kwargs: object) -> evaluator.Result:
        pytest.fail("preflight failure started a provider session")

    monkeypatch.setattr(evaluator, "probe", forbidden_probe)
    return values


def assert_failure(capsys: pytest.CaptureFixture[str], reason: str) -> None:
    assert evaluator.main(["--send-to-lyria", "--confirm-free-tier"]) == 1
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {"result": "failed", "reason": reason}
    assert captured.err == ""


def forbidden_key(*_args: object, **_kwargs: object) -> None:
    pytest.fail("key opened")


@pytest.mark.parametrize("gate", ["system", "machine", "getuid"])
def test_target_refusal_precedes_key_read(
    preflight: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    gate: str,
) -> None:
    if gate == "getuid":
        monkeypatch.setattr(evaluator.os, gate, lambda: 0)
    else:
        monkeypatch.setattr(evaluator.platform, gate, lambda: "unsupported")
    monkeypatch.setattr(Path, "open", forbidden_key)
    assert_failure(capsys, "target")


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("memory.max", "max"),
        ("memory.swap.max", "1"),
        ("pids.max", "65"),
        ("cpu.max", "max 100000"),
        ("cpu.max", "200000 0"),
        ("cpu.max", "100000 100000"),
        ("cpu.max", "malformed"),
    ],
)
def test_limits_refusal_precedes_key_read(
    preflight: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    name: str,
    value: str,
) -> None:
    preflight[name] = value
    monkeypatch.setattr(Path, "open", forbidden_key)
    assert_failure(capsys, "limits")


def test_unreadable_limits_are_redacted(
    preflight: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def unreadable(_path: Path) -> str:
        raise OSError("synthetic-private-error")

    monkeypatch.setattr(Path, "read_text", unreadable)
    monkeypatch.setattr(Path, "open", forbidden_key)
    assert_failure(capsys, "limits")


@pytest.mark.parametrize("content", [None, b"", b"bad key", b"x" * 513, b"\xff"])
def test_credential_refusals_are_redacted(
    preflight: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    content: bytes | None,
) -> None:
    key = tmp_path / "synthetic-key"
    if content is not None:
        key.write_bytes(content)
    monkeypatch.setattr(evaluator, "KEY_FILE", key)
    assert_failure(capsys, "credential_file")


def test_internal_failure_never_exposes_exception_text(
    preflight: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    key = tmp_path / "synthetic-key"
    key.write_text("placeholder")
    monkeypatch.setattr(evaluator, "KEY_FILE", key)

    async def broken(_key: str) -> evaluator.Result:
        raise RuntimeError("synthetic-private-error")

    monkeypatch.setattr(evaluator, "probe", broken)
    assert_failure(capsys, "internal")
