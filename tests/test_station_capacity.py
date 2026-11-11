"""Three station reservations: no provider work before admission, no borrowed debt."""

from __future__ import annotations

import asyncio

import pytest

from sobafm.catalog import RecordingKey
from sobafm.discord_adapter import Track
from sobafm.gemini_match import Matched, Refused
from sobafm.pcm import BANK_BYTES, PcmBankPool, PcmCapacityError
from tests.test_discord_adapter import (
    FakeChannel,
    FakeGuild,
    FakeInteraction,
    FakeMatcher,
    Producer,
    harness,
    inter,
    offline,
    recording,
    run,
    settle,
)
from tests.test_lyria import until


def test_three_pairs_are_reserved_before_allocation_and_never_borrowed() -> None:
    allocations: list[int] = []

    def allocate(size: int) -> bytearray:
        allocations.append(size)
        return bytearray(size)

    pool = PcmBankPool(allocate=allocate)
    stations = [pool.reserve(guild) for guild in range(3)]
    assert not allocations
    with pytest.raises(PcmCapacityError):
        pool.reserve(4)
    banks = [station.acquire() for station in stations for _ in range(2)]
    assert allocations == [BANK_BYTES] * 6 and pool.leased == 6
    assert banks[-1].release()
    with pytest.raises(PcmCapacityError):
        stations[0].acquire()  # another guild's free replacement slot is unavailable
    for bank in banks:
        assert bank.release()
    for guild in range(3):
        assert pool.release(guild)
    assert pool.owners == ()


def test_reservation_retains_bank_views_and_voice_frames_after_stop() -> None:
    pool = PcmBankPool(clock=lambda: 10)
    station = pool.reserve(1)
    pool.reserve(2)
    pool.reserve(3)
    bank = station.acquire()
    region = bank.reserve_write(4)
    assert region is not None
    view = region[:]
    bank.commit_write(0)
    bank.cancel()
    bank.cancel()
    assert bank.facts().cancelled_at == 10
    assert not bank.release() and not pool.release(1)
    assert pool.reserve(1) is station  # a same-guild restart inherits its old debt
    replacement = pool.reserve(1).acquire()
    with pytest.raises(PcmCapacityError):
        pool.reserve(1).acquire()
    assert replacement.release()
    credit = station.claim_voice_reader()
    assert credit is not None
    frame = credit.handoff(b"pcm")
    del credit
    view.release()
    assert bank.release() and not pool.release(1)
    with pytest.raises(PcmCapacityError):
        pool.reserve(4)
    del frame
    assert pool.release(1)
    replacement = pool.reserve(4)
    assert replacement is not station
    with pytest.raises(PcmCapacityError):
        station.acquire()  # stale owners cannot reenter a returned reservation
    assert station.claim_voice_reader() is None


@offline
async def test_fourth_request_is_refused_while_three_interpretations_are_pending() -> None:
    entered = 0
    ready = asyncio.Event()
    release = asyncio.Event()

    class HeldMatcher(FakeMatcher):
        async def match(
            self, request: str, played: frozenset[RecordingKey] = frozenset()
        ) -> Matched | Refused:
            nonlocal entered
            entered += 1
            if entered == 3:
                ready.set()
            await release.wait()
            return await super().match(request, played)

    h = harness()
    matcher = HeldMatcher(recording("a"))
    h.adapter.matcher = matcher
    rooms: list[FakeChannel] = []

    def interaction(room: FakeChannel, *, manager: bool = False) -> FakeInteraction:
        fake = inter(room, manager=manager)
        fake.guild = room.guild
        return fake

    for guild_id in range(1, 5):
        room = FakeChannel(f"room-{guild_id}")
        room.guild = FakeGuild(guild_id)
        rooms.append(room)
        await run(h.adapter.join, interaction(room, manager=True))
    requests = [
        asyncio.create_task(run(lambda i: h.adapter.play(i, "piano"), interaction(room)))
        for room in rooms[:3]
    ]
    await ready.wait()
    refused = await run(lambda i: h.adapter.play(i, "piano"), interaction(rooms[3]))
    assert "Three stations" in str(refused.texts()) and entered == 3
    assert h.pool.leased == 0
    await run(h.adapter.stop, interaction(rooms[0]))
    refused = await run(lambda i: h.adapter.play(i, "piano"), interaction(rooms[3]))
    assert "Three stations" in str(refused.texts()) and entered == 3
    release.set()
    await asyncio.gather(*requests)
    await settle()
    assert h.pool.leased == 2  # the stopped interpretation settled without starting audio
    accepted = await run(lambda i: h.adapter.play(i, "piano"), interaction(rooms[3]))
    assert "accepted" in str(accepted.texts()) and entered == 4
    assert h.pool.leased == 3
    for player in h.adapter.players.values():
        if player.reservation is not None:
            assert player.reservation.leased == 1
    assert await h.adapter.shutdown()


@offline
async def test_stopped_station_keeps_native_and_voice_debt_until_actual_release() -> None:
    producer = Producer()
    producer.hold = True
    h = harness(producer=producer)
    rooms = [FakeChannel(str(guild), guild=FakeGuild(guild)) for guild in range(1, 5)]

    def listener(guild: int, *, manager: bool = False) -> FakeInteraction:
        room = rooms[guild - 1]
        fake = inter(room, manager=manager)
        fake.guild = room.guild
        return fake

    for guild in range(1, 5):
        await run(h.adapter.join, listener(guild, manager=True))
        if guild < 4:
            await run(lambda i: h.adapter.play(i, "piano"), listener(guild))
    await settle()
    one = h.adapter.players[1]
    frame = one.output.read()
    track = one.source._current  # pyright: ignore[reportPrivateUsage]
    assert isinstance(track, Track)
    await settle()
    ending = asyncio.create_task(run(h.adapter.stop, listener(1)))
    await until(lambda: track.bank.facts().cancelled_at is not None)
    original_clock = track.bank.facts().cancelled_at
    track.bank.cancel()
    assert track.bank.facts().cancelled_at == original_clock
    refused = await run(lambda i: h.adapter.play(i, "piano"), listener(4))
    assert "Three stations" in str(refused.texts())
    assert h.pool.leased == 3  # stopped producer still owns its slot
    producer.release.set()
    await ending
    assert h.pool.leased == 2 and h.pool.voice_readers == 1
    refused = await run(lambda i: h.adapter.play(i, "piano"), listener(4))
    assert "Three stations" in str(refused.texts())  # frame survives the producer
    del frame
    accepted = await run(lambda i: h.adapter.play(i, "piano"), listener(4))
    assert "accepted" in str(accepted.texts())
    assert one.reservation is None
    assert set(h.pool.owners) == {2, 3, 4}
    assert await h.adapter.shutdown()
