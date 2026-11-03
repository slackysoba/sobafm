"""Canonical byte parity and actual construction-arena lifetime, offline only."""

from __future__ import annotations

import gc
import hashlib
import inspect
import json
import weakref

import pytest

from evaluations.fma_validation import limits
from evaluations.fma_validation.limits import REPORT_BYTES, digest, encode_report, json_size
from evaluations.linux_resource.probe import ProbeError


@pytest.mark.parametrize(
    "value",
    [
        None,
        True,
        False,
        0,
        -1,
        (1 << 128) - 1,
        -((1 << 128) - 1),
        0.0,
        -0.0,
        5e-324,
        -5e-324,
        1.7976931348623157e308,
        1e-7,
        1e20,
        "".join(chr(number) for number in range(128)),
        "\u0080\u07ff\u0800\uffff\ud800\udfff\U00010000\U0010ffff",
        {"\U0001f600": 1, "\ue000": 2, "\ud800": 3, "a": [(), {"z": None}]},
        ['\\"/', {}, [], (1, 2.5, True, None)],
    ],
)
@pytest.mark.parametrize("newline", [False, True])
def test_canonical_bytes_match_stdlib(value: object, newline: bool) -> None:
    oracle = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    ).encode("ascii")
    assert json_size(value) == len(oracle)
    expected = oracle + (b"\n" if newline else b"")
    assert encode_report(value, len(expected), newline=newline) == expected


def test_no_lf_digest_vector_and_no_materializing_json(monkeypatch: pytest.MonkeyPatch) -> None:
    expected = b'{"a":"\\n","b":-0.0}'

    def forbidden(*_args: object, **_kwargs: object) -> str:
        pytest.fail("FMA encoding reached a whole-document stdlib encoder")

    monkeypatch.setattr(json, "dumps", forbidden)
    monkeypatch.setattr(json.JSONEncoder, "iterencode", forbidden)
    value = {"b": -0.0, "a": "\n"}
    assert encode_report(value) == expected
    assert digest(value) == hashlib.sha256(expected).hexdigest()


@pytest.mark.parametrize("newline", [False, True])
def test_exact_record_and_one_byte_overflow_before_allocation(
    monkeypatch: pytest.MonkeyPatch, newline: bool
) -> None:
    value = "x" * (REPORT_BYTES - 2 - int(newline))
    assert len(encode_report(value, newline=newline)) == REPORT_BYTES

    def forbidden(_size: int) -> bytearray:
        pytest.fail("overflow reached work backing allocation")

    monkeypatch.setattr(limits, "bytearray", forbidden, raising=False)
    with pytest.raises(ProbeError, match="report_capacity_exceeded"):
        encode_report(value + "x", newline=newline)


@pytest.mark.parametrize("base", [str, int, float, list, tuple, dict])
def test_subclasses_are_rejected_without_custom_conversion(base: type[object]) -> None:
    def forbidden(_self: object) -> str:
        pytest.fail("subclass conversion was invoked")

    subclass = type("SyntheticSubclass", (base,), {"__str__": forbidden, "__repr__": forbidden})
    with pytest.raises(ProbeError, match="unsupported_report_value"):
        encode_report(subclass())
    if base is str:
        with pytest.raises(ProbeError, match="report_string_key_required"):
            encode_report({subclass(): 1})


def test_exact_depth_and_node_boundaries() -> None:
    nested: object = None
    for _ in range(12):
        nested = [nested]
    assert encode_report(nested) == b"[" * 12 + b"null" + b"]" * 12
    with pytest.raises(ProbeError, match="report_structure_limit"):
        encode_report([nested])
    assert json_size([None] * 8191) == 5 * 8191 + 1
    with pytest.raises(ProbeError, match="report_structure_limit"):
        encode_report([None] * 8192)
    cycle: dict[str, object] = {}
    cycle["self"] = cycle
    with pytest.raises(ProbeError, match="report_structure_limit"):
        encode_report(cycle)


@pytest.mark.parametrize(
    "value", [1 << 128, -(1 << 128), float("inf"), -float("inf"), float("nan")]
)
def test_scalar_limits_precede_backing_allocation(
    monkeypatch: pytest.MonkeyPatch, value: object
) -> None:
    def forbidden(_size: int) -> bytearray:
        pytest.fail("invalid scalar reached work backing")

    monkeypatch.setattr(limits, "bytearray", forbidden, raising=False)
    with pytest.raises(ProbeError):
        encode_report(value)


@pytest.mark.parametrize(
    "change", ["grow", "shrink", "type", "integer", "float", "key", "depth", "nodes"]
)
def test_mutation_between_proof_and_fill_rejects_before_freeze(
    monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    value: dict[object, object] = {"v": "x"}
    actual_size = json_size

    def changed(item: object, limit: int = REPORT_BYTES) -> int:
        size = actual_size(item, limit)
        if change == "key":
            value.clear()
            value[1] = "x"
        elif change == "depth":
            child: object = None
            for _ in range(13):
                child = [child]
            value["v"] = child
        else:
            value["v"] = {
                "grow": "x" * REPORT_BYTES,
                "shrink": "",
                "type": object(),
                "integer": 1 << 128,
                "float": float("nan"),
                "nodes": [None] * 8192,
            }[change]
        return size

    def forbidden(_view: memoryview) -> bytes:
        pytest.fail("changed report reached immutable freeze/output")

    monkeypatch.setattr(limits, "json_size", changed)
    monkeypatch.setattr(limits, "bytes", forbidden, raising=False)
    with pytest.raises(ProbeError):
        encode_report(value)


class TrackedArena(bytearray):
    pass


def track_arenas(monkeypatch: pytest.MonkeyPatch) -> list[weakref.ReferenceType[TrackedArena]]:
    arenas: list[weakref.ReferenceType[TrackedArena]] = []

    def allocate(size: int) -> bytearray:
        result = TrackedArena(size)
        arenas.append(weakref.ref(result))
        return result

    monkeypatch.setattr(limits, "bytearray", allocate, raising=False)
    return arenas


@pytest.mark.parametrize("failure", [False, True])
def test_repeated_arenas_release_without_gc_even_with_retained_tracebacks(
    monkeypatch: pytest.MonkeyPatch, failure: bool
) -> None:
    arenas = track_arenas(monkeypatch)
    original = limits.JsonBuffer.walk
    retained: list[Exception] = []

    def walk(self: limits.JsonBuffer, item: object, depth: int) -> None:
        original(self, item, depth)
        if failure and depth == 0 and self.backing is not None:
            try:
                raise RuntimeError("synthetic cause")
            except RuntimeError as error:
                raise ProbeError("synthetic_fill_failure") from error

    monkeypatch.setattr(limits.JsonBuffer, "walk", walk)
    enabled = gc.isenabled()
    gc.disable()
    try:
        for _ in range(30):
            try:
                result = encode_report({"v": "x" * 1024})
                assert not failure and len(result) == 1032
            except ProbeError as error:
                assert failure
                retained.append(error)
            assert all(arena() is None for arena in arenas)
        assert len(arenas) == 30 and len(retained) == (30 if failure else 0)
    finally:
        if enabled:
            gc.enable()


def test_old_record_work_and_immutable_overlap_is_three_capacities(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old = encode_report("x" * (REPORT_BYTES - 2))
    arenas = track_arenas(monkeypatch)
    actual_bytes = bytes
    overlaps: list[int] = []

    def freeze(view: memoryview) -> bytes:
        assert view.obj is arenas[0]() and len(view) == REPORT_BYTES
        frame = inspect.currentframe()
        while frame is not None:
            assert frame.f_code is not limits.JsonBuffer.walk.__code__
            frame = frame.f_back
        result = actual_bytes(view)
        overlaps.append(len(old) + len(view) + len(result))
        return result

    monkeypatch.setattr(limits, "bytes", freeze, raising=False)
    result = encode_report("y" * (REPORT_BYTES - 2))
    assert overlaps == [3 * REPORT_BYTES] and result is not old
    assert arenas[0]() is None


def test_freeze_failure_releases_arena_and_borrowed_view(monkeypatch: pytest.MonkeyPatch) -> None:
    arenas = track_arenas(monkeypatch)
    views: list[memoryview] = []

    def freeze(view: memoryview) -> bytes:
        views.append(view)
        raise MemoryError("synthetic immutable allocation failure")

    monkeypatch.setattr(limits, "bytes", freeze, raising=False)
    with pytest.raises(MemoryError):
        encode_report({"v": "x"})
    assert len(arenas) == 1 and arenas[0]() is None
    with pytest.raises(ValueError, match="released memoryview"):
        _ = views[0].obj


def test_owner_construction_failure_cannot_retain_preallocated_arena(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    arenas = track_arenas(monkeypatch)
    original = limits.JsonBuffer.__init__
    calls = 0

    def initialize(self: limits.JsonBuffer, limit: int, backing: bytearray | None = None) -> None:
        nonlocal calls
        calls += 1
        original(self, limit, backing)
        if calls == 2:  # First construction was the allocation-free size pass.
            raise MemoryError("synthetic work owner construction failure")

    monkeypatch.setattr(limits.JsonBuffer, "__init__", initialize)
    enabled = gc.isenabled()
    gc.disable()
    try:
        with pytest.raises(MemoryError) as retained:
            encode_report({"v": "x" * 1024})
        assert retained.value.__traceback__ is not None and calls == 2
        assert all(arena() is None for arena in arenas)
    finally:
        if enabled:
            gc.enable()
