"""Real catalog selection with a station/player and fixture-only PCM producers."""

from dataclasses import dataclass

from sobafm.failures import Failure
from sobafm.jamendo import CatalogError
from sobafm.pcm import SILENCE
from sobafm.program import Outcome
from sobafm.station import Station
from sobafm.status import Statuses
from tests.doubles import FakePlayer, settle
from tests.test_catalog import credit, discovered
from tests.test_catalog_engine import PLAN, FixtureTrack, Rig, rig, track


@dataclass
class Playback:
    selection: Rig
    station: Station
    player: FakePlayer

    async def tick(self) -> None:
        await settle()
        await self.station.reconcile()
        await settle()

    async def select(self) -> FixtureTrack:
        await self.tick()
        await self.tick()
        return self.selection.made[-1]


def playback(selection: Rig) -> Playback:
    player = FakePlayer()

    async def show(channel: int, status: str | None) -> bool:
        return True

    station = Station(
        lambda: player,
        lambda: 1,
        engines=[selection.engine],
        volume=1,
        statuses=Statuses(lambda: 10, show, clock=selection.clock),
        clock=selection.clock,
    )
    return Playback(selection, station, player)


async def test_real_engine_prefetches_one_and_commits_only_the_actual_incoming_read() -> None:
    r = playback(rig((track(1),), (track(2),), (track(3),)))
    started = r.station.play(PLAN, "Member")
    first = await r.select()
    first.send_audio(6, sample=1000)
    await r.tick()
    assert not started.done()
    assert not r.selection.history.excludes(first.track)
    assert r.station.mixer.read() == (1000).to_bytes(2, "little", signed=True) * 1920
    await r.tick()
    second = await r.select()
    assert started.result() is Outcome.PLAYING
    assert r.selection.history.excludes(first.track)
    assert len(r.selection.made) == 2
    second.send_audio(6, sample=2000)
    second.finish()
    first.finish()
    await r.tick()  # arm before the final old frame; no tick at the actual boundary
    for _ in range(299):
        assert r.station.mixer.read() == (1000).to_bytes(2, "little", signed=True) * 1920
    assert r.station.heard_source is first
    assert not r.selection.history.excludes(second.track)
    assert r.station.mixer.read() == (2000).to_bytes(2, "little", signed=True) * 1920
    assert r.station.heard_source is first
    await r.tick()
    assert r.station.heard_source is second
    assert r.station.program is not None
    assert r.station.program.first_credit is first.credits
    assert first.retired
    assert r.selection.history.excludes(second.track)
    await r.select()
    assert len(r.selection.made) == 3
    r.station.stop()
    assert r.station.mixer.read() == SILENCE
    await r.station.close()


async def test_credit_status_follow_accepted_pcm_and_keep_first_reply_snapshot() -> None:
    first_track = track(1)
    second_track = track(2)
    r = playback(rig((first_track,), (second_track,)))
    statuses: list[str | None] = []

    async def show(channel: int, status: str | None) -> bool:
        statuses.append(status)
        return False  # missing optional status permission never changes audio

    r.station.statuses = Statuses(lambda: 10, show, clock=r.selection.clock)
    started = r.station.play(PLAN, "Member")
    first = await r.select()
    first.send_audio(6)
    await r.tick()
    assert r.station.program is not None
    program = r.station.program
    assert program.first_credit is None
    assert r.station.heard_source is None
    r.station.mixer.read()
    await r.tick()
    assert started.result() is Outcome.PLAYING
    assert program.first_credit is first.credits
    second = await r.select()
    assert r.station.heard_source is first
    second.send_audio(6, sample=1234)
    first.finish()
    second.finish()
    await r.tick()
    for _ in range(299):
        r.station.mixer.read()
    r.station.mixer.read()  # actual incoming PCM, not an event-loop prediction
    assert r.station.heard_source is first
    await r.tick()
    assert r.station.heard_source is second
    assert program.first_credit is first.credits
    assert second.credits is not None
    assert f"{second.title} — {second.credits.artist}" in statuses
    assert program.playing
    await r.station.close()


async def test_metadata_failure_restores_the_existing_program_without_a_failed_source() -> None:
    r = playback(rig((track(1),), (track(2),), CatalogError("http", Failure.REJECTED)))
    started = r.station.play(PLAN, "Member")
    first = await r.select()
    first.send_audio(6)
    await r.tick()
    r.station.mixer.read()
    await r.tick()
    previous = r.station.program
    assert started.result() is Outcome.PLAYING
    # A new request owns its own cursor; the prefetch from the old program is
    # cancelled without treating its result as a failure of the replacement.
    replacement = r.station.play(PLAN, "Other")
    requested = r.station.program
    await r.tick()
    await r.tick()
    assert replacement.result() is Outcome.REJECTED
    assert r.station.program is previous
    assert r.station.heard_source is first
    assert all(source.program is not requested for source in r.selection.made)
    await r.station.close()


async def test_status_counts_utf16_without_changing_full_credit() -> None:
    value = discovered(title="🙂" * 200, artist="🎵" * 200)
    record = credit(title=value.title, artist=value.artist)
    r = playback(rig((value.with_credits(record),)))
    statuses: list[str | None] = []

    async def show(channel: int, status: str | None) -> bool:
        statuses.append(status)
        return True

    r.station.statuses = Statuses(lambda: 10, show, clock=r.selection.clock)
    started = r.station.play(PLAN, "Member")
    source = await r.select()
    source.send_audio(6)
    await r.tick()
    r.station.mixer.read()
    await r.tick()
    assert started.result() is Outcome.PLAYING
    actual = next(status for status in statuses if status is not None)
    assert len(actual.encode("utf-16-le")) // 2 <= 500
    assert actual.startswith(value.title)
    assert r.station.heard_source is source
    assert source.credits == record
    assert source.credits is not None
    assert source.credits.artist == "🎵" * 200
    await r.station.close()
