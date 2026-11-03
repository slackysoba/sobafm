"""Shared compact wire oracles, fixed hash backing and exceptional lifetimes."""

from __future__ import annotations

import gc
import hashlib
import json
import subprocess
import sys
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from sobafm import json_records as records
from tests.test_fma_report_encoding import track_arenas


def oracle(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


@pytest.mark.parametrize(
    "value",
    [
        None,
        True,
        False,
        0,
        -1,
        1 << 256,
        -(1 << 256),
        -0.0,
        5e-324,
        1.7976931348623157e308,
        float("nan"),
        float("inf"),
        -float("inf"),
        "".join(map(chr, range(128))),
        "\u0080\u07ff\u0800\uffff\ud800\udfff\U00010000\U0010ffff",
        {"\U0001f600": (1, False), "\ue000": [None], "\ud800": '\\"/', "a": {}},
    ],
)
def test_compact_intake_scalar_and_container_compatibility(value: object) -> None:
    expected = oracle(value)
    assert records.json_size(value, len(expected)) == len(expected)
    assert records.encode_record(value, len(expected)) == expected
    assert records.canonical_digest(value) == hashlib.sha256(expected).hexdigest()


class Text(str):
    def __str__(self) -> str:
        pytest.fail("custom string conversion")

    def __repr__(self) -> str:
        pytest.fail("custom string representation")

    def __iter__(self) -> Iterator[str]:
        pytest.fail("custom string iteration")

    def __len__(self) -> int:
        pytest.fail("custom string length")


def test_accepted_string_subclass_uses_underlying_data_without_copy_hooks() -> None:
    value = {Text("z"): Text("\u00e9\ud800\U0001f600"), Text("a"): (Text("x"),)}
    expected = oracle(value)
    assert records.encode_record(value, len(expected)) == expected
    assert records.canonical_digest(value) == hashlib.sha256(expected).hexdigest()


@pytest.mark.parametrize("limit", [4096, 16384])
def test_size_only_boundary_and_overflow_precede_document_allocation(
    monkeypatch: pytest.MonkeyPatch, limit: int
) -> None:
    def forbidden(_size: int) -> bytearray:
        pytest.fail("size proof or rejected value allocated document backing")

    monkeypatch.setattr(records, "bytearray", forbidden, raising=False)
    assert records.json_size("x" * (limit - 2), limit) == limit
    with pytest.raises(records.RecordError, match="capacity_exceeded"):
        records.encode_record("x" * (limit - 1), limit)


def test_impossible_integer_rejected_before_maintained_decimal_conversion() -> None:
    huge = 1 << 20000
    # Preserve the interpreter's existing conversion failure at a permissive cap.
    with pytest.raises(ValueError, match="integer string conversion"):
        oracle(huge)
    with pytest.raises(records.RecordError, match="capacity_exceeded"):
        records.json_size(huge, 16)
    with pytest.raises(ValueError, match="integer string conversion"):
        records.json_size(huge, 16384)
    with pytest.raises(ValueError, match="integer string conversion"):
        records.canonical_digest(huge)


def test_size_and_fill_scalar_phase_and_separate_immutable_freeze(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    value = (1 << 1024) - 1
    expected = oracle(value)
    limit = len(expected)
    phases: list[tuple[bool, int]] = []
    actual_ascii = records.JsonBuffer.ascii
    actual_bytes = bytes
    in_scalar = False

    def ascii_text(self: records.JsonBuffer, text: str) -> None:
        nonlocal in_scalar
        in_scalar = True
        phases.append((self.backing is not None, len(text)))
        try:
            actual_ascii(self, text)
        finally:
            in_scalar = False

    def freeze(view: memoryview) -> bytes:
        assert not in_scalar and len(view) == limit
        return actual_bytes(view)

    monkeypatch.setattr(records.JsonBuffer, "ascii", ascii_text)
    monkeypatch.setattr(records, "bytes", freeze, raising=False)
    assert records.encode_record(value, limit) == expected
    assert phases == [(False, limit), (True, limit)]


@pytest.mark.parametrize("wire_size", [2, 4095, 4096, 4097, 8192, 20000])
def test_hash_chunks_borrow_one_backing_and_release_each_view(
    monkeypatch: pytest.MonkeyPatch, wire_size: int
) -> None:
    value = "x" * (wire_size - 2)
    expected = oracle(value)
    actual_sha = hashlib.sha256
    expected_hash = actual_sha(expected).hexdigest()
    arenas = track_arenas(monkeypatch)
    chunks: list[bytes] = []
    views: list[memoryview] = []

    class Digest:
        def __init__(self) -> None:
            self.real = actual_sha()

        def update(self, data: memoryview) -> None:
            assert len(arenas) == 1 and data.obj is arenas[0]()
            assert len(data) <= records.HASH_BYTES
            for previous in views:
                with pytest.raises(ValueError, match="released memoryview"):
                    _ = previous.obj
            chunks.append(bytes(data))
            views.append(data)
            self.real.update(data)

        def hexdigest(self) -> str:
            return self.real.hexdigest()

    monkeypatch.setattr(records.hashlib, "sha256", Digest)
    assert records.canonical_digest(value) == expected_hash
    assert b"".join(chunks) == expected
    assert [len(chunk) for chunk in chunks] == [4096] * (wire_size // 4096) + (
        [wire_size % 4096] if wire_size % 4096 else []
    )
    assert len(arenas) == 1 and arenas[0]() is None
    for view in views:
        with pytest.raises(ValueError, match="released memoryview"):
            _ = view.obj


def test_hash_order_is_canonical_without_full_record_backing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    value = {"z": "x" * 20000, "\U0001f600": 2, "\ue000": 1, "a": {"z": 0, "a": 1}}
    expected = hashlib.sha256(oracle(value)).hexdigest()

    def forbidden(*_args: object, **_kwargs: object) -> bytes:
        pytest.fail("hash constructed a complete record")

    monkeypatch.setattr(records, "encode_record", forbidden)
    monkeypatch.setattr(json, "dumps", forbidden)
    monkeypatch.setattr(json.JSONEncoder, "iterencode", forbidden)
    assert records.canonical_digest(value) == expected
    assert records.canonical_digest(dict(reversed(list(value.items())))) == expected


@pytest.mark.parametrize("fail", [False, True])
def test_held_hash_owns_arena_until_return_then_releases_even_with_traceback(
    monkeypatch: pytest.MonkeyPatch, fail: bool
) -> None:
    entered, release = threading.Event(), threading.Event()
    actual_sha = hashlib.sha256
    expected = actual_sha(oracle("x" * 5000)).hexdigest()
    arenas = track_arenas(monkeypatch)
    views: list[memoryview] = []
    results: list[str | BaseException] = []

    class Digest:
        def __init__(self) -> None:
            self.real = actual_sha()

        def update(self, data: memoryview) -> None:
            views.append(data)
            entered.set()
            assert release.wait(5)
            if fail:
                raise ValueError("synthetic hash failure")
            self.real.update(data)

        def hexdigest(self) -> str:
            return self.real.hexdigest()

    def run() -> None:
        try:
            results.append(records.canonical_digest("x" * 5000))
        except BaseException as error:
            results.append(error)

    monkeypatch.setattr(records.hashlib, "sha256", Digest)
    thread = threading.Thread(target=run)
    enabled = gc.isenabled()
    gc.disable()
    try:
        thread.start()
        assert entered.wait(5)
        assert arenas[0]() is not None and views[0].obj is arenas[0]()
        # Concurrent operations have independent backing, even while hash holds H.
        assert records.encode_record({"a": 1}, 7) == b'{"a":1}'
        assert len(arenas) == 2 and arenas[1]() is None and arenas[0]() is not None
    finally:
        release.set()
        thread.join(5)
        assert all(arena() is None for arena in arenas)
        if enabled:
            gc.enable()
    assert not thread.is_alive() and len(results) == 1
    assert isinstance(results[0], ValueError) if fail else results[0] == expected
    assert all(arena() is None for arena in arenas)
    for view in views:
        with pytest.raises(ValueError, match="released memoryview"):
            _ = view.obj


def test_production_import_has_no_evaluation_dependency(tmp_path: Path) -> None:
    code = (
        "import sys; from sobafm import json_records, fma_intake; "
        "assert json_records.encode_record({'x':1}, 7) == b'{\"x\":1}'; "
        "assert not any(n == 'evaluations' or n.startswith('evaluations.') for n in sys.modules)"
    )
    result = subprocess.run([sys.executable, "-I", "-c", code], cwd=tmp_path, capture_output=True)
    assert result.returncode == 0, result.stderr
