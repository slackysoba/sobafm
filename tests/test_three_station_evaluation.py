"""Finite qualification harness tests: no Docker or native media execution."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web

from evaluations.three_station import probe
from evaluations.three_station import run as supervisor
from sobafm.decode import DecodeInput, DecodeProducer, RawInput
from sobafm.pcm import BANK_BYTES, PcmBank
from tests.test_decode import echo_frames


def scripted_decoder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> DecodeInput:
    path = tmp_path / "synthetic-input"
    path.write_bytes(b"x")
    source = DecodeInput(path, 1, hashlib.sha256(b"x").hexdigest(), 3 * BANK_BYTES)

    def create(
        source: DecodeInput, *, bank: PcmBank, open_input: Callable[[Path], RawInput] | None = None
    ) -> DecodeProducer:
        kwargs: dict[str, Any] = {} if open_input is None else {"open_input": open_input}
        return DecodeProducer(source, bank=bank, command=echo_frames(1800), **kwargs)

    monkeypatch.setattr(probe, "DecodeProducer", create)
    return source


def test_all_load_cases_use_real_ownership_and_bounded_loopback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    source = scripted_decoder(tmp_path, monkeypatch)

    async def exercise() -> None:
        peer = probe.Peer()
        app = web.Application()
        app.router.add_get("/{slot}", peer.handle)
        runner = web.AppRunner(app, access_log=None, shutdown_timeout=1)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = runner.addresses[0][1]
        assert isinstance(port, int)
        try:
            for case in ("catalog", "generated", "mixed"):
                await probe.load_case(case, source, port)
            assert peer.requests == 8 and peer.messages == 64 and peer.rejected == 0
            assert not probe.RETAINED
        finally:
            await runner.cleanup()

    asyncio.run(exercise())
    rows = supervisor.records(capsys.readouterr().out.encode())
    assert [r["event"] for r in rows] == ["load", "cleanup"] * 3
    assert all(r["frames"] == 600 and r["peak_slots"] == 6 for r in rows[::2])
    assert all(r["clean"] and r["banks_left"] == 0 for r in rows[1::2])


def test_held_close_expires_on_original_clock_and_keeps_failed_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    source = scripted_decoder(tmp_path, monkeypatch)
    asyncio.run(probe.debt_case(source))
    rows = supervisor.records(capsys.readouterr().out.encode())
    assert rows[0]["expired"] and rows[0]["retained_banks"] == 1
    assert rows[0]["fourth_refused"]
    assert rows[1]["expired_still"] and rows[1]["restart_admitted"]
    assert rows[1]["banks_left"] == rows[1]["reservations_left"] == 0
    assert not probe.RETAINED


def test_fixture_refusal_precedes_payload_read(tmp_path: Path) -> None:
    path = tmp_path / "fixture.mp3"
    path.write_bytes(b"wrong")
    with pytest.raises(probe.Failed, match="fixture_size"):
        probe.fixture(path)


@pytest.mark.parametrize("change", ["none", "revoked", "digest", "media", "duplicate", "missing"])
def test_existing_admission_must_bind_the_exact_fixture(tmp_path: Path, change: str) -> None:
    key = {"source": "fma", "recording_id": "9423"}
    reviews: list[dict[str, object]] = [
        {
            "key": key,
            "metadata_digest": supervisor.METADATA_DIGEST,
            "credits_complete": True,
            "access_ready": True,
            "synthetic": False,
        }
    ]
    media: list[dict[str, object]] = [
        {
            "key": key,
            "file": "fma/9423.mp3",
            "size": 1_793_243,
            "sha256": supervisor.FIXTURE_SHA256,
            "max_pcm_bytes": 21_699_840,
        }
    ]
    if change == "revoked":
        reviews[0]["access_ready"] = False
    elif change == "digest":
        reviews[0]["metadata_digest"] = "0" * 64
    elif change == "media":
        media[0]["sha256"] = "0" * 64
    elif change == "duplicate":
        reviews *= 2
    elif change == "missing":
        reviews.clear()
    (tmp_path / "reviews.json").write_text(json.dumps({"reviews": reviews}))
    (tmp_path / "media.json").write_text(json.dumps({"recordings": media}))
    assert supervisor.admitted(tmp_path / "fma" / "9423.mp3") == (change == "none")


@pytest.mark.parametrize(
    "raw",
    [
        b"x" * (supervisor.MAX_OUTPUT + 1),
        b"{}\n" * 17,
        b'{"event":"result","event":"failure"}',
        b'{"event":"/private/path"}',
        b'{"event":"https://example.invalid"}',
        b'{"number":NaN}',
        b'{"number":Infinity}',
        b'{"nested":{}}',
        b"[]",
        b"truncated",
    ],
)
def test_unsafe_or_incomplete_output_cannot_become_evidence(raw: bytes) -> None:
    with pytest.raises(supervisor.Refused):
        supervisor.records(raw)


def successful_records() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = [
        {"event": "target", "group": 268_435_456, "pcm": 13_824_000, "copies": 12_582_912}
    ]
    for case, native, sessions in (("catalog", 6, 0), ("generated", 0, 6), ("mixed", 4, 2)):
        rows.extend(
            [
                {
                    "event": "load",
                    "case": case,
                    "peak_slots": 6,
                    "native_slots": native,
                    "frames": 600,
                    "ready_ms": 100,
                    "fourth_refused": True,
                },
                {
                    "event": "cleanup",
                    "case": case,
                    "clean": True,
                    "banks_left": 0,
                    "reservations_left": 0,
                    "sessions": sessions,
                    "failed_cleanups": 0,
                    "native_settled": native,
                    "cleanup_ms": 20,
                    "crossfades": sessions // 2,
                    "stream_failure": None,
                },
            ]
        )
    rows.extend(
        [
            {"event": "debt", "expired": True, "retained_banks": 1, "fourth_refused": True},
            {
                "event": "debt_released",
                "expired_still": True,
                "banks_left": 0,
                "reservations_left": 0,
                "restart_admitted": True,
            },
            {"event": "resources", "memory_peak": 100_000_000, "tasks_peak_sampled": 30},
            {
                "event": "peer",
                "requests": 8,
                "messages": 64,
                "rejected": 0,
                "pcm_bytes": 24_576_000,
            },
            {"event": "result", "passed": True},
        ]
    )
    return rows


@pytest.mark.parametrize(
    "index,key,value",
    [
        (2, "clean", False),
        (2, "banks_left", 1),
        (2, "cleanup_ms", 3001),
        (3, "frames", 599),
        (4, "sessions", 5),
        (4, "failed_cleanups", 1),
        (7, "retained_banks", 0),
        (8, "expired_still", False),
        (9, "memory_peak", 268_435_457),
        (9, "tasks_peak_sampled", 65),
        (10, "requests", 9),
    ],
)
def test_pass_requires_complete_counts_and_preserved_failures(
    index: int, key: str, value: object
) -> None:
    rows = successful_records()
    assert supervisor.complete(rows)
    rows[index][key] = value
    assert not supervisor.complete(rows)
    assert not supervisor.complete(rows[-1:])


def test_container_arguments_bind_only_two_files_and_no_network() -> None:
    args = supervisor.create_args("unique", Path("/probe.py"), Path("/9423.mp3"))
    assert args[args.index("--network") + 1] == "none"
    assert args[args.index("--memory") + 1] == "268435456"
    assert args[args.index("--memory-swap") + 1] == "268435456"
    assert args.count("--mount") == 2 and all("readonly" in a for a in args if "type=bind" in a)
    assert args[-4:] == [supervisor.IMAGE, "-I", "-B", "/probe.py"]
    assert not any(a == "env" or "--env" in a or "--privileged" in a for a in args)


def test_uncertain_create_is_retained_even_when_current_lookup_is_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = supervisor.Run()
    run.create_attempted = True

    def empty(*args: str, seconds: float = 5) -> bytes:
        return b""

    monkeypatch.setattr(run, "call", empty)
    assert not run.cleanup()
    assert run.create_attempted and not run.create_returned
    first_deadline = run.cleanup_deadline
    assert not run.cleanup() and run.cleanup_deadline == first_deadline


def test_cleanup_never_removes_a_container_with_wrong_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = supervisor.Run()
    run.create_attempted = run.create_returned = True
    calls: list[tuple[str, ...]] = []

    def call(*args: str, seconds: float = 5) -> bytes:
        calls.append(args)
        if args[:2] == ("docker", "ps"):
            return b"a" * 64
        return b"another-owner false false"

    monkeypatch.setattr(run, "call", call)
    with pytest.raises(supervisor.Refused, match="cleanup_owner"):
        run.cleanup()
    assert not any("rm" in c or "stop" in c for c in calls)


def test_cleanup_keeps_failure_after_stopping_running_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = supervisor.Run()
    run.create_attempted = run.create_returned = True
    run.exit_status = 0
    lookups = 0

    def call(*args: str, seconds: float = 5) -> bytes:
        nonlocal lookups
        if args[:2] == ("docker", "ps"):
            lookups += 1
            return b"a" * 64 if lookups == 1 else b""
        if args[:2] == ("docker", "inspect"):
            return f"{run.name} true false".encode()
        if args[:2] == ("docker", "logs"):
            return b"\n".join(json.dumps(r).encode() for r in successful_records())
        return b""

    monkeypatch.setattr(run, "call", call)
    assert run.cleanup() and run.valid
    assert run.exit_status is None  # forced workload termination cannot look like a pass
