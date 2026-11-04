"""Interpreter facts gate the owner preflight and appear in the owner report (#67)."""

from __future__ import annotations

import sys
import threading
from types import SimpleNamespace
from typing import cast

import pytest

from evaluations.fma_validation import controller, owner
from evaluations.fma_validation.cases import NIGHT_OWL
from evaluations.fma_validation.ingress import INTERPRETER, Scan
from evaluations.fma_validation.limits import CaseClock, encode_report
from evaluations.fma_validation.observation import ObservationWorkspace
from evaluations.fma_validation.packet import Packet
from evaluations.fma_validation.process import ProcessPin
from evaluations.linux_resource.probe import ProbeError

ACCEPTED: dict[str, object] = {
    "version": "3.14.7",
    "abiflags": "",
    "debug": False,
    "free_threaded": False,
}


def test_expected_facts_are_the_pinned_release_gil_build() -> None:
    assert owner.INTERPRETER_FACTS == ACCEPTED
    owner.check_interpreter(dict(ACCEPTED))


@pytest.mark.parametrize(
    ("key", "value", "code"),
    [
        ("version", "3.14.6", "interpreter_version"),
        ("version", "3.14.8", "interpreter_version"),
        ("version", "3.14.10", "interpreter_version"),
        ("version", "3.14.7rc1", "interpreter_version"),
        ("version", "3.14.7+", "interpreter_version"),
        ("abiflags", "t", "interpreter_abiflags"),
        ("abiflags", "d", "interpreter_abiflags"),
        ("abiflags", "td", "interpreter_abiflags"),
        ("abiflags", None, "interpreter_abiflags"),
        ("debug", True, "interpreter_debug_build"),
        ("debug", 0, "interpreter_debug_build"),
        ("free_threaded", True, "interpreter_free_threaded"),
        ("free_threaded", 0, "interpreter_free_threaded"),
    ],
)
def test_each_rejected_fact_fails_closed(key: str, value: object, code: str) -> None:
    facts = dict(ACCEPTED)
    facts[key] = value
    with pytest.raises(ProbeError, match=f"^{code}$"):
        owner.check_interpreter(facts)


@pytest.mark.parametrize("change", ["missing", "extra"])
def test_fact_set_is_closed(change: str) -> None:
    facts = dict(ACCEPTED)
    if change == "missing":
        del facts["debug"]
    else:
        facts["gil_enabled"] = True
    with pytest.raises(ProbeError, match="^interpreter_facts$"):
        owner.check_interpreter(facts)


def observe(
    monkeypatch: pytest.MonkeyPatch,
    *,
    version: str = "3.14.7",
    abiflags: str = "",
    debug: bool = False,
    gil_disabled: object = 0,
) -> dict[str, object]:
    monkeypatch.setattr(owner.platform, "python_version", lambda: version)
    monkeypatch.setattr(owner.sys, "abiflags", abiflags, raising=False)
    if debug:
        monkeypatch.setattr(owner.sys, "gettotalrefcount", lambda: 0, raising=False)
    else:
        monkeypatch.delattr(owner.sys, "gettotalrefcount", raising=False)

    def config_var(name: str) -> object:
        assert name == "Py_GIL_DISABLED"
        return gil_disabled

    monkeypatch.setattr(owner.sysconfig, "get_config_var", config_var)
    return owner.interpreter_facts()


def test_observed_facts_of_accepted_build(monkeypatch: pytest.MonkeyPatch) -> None:
    facts = observe(monkeypatch)
    assert facts == ACCEPTED
    owner.check_interpreter(facts)


@pytest.mark.parametrize(
    ("version", "abiflags", "debug", "gil_disabled", "code"),
    [
        ("3.14.10", "", False, 0, "interpreter_version"),
        ("3.14.7rc1", "", False, 0, "interpreter_version"),
        ("3.14.7", "t", False, 0, "interpreter_abiflags"),
        ("3.14.7", "d", False, 0, "interpreter_abiflags"),
        ("3.14.7", "", True, 0, "interpreter_debug_build"),
        ("3.14.7", "", False, 1, "interpreter_free_threaded"),
        # An unknown build configuration is not evidence of a GIL build.
        ("3.14.7", "", False, None, "interpreter_free_threaded"),
    ],
)
def test_observed_rejected_build_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    version: str,
    abiflags: str,
    debug: bool,
    gil_disabled: object,
    code: str,
) -> None:
    facts = observe(
        monkeypatch, version=version, abiflags=abiflags, debug=debug, gil_disabled=gil_disabled
    )
    with pytest.raises(ProbeError, match=f"^{code}$"):
        owner.check_interpreter(facts)


def test_observed_facts_describe_this_interpreter() -> None:
    facts = owner.interpreter_facts()
    assert set(facts) == set(ACCEPTED)
    assert facts["abiflags"] == sys.abiflags
    assert facts["debug"] is hasattr(sys, "gettotalrefcount")
    assert type(facts["version"]) is str and type(facts["free_threaded"]) is bool


@pytest.mark.parametrize("key", ["version", "abiflags", "debug", "free_threaded"])
def test_owner_preflight_rejects_before_any_observation(key: str) -> None:
    facts = dict(ACCEPTED)
    facts[key] = {"version": "3.14.8", "abiflags": "t", "debug": True, "free_threaded": True}[key]

    class Untouched:
        def environment(self, *, intake: bool) -> None:
            raise AssertionError("preflight observed the environment of a rejected interpreter")

    packet = cast(Packet, SimpleNamespace())
    with pytest.raises(ProbeError, match="^interpreter_"):
        owner.preflight(packet, cast(ObservationWorkspace, Untouched()), facts)


def owner_report(monkeypatch: pytest.MonkeyPatch, facts: dict[str, object]) -> dict[str, object]:
    """Run ``owner.execute`` with fakes around the unchanged report assembly."""
    pin = ProcessPin(2, 1, 1, "/python", 1, 1, 1000, "/")
    clock = CaseClock(1e12, 1e12, wall=lambda: 100.0, monotonic=lambda: 100.0)
    pending = threading.Event()
    pending.set()
    seen: list[dict[str, object]] = []

    class Resources:
        swap = 0

        def delta(self, _before: object) -> dict[str, int]:
            return {"oom": 0, "oom_kill": 0}

    class Workspace:
        debt = False

        def resources(self, cleanup: bool = False) -> Resources:
            return Resources()

    class Reader:
        def join(self, _timeout: float) -> bool:
            return True

        def report(self) -> dict[str, object]:
            return {"completed": True}

    class Consumer:
        finished = True

        def receipt(self) -> dict[str, object]:
            return {}

    class Intake:
        def __init__(self) -> None:
            self.pending = pending

        def start(self, *, execute: bool) -> None:
            assert execute

        def join(self, _timeout: float) -> bool:
            return True

        def cancel(self, *_args: object, **_kwargs: object) -> None:
            pass

        def read_into(self, _consumer: object) -> object:
            return Reader()

        def report(self) -> dict[str, object]:
            return {"released": True, "cleanup_deadline_met": True}

    def preflight(
        _packet: Packet, _workspace: object, interpreter: dict[str, object]
    ) -> tuple[ProcessPin, ProcessPin]:
        seen.append(interpreter)
        return pin, pin

    def observed() -> dict[str, object]:
        return facts

    def case_clock(*_args: object) -> CaseClock:
        return clock

    def workspace(_clock: object) -> Workspace:
        return Workspace()

    def intake(*_args: object) -> Intake:
        return Intake()

    def consumer(*_args: object, **_kwargs: object) -> Consumer:
        return Consumer()

    def inventory(*_args: object, **_kwargs: object) -> set[int]:
        return set()

    def signal(*_args: object) -> None:
        pass

    monkeypatch.setattr(owner, "interpreter_facts", observed)
    monkeypatch.setattr(owner, "preflight", preflight)
    monkeypatch.setattr(owner, "CaseClock", case_clock)
    monkeypatch.setattr(owner, "ObservationWorkspace", workspace)
    monkeypatch.setattr(owner, "LocalIntake", intake)
    monkeypatch.setattr(owner, "ValidationConsumer", consumer)
    monkeypatch.setattr(owner, "inventory", inventory)
    monkeypatch.setattr(owner.signal, "signal", signal)
    packet = cast(
        Packet,
        SimpleNamespace(
            whole_end=1e12, expires=1e12, request=None, case=NIGHT_OWL, fingerprint="a" * 64
        ),
    )
    result = owner.execute(packet)
    # The preflight gate and the report share one observation.
    assert seen == [facts] and seen[0] is facts
    return result


def test_owner_report_carries_the_four_facts(monkeypatch: pytest.MonkeyPatch) -> None:
    facts = dict(ACCEPTED)
    result = owner_report(monkeypatch, facts)
    assert result["interpreter"] == ACCEPTED
    assert list(cast(dict[str, object], result["interpreter"])) == [
        "version",
        "abiflags",
        "debug",
        "free_threaded",
    ]
    assert result["passed"] is True


def owner_envelope(interpreter: object) -> tuple[dict[str, object], Packet]:
    packet = cast(
        Packet,
        SimpleNamespace(case=NIGHT_OWL, fingerprint="a" * 64, whole_end=1e12, expires=1e12),
    )
    report: dict[str, object] = {key: None for key in controller.OWNER_FIELDS}
    report.update(
        schema=NIGHT_OWL.schema("owner"),
        case_binding=NIGHT_OWL.record(),
        packet_sha256="a" * 64,
        passed=True,
        failure=None,
        catalog_admitted=False,
        container_exit_observed=False,
        interpreter=interpreter,
        intake={},
        reader={},
        validation={},
        cleanup={},
    )
    return report, packet


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("version", "3.14.8"),
        ("abiflags", "t"),
        ("debug", True),
        ("free_threaded", True),
    ],
)
def test_controller_rejects_owner_report_with_other_facts(key: str, value: object) -> None:
    facts = dict(ACCEPTED)
    facts[key] = value
    report, packet = owner_envelope(facts)
    with pytest.raises(ProbeError, match="^owner_interpreter_facts_failed$"):
        controller.validate_owner(report, packet)


def test_controller_requires_the_facts_field() -> None:
    report, packet = owner_envelope(dict(ACCEPTED))
    # Accepted facts pass this gate; validation proceeds to the next evidence.
    with pytest.raises(ProbeError, match="^owner_cleanup_evidence_failed$"):
        controller.validate_owner(report, packet)
    del report["interpreter"]
    with pytest.raises(ProbeError, match="^owner_result_failed$"):
        controller.validate_owner(report, packet)


def test_owner_wire_schema_requires_closed_facts() -> None:
    def accepted(value: object) -> bool:
        wire = encode_report(value)
        with memoryview(wire) as view:
            scan = Scan(view, INTERPRETER)
            scan.run()
            return scan.valid

    assert accepted(dict(ACCEPTED))
    for changed in (
        {k: v for k, v in ACCEPTED.items() if k != "debug"},
        {**ACCEPTED, "extra": False},
        {**ACCEPTED, "debug": "false"},
        {**ACCEPTED, "version": "3" * 17},
    ):
        assert not accepted(changed)
