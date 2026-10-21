import asyncio
from typing import Any

import pytest

from sobafm.mixer import Handoff
from sobafm.pcm import SILENCE
from sobafm.program import Outcome
from tests.test_catalog_engine import PLAN, rig, track
from tests.test_catalog_station import Playback, playback


def pcm(sample: int) -> bytes:
    return sample.to_bytes(2, "little", signed=True) * 1920


def request_skip(r: Playback) -> asyncio.Future[Outcome]:
    target = r.station.skip_target()
    assert target is not None
    return r.station.skip(target)


async def prepared(*, ready: bool = True) -> Playback:
    r = playback(rig((track(1),), (track(2),), (track(3),)))
    started = r.station.play(PLAN, "Member")
    first = await r.select()
    first.send_audio(6, sample=1000)
    await r.tick()
    r.station.mixer.read()
    await r.tick()
    assert started.result() is Outcome.PLAYING
    second = await r.select()
    second.send_audio(6 if ready else 1, sample=2000)
    await r.tick()
    return r


async def test_skip_is_successive_without_drain_and_settles_only_actual_pcm() -> None:
    r = await prepared()
    first, second = r.selection.made
    program = r.station.program
    frames = len(first.frames)
    skipped = request_skip(r)
    assert not skipped.done()
    assert request_skip(r).result() is Outcome.BUSY
    assert r.station.heard_source is first
    assert not r.selection.history.excludes(second.track)
    await r.tick()  # natural handover must not overwrite the explicit skip arm
    assert r.station.mixer.read() == pcm(2000)
    assert len(first.frames) == frames  # no fade, overlap, outgoing pop or false drain
    assert not skipped.done()
    assert r.station.heard_source is first
    await r.tick()
    assert skipped.result() is Outcome.PLAYING
    assert r.station.program is program
    assert r.station.heard_source is second
    assert first.retired
    assert r.selection.history.excludes(second.track)
    assert program is not None
    assert program.first_credit is first.credits
    await r.station.close()


async def test_unready_prefetch_does_not_schedule_a_later_skip_or_new_query() -> None:
    r = await prepared(ready=False)
    first, second = r.selection.made
    queries = list(r.selection.catalog.queries)
    assert request_skip(r).result() is Outcome.NO_MATCH
    assert not first.retired
    second.send_audio(5, sample=2000)
    await r.tick()
    assert second.ready
    assert r.station.mixer.read() == pcm(1000)
    assert r.station.heard_source is first
    assert r.selection.catalog.queries == queries
    assert len(r.selection.made) == 2
    await r.station.close()


async def test_failed_incoming_never_cuts_or_announces_the_outgoing() -> None:
    r = await prepared()
    first, second = r.selection.made
    skipped = request_skip(r)
    second.finish(Outcome.FAILED)
    assert r.station.mixer.read() == pcm(1000)
    await r.tick()
    assert skipped.result() is Outcome.FAILED
    assert r.station.heard_source is first
    assert not first.retired
    await r.station.close()


@pytest.mark.parametrize("end", ["cancel", "stop", "replace", "voice", "player", "close"])
async def test_pending_skip_is_invalidated_without_late_cutover(end: str) -> None:
    r = await prepared()
    skipped = request_skip(r)
    if end == "cancel":
        assert r.station.cancel_skip(skipped) is Outcome.STOPPED
    elif end == "stop":
        r.station.stop()
    elif end == "replace":
        r.station.play(PLAN, "New request")
    elif end == "voice":
        r.station.invalidate_playback()
    elif end == "player":
        r.player.end_thread()
        await asyncio.sleep(0)
    else:
        await r.station.close()
    assert skipped.done()
    assert skipped.result() is not Outcome.PLAYING
    assert r.station.mixer.read() != pcm(2000)
    if end in {"stop", "close"}:
        assert r.station.mixer.read() == SILENCE
    await r.station.close()


@pytest.mark.parametrize("end", ["cancel", "stop", "replace"])
async def test_delivery_winning_atomic_withdrawal_keeps_success_and_credits(
    monkeypatch: pytest.MonkeyPatch,
    end: str,
) -> None:
    r = await prepared()
    first, second = r.selection.made
    skipped = request_skip(r)
    original = r.station.mixer.withdraw

    def delivered_before_lock(handoff: Handoff) -> Handoff | None:
        assert r.station.mixer.read() == pcm(2000)
        return original(handoff)

    monkeypatch.setattr(r.station.mixer, "withdraw", delivered_before_lock)
    if end == "cancel":
        assert r.station.cancel_skip(skipped) is Outcome.PLAYING
    elif end == "stop":
        r.station.stop()
    else:
        r.station.play(PLAN, "New request")
    assert skipped.result() is Outcome.PLAYING
    assert r.selection.history.excludes(second.track)
    assert first.retired
    if end == "stop":
        assert r.station.mixer.read() == SILENCE
    else:
        assert r.station.heard_source is second
    await r.station.close()


async def test_natural_read_winning_arm_has_no_delayed_skip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    r = await prepared()
    first, second = r.selection.made
    original = r.station.mixer.arm

    def natural_before_arm(*args: Any, **kwargs: Any) -> Handoff | None:
        original(*args, **kwargs)
        assert r.station.mixer.read() == pcm(2000)
        return original(*args, **kwargs)

    target = r.station.skip_target()
    assert target is not None
    monkeypatch.setattr(r.station.mixer, "arm", natural_before_arm)
    assert r.station.skip(target).result() is Outcome.REPLACED
    monkeypatch.setattr(r.station.mixer, "arm", original)
    await r.tick()
    assert r.station.heard_source is second
    assert first.retired
    assert r.station.mixer.read() == pcm(2000)
    await r.station.close()
