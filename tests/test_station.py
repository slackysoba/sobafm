"""Deterministic schedules for desired, prepared and heard station facts."""

from dataclasses import replace

import pytest

from sobafm.catalog import (
    AdmissionReview,
    Availability,
    Catalog,
    CreditSnapshot,
    Partition,
    Query,
    Recording,
    RecordingKey,
    RecordingKind,
    SnapshotState,
    Source,
    metadata_digest,
)
from sobafm.credits import render_credits
from sobafm.station import HISTORY_LIMIT, EndReason, Outcome, Station, select_next

LINK = "https://example.invalid/synthetic/track"


def recording(identifier: str) -> Recording:
    return Recording(
        key=RecordingKey(Source.JAMENDO, identifier),
        title=f"Song {identifier}",
        primary_artist="Synthetic Artist",
        creators=("Synthetic Artist",),
        provenance=LINK,
        kind=RecordingKind.COMPLETE,
        kind_evidence=LINK,
        duration_seconds=180,
        availability=Availability.AVAILABLE,
        composition=None,
        composer=None,
        facts=(),
        credits=CreditSnapshot(f"credit-{identifier}", ("Synthetic notice.",), None, None),
    )


def playing(identifier: str = "a") -> Station:
    station = Station()
    program = station.request("calm piano").program
    assert station.prepared(program, recording(identifier)) is Outcome.ACCEPTED
    audio = station.claim_ready()
    assert audio is not None
    assert station.switched(audio) is Outcome.ACCEPTED
    return station


def test_heard_source_changes_only_on_actual_switch() -> None:
    station = playing("a")
    heard = station.heard
    assert heard is not None and heard.recording.key.recording_id == "a"
    assert heard.credits == render_credits(recording("a"))

    replacement = station.request("upbeat jazz")
    assert replacement.superseded is None
    assert station.heard is heard
    assert station.desired == replacement.program and station.preparing

    assert station.prepared(replacement.program, recording("b")) is Outcome.ACCEPTED
    assert station.heard is heard and not station.preparing
    audio = station.claim_ready()
    assert audio is not None and station.ready is None
    assert station.claim_ready() is None
    assert station.heard is heard

    assert station.switched(audio) is Outcome.ACCEPTED
    assert station.heard is audio and station.switching is None
    assert station.desired == replacement.program


def test_stale_callbacks_are_ignored() -> None:
    station = playing("a")
    heard = station.heard
    first = station.request("first").program
    second = station.request("second")
    assert second.superseded == first

    assert station.prepared(first, recording("x")) is Outcome.STALE
    assert station.preparation_failed(first) is Outcome.STALE
    assert station.ready is None and station.preparing
    assert station.prepared(second.program, recording("b")) is Outcome.ACCEPTED
    assert station.prepared(second.program, recording("c")) is Outcome.STALE
    ready = station.ready
    assert ready is not None and ready.recording.key.recording_id == "b"
    assert station.preparation_failed(second.program) is Outcome.STALE
    assert station.heard is heard

    # A switch report for audio never claimed, or claimed and superseded, is stale.
    assert station.switched(ready) is Outcome.STALE
    assert station.switch_failed(ready) is Outcome.STALE
    assert station.heard is heard


def test_newer_request_during_switch_keeps_both_facts_honest() -> None:
    station = playing("a")
    old = station.request("old").program
    assert station.prepared(old, recording("b")) is Outcome.ACCEPTED
    audio = station.claim_ready()
    assert audio is not None

    newer = station.request("newer")
    assert newer.superseded is None  # the player owns the claimed audio
    assert station.switched(audio) is Outcome.ACCEPTED
    assert station.heard is audio
    assert station.desired == newer.program and station.preparing
    assert station.switched(audio) is Outcome.STALE


@pytest.mark.parametrize("failure", ["preparation", "credits", "switch"])
def test_failed_or_cancelled_replacement_preserves_heard_source(failure: str) -> None:
    station = playing("a")
    heard = station.heard
    assert heard is not None
    program = station.request("replacement").program
    match failure:
        case "preparation":
            assert station.preparation_failed(program) is Outcome.ACCEPTED
        case "credits":
            unsafe = replace(recording("b"), provenance="https://u:p@example.invalid/x")
            assert station.prepared(program, unsafe) is Outcome.REJECTED
            assert station.preparing and station.ready is None
            assert station.preparation_failed(program) is Outcome.ACCEPTED
        case _:
            assert station.prepared(program, recording("b")) is Outcome.ACCEPTED
            audio = station.claim_ready()
            assert audio is not None
            assert station.switch_failed(audio) is Outcome.ACCEPTED
    assert station.heard is heard
    assert heard.credits == render_credits(recording("a"))
    assert station.desired == heard.program
    assert not station.preparing and station.ready is None and station.switching is None
    assert station.prepared(program, recording("c")) is Outcome.STALE


def test_failure_without_heard_audio_leaves_station_idle() -> None:
    station = Station()
    program = station.request("calm").program
    assert station.preparation_failed(program) is Outcome.ACCEPTED
    assert station.desired is None and station.heard is None and not station.preparing


def test_stop_cancels_unheard_work_until_player_is_silent() -> None:
    station = playing("a")
    heard = station.heard
    pending = station.request("replacement").program
    assert station.stop() == pending
    assert station.desired is None and not station.preparing
    assert station.heard is heard
    assert station.prepared(pending, recording("b")) is Outcome.STALE
    assert station.stop() is None
    station.silenced()
    assert station.heard is None


def test_request_text_is_bounded() -> None:
    station = Station()
    with pytest.raises(ValueError):
        station.request("")
    with pytest.raises(ValueError):
        station.request("x" * 201)
    assert station.request("x" * 200).program.generation == 1


def advance(station: Station, identifier: str) -> None:
    """Continue the desired program with ``identifier`` and hear it."""
    program = station.desired
    assert program is not None and station.needs_next
    assert station.continue_program(program) is Outcome.ACCEPTED
    assert station.prepared(program, recording(identifier)) is Outcome.ACCEPTED
    audio = station.claim_ready()
    assert audio is not None
    assert station.switched(audio) is Outcome.ACCEPTED


def test_a_program_continues_and_keeps_its_history() -> None:
    station = playing("a")
    program = station.desired
    assert program is not None and station.continuing and station.needs_next
    advance(station, "b")
    heard = station.heard
    assert heard is not None and heard.program == program and heard.recording == recording("b")
    assert station.history(program) == {recording(i).key for i in "ab"}
    assert station.continue_program(program) is Outcome.ACCEPTED
    assert station.continue_program(program) is Outcome.STALE  # one prepared next track
    assert HISTORY_LIMIT >= 3 * 256  # covers every recording a snapshot may hold


def test_a_failed_next_track_keeps_the_program() -> None:
    station = playing("a")
    program = station.desired
    assert program is not None
    assert station.continue_program(program) is Outcome.ACCEPTED
    assert station.prepared(program, recording("b")) is Outcome.ACCEPTED
    audio = station.claim_ready()
    assert audio is not None
    assert station.switch_failed(audio) is Outcome.ACCEPTED
    assert station.desired == program and station.needs_next  # it may try again
    assert recording("b").key in station.history(program)  # not retried


def test_exhaustion_ends_the_program_after_its_heard_track() -> None:
    station = playing("a")
    program = station.desired
    heard = station.heard
    assert program is not None and heard is not None
    assert station.continue_program(program) is Outcome.ACCEPTED
    assert station.exhausted(program, EndReason.EXHAUSTED) is Outcome.ACCEPTED
    assert station.ending is EndReason.EXHAUSTED and not station.needs_next
    assert station.desired == program and station.ended is None  # still playing
    assert station.drained(heard) is Outcome.ACCEPTED
    assert station.desired is None and station.heard is None
    assert station.ended is EndReason.EXHAUSTED
    assert station.drained(heard) is Outcome.STALE
    station.request("again")
    assert station.ended is None


def test_a_drained_track_with_its_successor_claimed_continues() -> None:
    station = playing("a")
    program = station.desired
    heard = station.heard
    assert program is not None and heard is not None
    station.continue_program(program)
    station.prepared(program, recording("b"))
    audio = station.claim_ready()
    assert audio is not None
    assert station.drained(heard) is Outcome.ACCEPTED
    assert station.desired == program and station.heard is None and station.continuing
    assert station.switched(audio) is Outcome.ACCEPTED and station.heard == audio


def test_a_failed_replacement_restores_the_heard_programs_progress() -> None:
    station = playing("a")
    old = station.desired
    assert old is not None
    station.continue_program(old)
    station.exhausted(old, EndReason.EXHAUSTED)
    replacement = station.request("other").program
    assert station.ending is None and not station.continuing
    assert station.preparation_failed(replacement) is Outcome.ACCEPTED
    assert station.desired == old and station.ending is EndReason.EXHAUSTED


def test_select_next_skips_history_and_reports_exhaustion() -> None:
    lengths = (("a", 300), ("b", 200))
    recordings = tuple(replace(recording(i), duration_seconds=d) for i, d in lengths)
    catalog = Catalog(True, (Partition(Source.JAMENDO, SnapshotState.COMPLETE, "", recordings),))
    reviews = tuple(
        AdmissionReview(r.key, metadata_digest(r), True, True, True, LINK) for r in recordings
    )
    first = select_next(catalog, reviews, Query(), frozenset())
    assert first is not None and first.key.recording_id == "a"  # longer first on a tie
    second = select_next(catalog, reviews, Query(), frozenset({first.key}))
    assert second is not None and second.key.recording_id == "b"
    assert select_next(catalog, reviews, Query(), frozenset({first.key, second.key})) is None
