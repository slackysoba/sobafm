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
    print('{}' if mode == "missing_verdict" else '{"result": "pass"}')
elif args[:2] == ["container", "ls"]:
    if mode == "unknown_cleanup": sys.exit(1)
    if state.exists(): print(json.loads(state.read_text())[0])
elif args[0] == "rm":
    if mode == "retained": sys.exit(1)
    state.unlink()
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


def test_wrong_owner_is_never_stopped_or_removed(tmp_path: Path) -> None:
    result, calls = execute(tmp_path, "wrong_owner")
    assert result.returncode == 1
    assert not any(row[0] in {"stop", "rm"} for row in calls)
    assert (tmp_path / "state").exists()


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
