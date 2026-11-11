"""Host procedure tests use a Docker command double, never a Docker daemon."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import cast

import pytest

RUNNER = Path(__file__).with_name("run.sh")
IMAGE = "sha256:bd7d1aee495e612a8f7a168b767ce43eb996466d75e743e0f7997807fbb7fe23"

DOUBLE = """#!/usr/bin/python3
import json, os, pathlib, sys
root = pathlib.Path(os.environ["DOUBLE_ROOT"])
args = sys.argv[1:]
with (root / "calls").open("a") as stream:
    stream.write(json.dumps(args) + "\\n")
state = root / "state"
mode = os.environ["DOUBLE_MODE"]
if args[:2] == ["image", "inspect"]:
    print(os.environ["DOUBLE_IMAGE"] + " linux amd64")
elif args[0] == "create":
    if mode == "pending_create": sys.exit(1)
    name = args[args.index("--name") + 1]
    label = args[args.index("--label") + 1].split("=", 1)[1]
    state.write_text(json.dumps([name, label]))
    if mode == "lost_create": sys.exit(1)
    print("synthetic-container")
elif args[0] == "wait":
    print("1" if mode == "client_failure" else "0")
elif args[0] == "inspect":
    if "Labels" in args[2]:
        print("wrong-owner" if mode == "wrong_owner" else json.loads(state.read_text())[1])
    else:
        print("false true" if mode == "oom" else "false false")
elif args[0] == "logs":
    if mode in {"stderr_only", "log_failure", "partial_log_failure"}:
        print("Traceback: synthetic-private-provider-error", file=sys.stderr)
    if mode == "partial_log_failure": print('{"result": "pass"}')
    if mode in {"log_failure", "partial_log_failure"}: sys.exit(1)
    if mode == "stderr_only": sys.exit(0)
    if mode == "startup_failure":
        print('{"result":"failed","reason":"startup_import"}')
    elif mode == "oversized_logs": print("x" * 65537)
    else: print('{}' if mode == "missing_verdict" else '{"result": "pass"}')
elif args[:2] == ["container", "ls"]:
    if mode == "unknown_cleanup": sys.exit(1)
    if state.exists(): print(json.loads(state.read_text())[0])
elif args[0] == "rm":
    if mode == "retained": sys.exit(1)
    state.unlink()
    if mode == "removed_with_error": sys.exit(1)
elif args[0] == "stop" and mode == "stop_error":
    sys.exit(1)
elif args[0] not in {"start", "stop"}:
    sys.exit(2)
"""


def execute(tmp_path: Path, mode: str) -> tuple[subprocess.CompletedProcess[str], list[list[str]]]:
    docker = tmp_path / "docker"
    docker.write_text(DOUBLE)
    docker.chmod(0o700)
    key = tmp_path / "key.txt"
    key.write_text("synthetic-placeholder-never-read-by-host")
    result = subprocess.run(
        ["bash", str(RUNNER), "--send-to-lyria", "--confirm-free-tier", str(key)],
        env={
            "PATH": f"{tmp_path}:/usr/bin:/bin",
            "TMPDIR": str(tmp_path),
            "DOUBLE_ROOT": str(tmp_path),
            "DOUBLE_MODE": mode,
            "DOUBLE_IMAGE": IMAGE,
        },
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    calls = [
        cast(list[str], json.loads(line)) for line in (tmp_path / "calls").read_text().splitlines()
    ]
    assert "synthetic-placeholder" not in result.stdout + result.stderr
    return result, calls


def test_runner_uses_exact_image_and_no_credential_environment(tmp_path: Path) -> None:
    result, calls = execute(tmp_path, "success")
    assert result.returncode == 0, result.stderr
    assert '"wrapper":"pass"' in result.stdout
    create = next(row for row in calls if row[0] == "create")
    assert IMAGE in create
    assert "--env" not in create and "--env-file" not in create
    assert create[create.index("--entrypoint") + 1 :] == [
        "/app/.venv/bin/python",
        IMAGE,
        "-I",
        "/eval/probe.py",
        "--send-to-lyria",
        "--confirm-free-tier",
    ]
    mounts = [create[i + 1] for i, value in enumerate(create) if value == "--mount"]
    assert len(mounts) == 2
    assert all(mount.endswith(",readonly") for mount in mounts)
    assert create[-2:] == ["--send-to-lyria", "--confirm-free-tier"]
    assert len([row for row in calls if row[0] == "create"]) == 1
    assert not (tmp_path / "state").exists()


@pytest.mark.parametrize(
    "mode",
    [
        "lost_create",
        "pending_create",
        "client_failure",
        "oom",
        "missing_verdict",
        "retained",
        "unknown_cleanup",
        "removed_with_error",
        "stop_error",
    ],
)
def test_host_failures_cannot_pass(tmp_path: Path, mode: str) -> None:
    result, calls = execute(tmp_path, mode)
    assert result.returncode == 1
    assert '"wrapper":"failed"' in result.stdout
    assert len([row for row in calls if row[0] == "create"]) == 1
    if mode in {"retained", "unknown_cleanup"}:
        assert '"cleanup_verified":false' in result.stdout
        assert (tmp_path / "state").exists()
    elif mode == "pending_create":
        assert '"cleanup_verified":false' in result.stdout
    else:
        assert not (tmp_path / "state").exists()
    if mode in {"removed_with_error", "stop_error"}:
        assert '"cleanup_verified":true' in result.stdout
        assert '"cleanup_commands_ok":false' in result.stdout


def test_wrong_owner_is_never_stopped_or_removed(tmp_path: Path) -> None:
    result, calls = execute(tmp_path, "wrong_owner")
    assert result.returncode == 1
    assert not any(row[0] in {"stop", "rm"} for row in calls)
    assert (tmp_path / "state").exists()


@pytest.mark.parametrize(
    ("mode", "reason"),
    [
        ("stderr_only", "evaluator_output_missing"),
        ("missing_verdict", "evaluator_output_missing"),
        ("log_failure", "log_retrieval"),
        ("partial_log_failure", "log_retrieval"),
        ("oversized_logs", "log_size"),
        ("startup_failure", "client_result"),
    ],
)
def test_collection_failures_are_distinct_and_redacted(
    tmp_path: Path, mode: str, reason: str
) -> None:
    result, calls = execute(tmp_path, mode)
    assert result.returncode == 1
    wrapper = json.loads(result.stdout.splitlines()[-1])
    assert wrapper["reason"] == reason
    assert wrapper["cleanup_verified"] and wrapper["cleanup_commands_ok"]
    assert result.stderr == ""
    assert "synthetic-private-provider-error" not in result.stdout
    records = list(tmp_path.glob("sobafm-lyria.*/result.jsonl"))
    assert len(records) == 1
    assert "synthetic-private-provider-error" not in records[0].read_text()
    assert len([row for row in calls if row[0] == "logs"]) == 1
    assert len([row for row in calls if row[0] == "create"]) == 1
    if mode == "startup_failure":
        assert '"reason":"startup_import"' in result.stdout
    if mode == "partial_log_failure":
        assert '"result": "pass"' not in result.stdout


def test_default_host_path_cannot_call_docker(tmp_path: Path) -> None:
    result = subprocess.run(
        ["/bin/bash", str(RUNNER)],
        env={"PATH": str(tmp_path)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert '"mode":"dry_run"' in result.stdout
