import asyncio
import threading
from collections import deque
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from typing import cast

import discord
import pytest

import sobafm.mixer as mixing
from sobafm.audio_budget import (
    LYRIA_RECEIVE_RESERVATION_BYTES,
    PCM_LIMIT,
    TOTAL_LIMIT,
    AudioBytes,
    AudioLedger,
)
from sobafm.deck import Deck, EndReason
from sobafm.mixer import Mixer
from sobafm.owned_pcm import OwnedPCM, PCMCapacityError, PCMOwner, PCMReceipt
from sobafm.pcm import FRAME_BYTES, SILENCE, FrameSplitter
from sobafm.plan import MusicPlan
from sobafm.source import State
from sobafm.station import SessionPool, Station
from sobafm.status import Statuses
from sobafm.track_deck import TrackFailure
from tests.doubles import FakeLyria, PCMFixtureLedger, drain_pcm, settle
from tests.test_track_deck import Rig
from tests.test_track_deck import settle as settle_track


def owner(size: int) -> PCMOwner:
    # Tiny source/staging allowances still fund the owner's separate static backing.
    lease = AudioLedger().reserve_pcm_capacity(size + len(SILENCE))
    assert lease is not None
    return PCMOwner(lease)


def test_one_parent_precedes_unknown_work_and_survives_replacement() -> None:
    audio = AudioLedger()
    pcm = audio.pcm
    debt = audio.unbounded()
    assert audio.pcm is pcm
    assert audio.reserved == AudioBytes(pcm=PCM_LIMIT)
    storage = pcm.storage(4)
    paid = storage.copy(memoryview(b"paid"))
    assert paid.data == b"paid"
    assert storage.used == 4
    receipt = paid.receipt
    del paid
    receipt.release()
    child = pcm.capacity.reserve_pcm(PCM_LIMIT - len(SILENCE))
    assert child is not None
    assert audio.pcm.capacity.reserve_pcm(1) is None
    child.release()
    assert audio.pcm is pcm
    assert not audio.bounded
    assert debt.size is None
    unknown_first = AudioLedger()
    unknown_first.unbounded()
    with pytest.raises(PCMCapacityError):
        _ = unknown_first.pcm


async def test_unknown_without_funded_parent_refuses_replacement_before_work() -> None:
    audio = AudioLedger()
    audio.unbounded()
    lyria = FakeLyria()

    async def show(channel: int, status: str | None) -> bool:
        return True

    with pytest.raises(PCMCapacityError):
        Station(
            lambda: None,
            lambda: 0,
            lyria.connect,
            SessionPool(4),
            audio=audio,
            volume=1,
            statuses=Statuses(lambda: None, show),
        )
    assert lyria.sessions == []
    assert audio.reserved == AudioBytes()
    assert not audio.bounded
    assert audio.reserve_pcm_capacity(1) is None


async def test_full_runtime_pcm_and_large_native_refuse_before_fake_producer(
    tmp_path: Path,
) -> None:
    rig = Rig(tmp_path)
    allocation = rig.supervisor.envelope.allocation
    assert allocation is not None
    rig.supervisor.envelope = replace(
        rig.supervisor.envelope,
        allocation=replace(allocation, native=max(8 * 1024**2, TOTAL_LIMIT - PCM_LIMIT)),
    )
    rig.deck.start()
    await rig.deck.wait_ended()
    assert rig.deck.failure is TrackFailure.CAPACITY
    assert not rig.boundary.prepared
    assert not rig.child.bound
    assert not rig.child.writes
    assert rig.slots.count == 0
    assert rig.audio.reserved == AudioBytes(pcm=PCM_LIMIT)


def test_insufficient_static_funding_refuses_owner_before_pcm_growth() -> None:
    lease = AudioLedger().reserve_pcm_capacity(len(SILENCE) - 1)
    assert lease is not None
    with pytest.raises(PCMCapacityError):
        PCMOwner(lease)


async def test_static_charge_survives_cleanup_and_station_replacement_once() -> None:
    audio = AudioLedger()
    lyria = FakeLyria()

    async def show(channel: int, status: str | None) -> bool:
        return True

    stations = [
        Station(
            lambda: None,
            lambda: 0,
            lyria.connect,
            SessionPool(4),
            audio=audio,
            volume=1,
            statuses=Statuses(lambda: None, show),
        )
        for _ in range(2)
    ]
    assert stations[0].pcm is stations[1].pcm is audio.pcm
    for station in stations:
        wrapper = station.mixer.player_source()
        assert wrapper is not None
        wrapper.cleanup()
        await asyncio.sleep(0)
        # One static frame remains unavailable; replacement/cleanup cannot fund
        # another static charge or refund the original backing.
        assert audio.pcm.capacity.reserve_pcm(PCM_LIMIT - len(SILENCE) + 1) is None
        remaining = audio.pcm.capacity.reserve_pcm(PCM_LIMIT - len(SILENCE))
        assert remaining is not None
        remaining.release()
        await station.close()
    assert not audio.bounded
    assert audio.reserved == AudioBytes(pcm=PCM_LIMIT, other=2 * LYRIA_RECEIVE_RESERVATION_BYTES)


async def test_eight_frame_parent_refuses_staging_before_unpaid_crossfade_growth() -> None:
    lease = AudioLedger().reserve_pcm_capacity(8 * FRAME_BYTES)
    assert lease is not None
    pcm = PCMOwner(lease)
    storage = pcm.storage(4 * FRAME_BYTES)
    frames = deque(storage.silence(FRAME_BYTES) for _ in range(4))
    admitted = [frame.data for frame in frames]
    mixer = Mixer(1, pcm)
    assert mixer.player_source() is None  # static1 + source4 leaves only3 for required4 staging
    assert mixer.read() is SILENCE
    assert [frame.data for frame in frames] == admitted
    del admitted
    drain_pcm(frames)


async def test_actual_crossfade_peak_includes_persistent_silence_and_fits_funded_parent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    capacity = 9 * FRAME_BYTES
    lease = AudioLedger().reserve_pcm_capacity(capacity)
    assert lease is not None
    pcm = PCMOwner(lease)
    storage = pcm.storage(4 * FRAME_BYTES)

    class Source:
        def __init__(self) -> None:
            self.frames = deque(storage.silence(FRAME_BYTES) for _ in range(2))

    left, right = Source(), Source()
    inputs: list[bytes] = []
    prior: bytes | None = None
    observed: list[int] = []

    def scaled(frame: bytes, _width: int, _gain: float) -> bytes:
        inputs.append(frame)
        return memoryview(frame).tobytes()

    def added(first: bytes, second: bytes, _width: int) -> bytes:
        result = memoryview(first).tobytes()
        if prior is not None:
            sources = inputs + [frame.data for source in (left, right) for frame in source.frames]
            live = [*sources, first, second, result, prior, SILENCE]
            assert len(sources) == 4
            assert len({id(data) for data in live}) == 9
            assert all(len(data) == FRAME_BYTES for data in live)
            assert pcm.capacity.reserve_pcm(1) is None
            observed.append(sum(map(len, live)))
        return result

    monkeypatch.setattr(mixing.audioop, "mul", scaled)
    monkeypatch.setattr(mixing.audioop, "add", added)
    mixer = Mixer(1, pcm)
    wrapper = mixer.player_source()
    assert wrapper is not None
    mixer.switch_to(left, 0)
    mixer.switch_to(right, 0.04)
    prior = wrapper.read()
    inputs.clear()
    await asyncio.sleep(0)
    left.frames.append(storage.silence(FRAME_BYTES))
    right.frames.append(storage.silence(FRAME_BYTES))
    current = wrapper.read()
    assert observed == [capacity]
    inputs.clear()
    prior = None
    del current
    await asyncio.sleep(0)
    drain_pcm(left.frames)
    drain_pcm(right.frames)
    wrapper.cleanup()
    await asyncio.sleep(0)
    assert pcm.capacity.reserve_pcm(capacity - len(SILENCE) + 1) is None


def test_aliases_share_one_backing_but_finish_independently() -> None:
    storage = owner(4).storage(4)
    first = storage.copy(memoryview(b"abcd"))
    alias = first.borrow()
    assert alias.data is first.data
    assert alias.receipt._backing is first.receipt._backing  # pyright: ignore[reportPrivateUsage]
    assert storage.used == 4
    receipt = first.receipt
    del first
    receipt.release()
    receipt.release()  # a duplicated acknowledgement cannot release the other alias
    assert storage.used == 4
    assert alias.data == b"abcd"
    other = alias.receipt
    del alias
    other.release()
    other.release()
    assert storage.used == 0


@pytest.mark.parametrize("size", [7, 8])
def test_partial_carry_growth_counts_old_and_new_backings(size: int) -> None:
    storage = owner(size).storage(size)
    splitter = FrameSplitter(storage)
    assert list(splitter.split(b"abc")) == []
    if size == 7:
        with pytest.raises(PCMCapacityError):
            list(splitter.split(b"de"))
        assert storage.used == 3
        pending = splitter._pending  # pyright: ignore[reportPrivateUsage]
        assert pending is not None
        assert pending.data == b"abc"
    else:
        assert list(splitter.split(b"de")) == []
        assert storage.used == 5
    splitter.discard()
    assert storage.used == 0


def test_carry_completion_refuses_before_replacing_admitted_carry() -> None:
    storage = owner(FRAME_BYTES + 5).storage(FRAME_BYTES + 5)
    splitter = FrameSplitter(storage)
    assert list(splitter.split(b"prefix")) == []
    with pytest.raises(PCMCapacityError):
        list(splitter.split(bytes(FRAME_BYTES - 6)))
    assert storage.used == 6
    pending = splitter._pending  # pyright: ignore[reportPrivateUsage]
    assert pending is not None
    assert pending.data == b"prefix"


@pytest.mark.parametrize("end", ["capacity", "close", "error"])
def test_unconsumed_yield_remains_owned_after_iterator_exit(end: str) -> None:
    storage = owner(2 * FRAME_BYTES - 1).storage(2 * FRAME_BYTES - 1)
    splitter = FrameSplitter(storage)
    frames = splitter.split(bytes(2 * FRAME_BYTES))
    first = next(frames)
    if end == "capacity":
        with pytest.raises(PCMCapacityError):
            next(frames)
    elif end == "close":
        frames.close()
    else:
        with pytest.raises(ValueError, match="escaping"):
            frames.throw(ValueError("escaping"))
    assert first.data == bytes(FRAME_BYTES)
    assert storage.used == FRAME_BYTES
    receipt = first.receipt
    del first
    receipt.release()
    assert storage.used == (FRAME_BYTES if end == "error" else 0)


def test_refusal_precedes_copy_and_allocation_error_retains_debt() -> None:
    class Copy:
        called = False

        def __len__(self) -> int:
            return 4

        def tobytes(self) -> bytes:
            self.called = True
            raise MemoryError("owned allocation failed")

    data = Copy()
    storage = owner(3).storage(3)
    with pytest.raises(PCMCapacityError):
        storage.copy(cast(memoryview, data))
    assert not data.called
    enough = owner(4).storage(4)
    with pytest.raises(MemoryError):
        enough.copy(cast(memoryview, data))
    assert data.called
    assert enough.uncertain
    assert enough.used == 4


async def test_generated_refusal_preserves_frames_and_reports_local_failure() -> None:
    lyria = FakeLyria()
    audio = PCMFixtureLedger(2 * FRAME_BYTES - 1 + len(SILENCE))
    deck = Deck(lyria.connect, MusicPlan.from_request("ambient"), audio=audio)
    deck.start()
    await settle()
    lyria.sessions[0].send_audio(data=bytes(2 * FRAME_BYTES))
    await deck.wait_ended()
    assert deck.end_reason is EndReason.FAILED
    assert deck.detail == "PCM capacity exhausted"
    assert deck.failure is None  # local capacity is never provider quota
    assert [frame.data for frame in deck.frames] == [bytes(FRAME_BYTES)]
    assert deck.pcm.used == FRAME_BYTES
    deck.discard_pcm()
    assert deck.pcm.used == 0


async def test_explicit_drop_does_not_refund_a_popped_reader() -> None:
    deck = Deck(
        FakeLyria().connect,
        MusicPlan.from_request("ambient"),
        audio=PCMFixtureLedger(3 * FRAME_BYTES + len(SILENCE)),
    )
    with closing(deck._splitter.split(bytes(2 * FRAME_BYTES + 7))) as frames:  # pyright: ignore[reportPrivateUsage]
        deck.frames.extend(frames)
    borrowed = deck.frames.popleft()
    deck.state = State.ENDED
    deck.discard_pcm()
    assert deck.pcm.used == FRAME_BYTES
    receipt = borrowed.receipt
    del borrowed
    receipt.release()
    assert deck.pcm.used == 0


async def test_catalog_reserves_before_bounded_backend_read(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    held = rig.audio.pcm.capacity.reserve_pcm(PCM_LIMIT - len(SILENCE) - FRAME_BYTES + 1)
    assert held is not None
    with pytest.raises(PCMCapacityError):
        await rig.deck._output(rig.child)  # pyright: ignore[reportPrivateUsage]
    assert rig.child.offset == 0
    assert rig.deck.pcm.used == 0


@pytest.mark.parametrize("remaining", [2 * FRAME_BYTES - 4, 2 * FRAME_BYTES])
async def test_catalog_padding_prepaid_and_pcm_not_charged_twice(
    tmp_path: Path, remaining: int
) -> None:
    rig = Rig(tmp_path, seconds=1, output=bytes(FRAME_BYTES - 4), frames=100)
    held = rig.audio.pcm.capacity.reserve_pcm(PCM_LIMIT - len(SILENCE) - remaining)
    assert held is not None
    rig.deck.start()
    await settle_track(rig)
    assert rig.audio.reserved.pcm == PCM_LIMIT
    assert rig.audio.reserved.other > 0
    assert rig.audio.reserved.native > 0
    if remaining == 2 * FRAME_BYTES:
        assert rig.deck.completed
        assert [frame.data for frame in rig.deck.frames] == [bytes(FRAME_BYTES)]
    else:
        assert rig.deck.failure is TrackFailure.CAPACITY
        assert not rig.deck.completed
        assert not rig.deck.frames


class PublicSource(discord.AudioSource):
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail

    def read(self) -> bytes:
        return SILENCE

    def cleanup(self) -> None:
        if self.fail:
            raise RuntimeError("owned cleanup failure")

    def __del__(self) -> None:
        pass


async def test_public_cleanup_quiesces_before_thread_return_but_stop_after_do_not() -> None:
    pcm = owner(4 * FRAME_BYTES)
    source = pcm.wrap(PublicSource(), asyncio.get_running_loop(), 4 * FRAME_BYTES)
    assert source is not None
    held, finish, cleaned, leave = (threading.Event() for _ in range(4))

    def caller() -> None:
        frame = source.read()
        held.set()
        finish.wait()
        del frame
        source.cleanup()
        cleaned.set()
        leave.wait()  # public cleanup can release PCM while this thread remains alive

    thread = threading.Thread(target=caller)
    thread.start()
    try:
        assert held.wait(1)
        assert pcm.wrap(PublicSource(), asyncio.get_running_loop(), 4 * FRAME_BYTES) is None
        finish.set()
        assert cleaned.wait(1)
        await asyncio.sleep(0)
        successor = pcm.wrap(PublicSource(), asyncio.get_running_loop(), 4 * FRAME_BYTES)
        assert successor is not None
        assert thread.is_alive()
        successor.cleanup()
    finally:
        finish.set()
        leave.set()
        thread.join(1)
    assert not thread.is_alive()


async def test_failed_public_cleanup_stays_closed_after_loop_turn() -> None:
    pcm = owner(8 * FRAME_BYTES)
    source = pcm.wrap(PublicSource(fail=True), asyncio.get_running_loop(), 4 * FRAME_BYTES)
    assert source is not None
    with pytest.raises(RuntimeError, match="owned cleanup"):
        source.cleanup()
    await asyncio.sleep(0)
    assert pcm.wrap(PublicSource(), asyncio.get_running_loop(), 4 * FRAME_BYTES) is None


async def test_closed_loop_cannot_refund_or_open_player_admission() -> None:
    class ClosedLoop:
        def call_soon_threadsafe(self, *_args: object) -> None:
            raise RuntimeError("owned loop closed")

    pcm = owner(8 * FRAME_BYTES)
    source = pcm.wrap(
        PublicSource(), cast(asyncio.AbstractEventLoop, ClosedLoop()), 4 * FRAME_BYTES
    )
    assert source is not None
    with pytest.raises(RuntimeError, match="owned loop closed"):
        source.cleanup()
    await asyncio.sleep(0)
    assert pcm.wrap(PublicSource(), asyncio.get_running_loop(), 4 * FRAME_BYTES) is None


async def test_unknown_error_staging_debt_and_stale_epoch_cannot_refund_successor() -> None:
    pcm = owner(8 * FRAME_BYTES)
    source = pcm.wrap(PublicSource(), asyncio.get_running_loop(), 4 * FRAME_BYTES)
    assert source is not None
    source.uncertain = True
    epoch = source._epoch  # pyright: ignore[reportPrivateUsage]
    receipt = source._receipt  # pyright: ignore[reportPrivateUsage]
    source.cleanup()
    await asyncio.sleep(0)
    successor = pcm.wrap(PublicSource(), asyncio.get_running_loop(), 4 * FRAME_BYTES)
    assert successor is not None
    source.cleanup()  # a duplicate/late old hook cannot open the new epoch
    pcm.finished(epoch, receipt, False)  # even an already queued old acknowledgement is stale
    await asyncio.sleep(0)
    assert pcm.wrap(PublicSource(), asyncio.get_running_loop(), FRAME_BYTES) is None
    successor.cleanup()
    await asyncio.sleep(0)
    assert pcm.capacity.reserve_pcm(4 * FRAME_BYTES + 1) is None
    assert pcm.capacity.reserve_pcm(4 * FRAME_BYTES) is not None


@pytest.mark.parametrize("gain", [1.0, 0.5])
async def test_mixer_returns_separate_prepaid_copy_then_loop_releases_input_borrow(
    monkeypatch: pytest.MonkeyPatch, gain: float
) -> None:
    pcm = owner(6 * FRAME_BYTES)
    storage = pcm.storage(2 * FRAME_BYTES)
    backing = storage.copy(memoryview(bytes(FRAME_BYTES)))

    class Source:
        def __init__(self, frame: OwnedPCM) -> None:
            self.frames = deque([frame])

    mixer = Mixer(gain, pcm)
    wrapper = mixer.player_source()
    assert wrapper is not None
    mixer.switch_to(Source(backing), 0)
    calls: list[int] = []
    release = PCMReceipt.release

    def released(receipt: PCMReceipt) -> None:
        calls.append(threading.get_ident())
        release(receipt)

    monkeypatch.setattr(PCMReceipt, "release", released)
    output: list[bytes] = []
    thread = threading.Thread(target=lambda: output.append(wrapper.read()))
    thread.start()
    thread.join(1)
    assert not thread.is_alive()
    assert output == [backing.data]
    assert output[0] is not backing.data
    assert storage.used == FRAME_BYTES  # pop/read completion alone did not refund
    assert calls == []
    del backing
    await asyncio.sleep(0)
    assert storage.used == 0
    assert calls == [threading.get_ident()]
    assert output[0] == bytes(FRAME_BYTES)  # prior caller output remains covered by staging
    wrapper.cleanup()


async def test_mixer_error_keeps_borrow_and_renews_scratch_before_recovery() -> None:
    pcm = owner(10 * FRAME_BYTES)
    storage = pcm.storage(2 * FRAME_BYTES)

    class Source:
        def __init__(self) -> None:
            self.frames = deque([storage.copy(memoryview(b"x")), storage.silence(FRAME_BYTES)])

    mixer = Mixer(0.5, pcm)
    wrapper = mixer.player_source()
    assert wrapper is not None
    source = Source()
    mixer.switch_to(source, 0)
    assert wrapper.read() == SILENCE
    assert storage.used == FRAME_BYTES + 1
    assert len(source.frames) == 1
    assert wrapper.read() == SILENCE  # no further allocation before loop admission
    assert len(source.frames) == 1
    await asyncio.sleep(0)
    assert wrapper.read() == bytes(FRAME_BYTES)
    await asyncio.sleep(0)
    assert storage.used == 1  # the escaped bad frame is still uncertain debt
    assert wrapper.uncertain
    drain_pcm(source.frames)
    wrapper.cleanup()


async def test_crossfade_with_prior_caller_output_uses_prepaid_scratch_and_receipt_only_callbacks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pcm = owner(8 * FRAME_BYTES)
    storage = pcm.storage(4 * FRAME_BYTES)

    class Source:
        def __init__(self, sample: int) -> None:
            data = sample.to_bytes(2, "little", signed=True) * (FRAME_BYTES // 2)
            self.frames = deque(storage.copy(memoryview(data)) for _ in range(2))

    mixer = Mixer(1, pcm)
    wrapper = mixer.player_source()
    assert wrapper is not None
    mixer.switch_to(Source(4000), 0)
    mixer.switch_to(Source(8000), 0.04)
    loop = asyncio.get_running_loop()
    schedule = loop.call_soon_threadsafe
    acknowledgements: list[tuple[PCMReceipt, ...]] = []

    def scheduled(callback: object, receipts: tuple[PCMReceipt, ...]) -> object:
        assert callback == Mixer._released  # pyright: ignore[reportPrivateUsage]
        assert all(isinstance(receipt, PCMReceipt) for receipt in receipts)
        acknowledgements.append(receipts)
        return schedule(Mixer._released, receipts)  # pyright: ignore[reportPrivateUsage]

    with monkeypatch.context() as patch:
        patch.setattr(loop, "call_soon_threadsafe", scheduled)
        prior = wrapper.read()
        await asyncio.sleep(0)
        current = wrapper.read()
    assert len(prior) == len(current) == FRAME_BYTES
    assert prior is not current
    assert len(acknowledgements) == 2
    await asyncio.sleep(0)
    assert storage.used == 0
    assert pcm.capacity.reserve_pcm(4 * FRAME_BYTES) is not None
    assert pcm.capacity.reserve_pcm(1) is None  # four caller/mixing frames remain prepaid
    wrapper.cleanup()
