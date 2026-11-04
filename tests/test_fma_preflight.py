"""#63 preflight and packet freeze. Docker is a local fake script; no media is read."""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import cast

import pytest

from evaluations.fma_validation import preflight as module
from evaluations.fma_validation.cases import CaseBinding
from evaluations.fma_validation.controller import Journal
from evaluations.fma_validation.ingress import decode
from evaluations.fma_validation.limits import FILE_BYTES, CaseClock, encode_report
from evaluations.fma_validation.packet import Packet
from evaluations.fma_validation.preflight import (
    PTY_FACTS,
    Preflight,
    blank_report,
    container_report,
    freeze,
    input_facts,
    pty_facts,
)
from evaluations.linux_resource.probe import ProbeError
from tests.test_fma_validation_packet import NOW, ROOT, qualification_packet
from tests.test_fma_validation_packet import packet_path as packet_path

IMAGE = "sha256:" + "3" * 64
VOLUME = "sobafm-44-preflight-a"
IDENTITY = "9" * 64

FAKE_DOCKER = r"""
import json, os, sys, time
from pathlib import Path

state_path = Path(os.environ["SOBAFM_FAKE_DOCKER"])
state = json.loads(state_path.read_text())
args = sys.argv[1:]
state["calls"].append(args[:2])

def save():
    state_path.write_text(json.dumps(state))

def emit(value):
    save()
    sys.stdout.write(value if isinstance(value, str) else json.dumps(value))
    sys.stdout.flush()
    raise SystemExit(0)

if args[0] == "info":
    emit({"architecture": "x86_64", "os": "linux", "seccomp": True})
if args[:2] == ["image", "inspect"]:
    emit({"id": args[-1], "architecture": "amd64", "os": "linux"})
if args[:2] == ["volume", "create"]:
    labels = [args[i + 1] for i, a in enumerate(args) if a == "--label"]
    identity = next(v.split("=", 1)[1] for v in labels if v.startswith("org.sobafm.preflight="))
    state["volume_identity"] = state.get("existing_volume") or identity
    emit(args[-1] + "\n")
if args[:2] == ["volume", "inspect"]:
    emit({"Name": args[-1], "CreatedAt": "2026-11-04T00:00:00Z", "Mountpoint": "/var/lib/v",
          "Labels": {"org.sobafm.issue": "44", "org.sobafm.preflight": state["volume_identity"]}})
if args[0] == "create":
    name = args[args.index("--name") + 1]
    mode = name.rsplit("-", 1)[1]
    identifier = ("a" if mode == "setup" else "b") * 64
    labels = dict(args[i + 1].split("=", 1) for i, a in enumerate(args) if a == "--label")
    mounts = []
    for i, a in enumerate(args):
        if a == "--mount":
            fields = dict(p.split("=", 1) for p in args[i + 1].split(",") if "=" in p)
            row = {"Type": fields["type"], "Destination": fields["target"],
                   "RW": "readonly" not in args[i + 1]}
            row["Name" if fields["type"] == "volume" else "Source"] = fields["source"]
            mounts.append(row)
    entry = args.index("--entrypoint")
    state["containers"][identifier] = {
        "mode": mode, "name": "/" + name, "image": args[entry + 2],
        "labels": {k: labels[k] for k in
                   ("org.sobafm.project", "org.sobafm.issue", "org.sobafm.packet")},
        "user": next(a.split("=", 1)[1] for a in args if a.startswith("--user=")),
        "cap_add": ["CAP_CHOWN"] if "--cap-add=CHOWN" in args else None,
        "path": args[entry + 1], "args": args[entry + 3:], "mounts": mounts,
        "running": False, "exit": None,
    }
    emit(identifier + "\n")
if args[:2] == ["container", "inspect"]:
    c = state["containers"][args[-1]]
    record = {
        "id": args[-1], "name": c["name"], "image": c["image"], "labels": c["labels"],
        "state": {"Running": c["running"], "Pid": 7 if c["running"] else 0,
                  "Status": "running" if c["running"] else
                  ("exited" if c["exit"] is not None else "created"),
                  "ExitCode": c["exit"] or 0, "OOMKilled": False},
    }
    if "mounts" in args[3]:
        record.update(
            user=c["user"], workdir="/source", path=c["path"], args=c["args"],
            mounts=c["mounts"],
            host={"Init": True, "ReadonlyRootfs": True, "Privileged": False,
                  "NetworkMode": "none", "CgroupnsMode": "private", "Memory": 90177536,
                  "MemorySwap": 90177536, "PidsLimit": 64, "NanoCpus": 2000000000,
                  "CapDrop": ["ALL"], "CapAdd": c["cap_add"],
                  "SecurityOpt": ["no-new-privileges=true"],
                  "RestartPolicy": {"Name": "no"}, "LogConfig": {"Type": "none"}})
    emit(record)
if args[0] == "start":
    c = state["containers"][args[-1]]
    if state.get("hang") == c["mode"]:
        c["running"] = True
        save()
        time.sleep(30)
    c["exit"] = state.get("exit", {}).get(c["mode"], 0)
    emit(json.dumps(state["reports"][c["mode"]]) + "\n")
if args[0] == "stop":
    if state.get("stop_fails"):
        save()
        raise SystemExit(1)
    c = state["containers"][args[-1]]
    c["running"], c["exit"] = False, 137
    emit(args[-1] + "\n")
raise SystemExit(2)
"""


def good_report(mode: str) -> dict[str, object]:
    report = blank_report(mode)
    uid = 0 if mode == "setup" else 1000
    report.update(
        uid=uid,
        gid=uid,
        slot={"device": 51, "inode": 7, "mode": 0o700, "uid": 1000, "gid": 1000, "writable": True},
        input={
            "device": 60,
            "inode": 9,
            "bytes": FILE_BYTES,
            "mode": 0o444,
            "mtime_ns": 1_700_000_000_000_000_000,
            "ctime_ns": 1_700_000_000_000_000_001,
        },
    )
    if mode == "probe":
        pty: dict[str, object] = dict.fromkeys(PTY_FACTS, True)
        pty["backpressure_bytes"] = 20_480
        report.update(init_sha256="4" * 64, python_sha256="5" * 64, pty=pty)
    return report


@pytest.fixture
def docker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[str, Path]]:
    script = tmp_path / "fake-docker"
    script.write_text(f"#!{sys.executable}\n{FAKE_DOCKER}")
    script.chmod(0o700)
    state = tmp_path / "docker-state.json"
    state.write_text(
        json.dumps(
            {
                "calls": [],
                "containers": {},
                "reports": {mode: good_report(mode) for mode in ("setup", "probe")},
            }
        )
    )
    monkeypatch.setenv("SOBAFM_FAKE_DOCKER", str(state))
    yield str(script), state


def update(state: Path, change: Callable[[dict[str, object]], None]) -> None:
    data = cast(dict[str, object], json.loads(state.read_text()))
    change(data)
    state.write_text(json.dumps(data))


def preflight(tmp_path: Path, docker: str, seconds: float = 60) -> Preflight:
    source = str(tmp_path / "absent-input")
    journal = Journal(
        tmp_path / "receipt.json",
        Preflight.ledger(
            image=IMAGE,
            python_path="/usr/local/bin/python",
            volume=VOLUME,
            checkout_path=str(ROOT),
            source_path=source,
            identity=IDENTITY,
            root=ROOT,
        ),
    )
    end = time.time() + seconds
    return Preflight(
        journal,
        image=IMAGE,
        python_path="/usr/local/bin/python",
        volume=VOLUME,
        checkout_path=str(ROOT),
        source_path=source,
        identity=IDENTITY,
        clock=CaseClock(end, end),
        docker=docker,
    )


def run(owner: Preflight) -> dict[str, object]:
    try:
        return owner.execute()
    finally:
        owner.journal.close()


def test_successful_preflight_writes_bounded_receipt(
    tmp_path: Path, docker: tuple[str, Path]
) -> None:
    result = run(preflight(tmp_path, docker[0]))
    assert result["passed"] is True and result["phase"] == "settled"
    assert result["cleanup_debt"] is False and result["failure"] is None
    assert result["slot_device"] == 51 and result["slot_inode"] == 7
    assert result["init_sha256"] == "4" * 64
    receipt = (tmp_path / "receipt.json").read_bytes()
    assert len(receipt) <= 65_536 and json.loads(receipt) == result
    calls = json.loads(docker[1].read_text())["calls"]
    assert ["volume", "create"] in calls and all(call[0] != "rm" for call in calls)
    assert sum(call[0] == "create" for call in calls) == 2


def test_probe_container_uses_owner_limits_and_setup_only_adds_chown(
    tmp_path: Path, docker: tuple[str, Path]
) -> None:
    owner = preflight(tmp_path, docker[0])
    try:
        setup, probe = owner.creation_args("setup"), owner.creation_args("probe")
    finally:
        owner.journal.close()
    assert "--user=1000:1000" in probe and not any(a.startswith("--cap-add") for a in probe)
    assert "--user=0:0" in setup and "--cap-add=CHOWN" in setup
    for args in (setup, probe):
        assert "--network=none" in args and "--read-only" in args and "--rm" not in args
        assert "--memory=90177536" in args and "--cap-drop=ALL" in args
        binds = [a for a in args if a.startswith("type=bind,")]
        assert len(binds) == 2 and all(a.endswith(",readonly") for a in binds)


def report_change(
    mode: str, path: tuple[str, ...], value: object
) -> Callable[[dict[str, object]], None]:
    def change(data: dict[str, object]) -> None:
        target = cast(dict[str, object], cast(dict[str, object], data["reports"])[mode])
        for key in path[:-1]:
            target = cast(dict[str, object], target[key])
        target[path[-1]] = value

    return change


FAILED_FACTS: list[tuple[str, Callable[[dict[str, object]], None]]] = [
    *((f"pty_{key}", report_change("probe", ("pty", key), False)) for key in PTY_FACTS),
    ("slot_mode", report_change("probe", ("slot", "mode"), 0o755)),
    ("slot_owner", report_change("probe", ("slot", "uid"), 0)),
    ("slot_read_only", report_change("probe", ("slot", "writable"), False)),
    ("slot_unstable", report_change("probe", ("slot", "inode"), 8)),
    ("input_unstable", report_change("probe", ("input", "mtime_ns"), 1)),
    ("probe_identity", report_change("probe", ("uid",), 0)),
    ("init_hash", report_change("probe", ("init_sha256",), None)),
    ("container_failure", report_change("setup", ("failure",), "preflight_setup_os_error")),
    ("volume_not_new", lambda data: data.update(existing_volume="8" * 64)),
    ("probe_exit", lambda data: data.update(exit={"probe": 1})),
]


EXPECTED = {
    "slot_mode": "persistent_private_slot_binding",
    "slot_owner": "persistent_private_slot_binding",
    "slot_read_only": "persistent_private_slot_binding",
    "slot_unstable": "preflight_slot_identity_unstable",
    "input_unstable": "preflight_input_identity_unstable",
    "probe_identity": "preflight_container_identity",
    "init_hash": "preflight_executable_identity",
    "container_failure": "preflight_container_failed",
    "volume_not_new": "preflight_volume_not_new",
    "probe_exit": "preflight_container_exit",
}


@pytest.mark.parametrize(("name", "change"), FAILED_FACTS, ids=[item[0] for item in FAILED_FACTS])
def test_each_failed_fact_fails_without_retry(
    tmp_path: Path,
    docker: tuple[str, Path],
    name: str,
    change: Callable[[dict[str, object]], None],
) -> None:
    update(docker[1], change)
    result = run(preflight(tmp_path, docker[0]))
    assert result["passed"] is False and result["phase"] == "retained_for_reconciliation"
    assert result["cleanup_debt"] is True and result["failure"] is not None
    assert result["failure"] == ("preflight_" + name if name.startswith("pty_") else EXPECTED[name])
    calls = json.loads(docker[1].read_text())["calls"]
    assert sum(call == ["volume", "create"] for call in calls) == 1
    assert sum(call[0] == "start" for call in calls) <= 2


def test_interrupted_cleanup_retains_debt_and_original_clock(
    tmp_path: Path, docker: tuple[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(module, "CONTAINER_SECONDS", 0.5)
    update(docker[1], lambda data: data.update(hang="probe", stop_fails=True))
    owner = preflight(tmp_path, docker[0])
    result = run(owner)
    assert result["passed"] is False and result["cleanup_debt"] is True
    assert result["failure"] == "management_command_deadline"
    containers = cast(list[dict[str, object]], result["containers"])
    assert containers[-1]["mode"] == "probe" and containers[-1]["exited"] is False
    cleanup = cast(dict[str, object], result["cleanup"])
    started = cleanup["cleanup_start_utc"]
    assert isinstance(started, float) and owner.clock.cleanup_start_utc == started
    calls = json.loads(docker[1].read_text())["calls"]
    assert sum(call[0] == "stop" for call in calls) == 1
    assert not any(call[0] in ("rm", "kill") or call[:2] == ["volume", "rm"] for call in calls)


def test_container_report_is_valid_preflight_wire(tmp_path: Path) -> None:
    for mode in ("setup", "probe"):
        report = good_report(mode)
        assert decode(encode_report(report, newline=True), "preflight") == report
    blank = blank_report("probe")
    blank["failure"] = "x" * 81
    with pytest.raises(ProbeError):
        decode(encode_report(blank), "preflight")


def test_input_is_stat_only(tmp_path: Path) -> None:
    source = tmp_path / "input"
    source.write_bytes(b"synthetic, never media")
    opened: list[object] = []
    active = [True]

    def audit(event: str, args: tuple[object, ...]) -> None:
        if active[0] and event == "open" and args and str(args[0]) == str(source):
            opened.append(args)

    sys.addaudithook(audit)
    try:
        facts = input_facts(source)
    finally:
        active[0] = False
    assert not opened
    assert facts["bytes"] == 22 and facts["inode"] == source.stat().st_ino
    link = tmp_path / "link"
    link.symlink_to(source)
    with pytest.raises(ProbeError, match="preflight_input_not_regular"):
        input_facts(link)


def test_probe_mode_collects_slot_input_and_pty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    slot = tmp_path / "slot"
    slot.mkdir(0o700)
    source = tmp_path / "input"
    source.write_bytes(b"synthetic")

    def executable(_pid: int) -> str:
        return "4" * 64

    monkeypatch.setattr(module, "executable_sha256", executable)
    report = container_report("probe", slot, source)
    assert report["failure"] is None
    assert cast(dict[str, object], report["slot"])["inode"] == slot.stat().st_ino
    assert all(cast(dict[str, object], report["pty"])[key] is True for key in PTY_FACTS)
    missing = container_report("probe", tmp_path / "absent", source)
    assert missing["failure"] == "preflight_probe_os_error"
    existing = container_report("setup", slot, source)
    assert existing["failure"] == "preflight_setup_os_error"


def test_pty_facts_report_backpressure_and_held_slave_eof() -> None:
    before = len(os.listdir("/proc/self/fd"))
    facts = pty_facts(time.monotonic() + 5)
    assert all(facts[key] is True for key in PTY_FACTS)
    assert 0 < cast(int, facts["backpressure_bytes"]) < module.PTY_BACKPRESSURE_CAP
    assert len(os.listdir("/proc/self/fd")) == before


def receipt(tmp_path: Path, **changes: object) -> Path:
    data: dict[str, object] = Preflight.ledger(
        image=IMAGE,
        python_path="/usr/local/bin/python",
        volume=VOLUME,
        checkout_path=str(ROOT),
        source_path=str(tmp_path / "absent-input"),
        identity=IDENTITY,
        root=ROOT,
    )
    data.update(
        phase="settled",
        passed=True,
        cleanup_debt=False,
        volume_created="2026-11-04T00:00:00Z",
        volume_mountpoint_sha256="6" * 64,
        slot_device=51,
        slot_inode=7,
        input=good_report("probe")["input"],
        init_sha256="4" * 64,
        python_sha256="5" * 64,
    )
    data.update(changes)
    path = tmp_path / "preflight-receipt.json"
    path.write_bytes(encode_report(data))
    return path


def request_file(packet_path: Path) -> Path:
    path = packet_path.parent / "request.json"
    path.write_bytes(encode_report(json.loads(packet_path.read_bytes())["request"]))
    return path


def frozen(
    receipt_path: Path,
    packet_path: Path,
    target: Path,
    *,
    qualification: bool = False,
    end: str = "2026-11-02T00:04:30Z",
) -> Packet:
    return freeze(
        receipt_path,
        request_file(packet_path),
        target,
        ROOT,
        whole_end_utc=end,
        case_id="sobafm-44-synthetic",
        review="https://github.com/slackysoba/sobafm/issues/63#synthetic-test-only",
        source_commit="1" * 40,
        source_tree="2" * 40,
        qualification=qualification,
        now=NOW,
    )


def test_freeze_builds_packet_with_load_round_trip(tmp_path: Path, packet_path: Path) -> None:
    path = receipt(tmp_path)
    target = tmp_path / "frozen.json"
    packet = frozen(path, packet_path, target)
    assert (
        packet.text("synthetic_preflight_sha256") == hashlib.sha256(path.read_bytes()).hexdigest()
    )
    assert packet.raw["slot_inode"] == 7 and packet.text("volume") == VOLUME
    assert packet.text("packet_path") == str(target.resolve())
    assert packet.whole_end == NOW + 210
    assert os.stat(target).st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        frozen(path, packet_path, target)


def test_freeze_qualification_packet(tmp_path: Path, packet_path: Path) -> None:
    qualification_packet(packet_path, "tagged-full")
    facts = {**cast(dict[str, object], good_report("probe")["input"])}
    facts["bytes"] = CaseBinding("tagged-full").bytes
    path = receipt(tmp_path, input=facts)
    packet = frozen(path, packet_path, tmp_path / "q.json", qualification=True)
    assert packet.case.name == "tagged-full"


@pytest.mark.parametrize(
    ("changes", "end"),
    [
        ({"passed": False}, "2026-11-02T00:04:30Z"),
        ({"cleanup_debt": True}, "2026-11-02T00:04:30Z"),
        ({"checkout_path": "/elsewhere"}, "2026-11-02T00:04:30Z"),
        ({}, "2026-11-02T00:04:31Z"),
        ({"input": {"bytes": 1}}, "2026-11-02T00:04:30Z"),
    ],
)
def test_freeze_rejects_failed_receipt_or_window(
    tmp_path: Path, packet_path: Path, changes: dict[str, object], end: str
) -> None:
    with pytest.raises(ProbeError):
        frozen(receipt(tmp_path, **changes), packet_path, tmp_path / "frozen.json", end=end)
