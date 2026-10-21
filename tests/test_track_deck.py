import asyncio
import gc
import logging
import weakref
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import cast

import pytest

import sobafm.track_deck as tracks
from sobafm.audio_budget import AudioBytes, AudioLedger
from sobafm.catalog import CatalogQuery, CatalogTrack
from sobafm.catalog_audio import TrackError, TrackFailure
from sobafm.catalog_interpreter import CatalogPlan
from sobafm.decoder_process import Boundary, InputCloser
from sobafm.native import SourceArchive, VerifiedNativeRuntime
from sobafm.pcm import BYTES_PER_SECOND, FRAME_BYTES
from sobafm.program import Program
from sobafm.source import Source, State
from sobafm.track_deck import DecoderSupervisor, EndReason, Envelope, Slots, TrackDeck
from tests.test_catalog import credit, discovered
from tests.test_decoder_check import Writer


class FakeBoundary:
    def __init__(self) -> None:
        self.prepared = self.bound = self.closed = False
        self.bind_error = False

    def prepare(self) -> None:
        self.prepared = True

    def bind(self, pid: int) -> None:
        if self.bind_error:
            raise TrackError(TrackFailure.CAPACITY)
        self.bound = True

    def terminate(self, pid: int) -> None:
        pass

    def kill(self) -> None:
        pass

    def empty(self) -> bool:
        return True

    def close(self) -> None:
        self.closed = True


class FakeInput:
    def __init__(self, complete: bool = True) -> None:
        self.complete = False
        self.succeed = complete
        self.closed = False
        self.blocks: list[bytes] = [b"owned-mp3-fixture"]
        self.gate: asyncio.Event | None = None

    async def chunks(self) -> AsyncGenerator[bytes]:
        if self.gate is not None:
            await self.gate.wait()
        for value in self.blocks:
            yield value
        self.complete = self.succeed

    async def aclose(self) -> None:
        self.closed = True


class FakeChild:
    def __init__(self, boundary: FakeBoundary, output: bytes) -> None:
        self.boundary = boundary
        self.output = output
        self.offset = 0
        self.bound = False
        self.writes: list[bytes] = []
        self.input_closed = asyncio.Event()
        self.reaped = False
        self.tree_empty = True
        self.pipes_closed = False
        self.exit_code = 0
        self.split = FRAME_BYTES
        self.block_output: asyncio.Event | None = None
        self.terminated = self.killed = 0
        self.eof_gate: asyncio.Event | None = None
        self.close_error = False

    def bind(self) -> None:
        self.boundary.bind(1)
        self.bound = True

    async def write(self, chunk: bytes) -> None:
        assert self.bound
        assert self.boundary.prepared
        self.writes.append(chunk)
        await asyncio.sleep(0)

    async def close_input(self) -> None:
        self.input_closed.set()

    async def read(self, pipe: int, size: int) -> bytes:
        if pipe == 2:
            return b""
        if self.block_output is not None:
            await self.block_output.wait()
        await asyncio.sleep(0)
        if self.offset == len(self.output) and self.eof_gate is not None:
            await self.eof_gate.wait()
        value = self.output[self.offset : self.offset + min(size, self.split)]
        self.offset += len(value)
        return value

    async def wait(self) -> int:
        await self.input_closed.wait()
        self.reaped = True
        return self.exit_code

    def terminate(self) -> None:
        self.terminated += 1
        self.input_closed.set()

    def kill(self) -> None:
        self.killed += 1
        self.input_closed.set()

    def empty(self) -> bool:
        return self.reaped and self.tree_empty

    def close_pipes(self) -> bool:
        if self.close_error:
            raise OSError("untrusted diagnostic")
        self.pipes_closed = True
        return True


class SharedCloseChild(FakeChild):
    def __init__(self, boundary: FakeBoundary, output: bytes) -> None:
        super().__init__(boundary, output)
        self.writer = Writer()
        self.writer.defer_close = True
        self.closer = InputCloser(cast(asyncio.StreamWriter, self.writer))

    async def close_input(self) -> None:
        self.input_closed.set()
        await self.closer.close()


class Rig:
    def __init__(
        self, tmp: Path, *, seconds: int = 2, output: bytes | None = None, frames: int = 400
    ) -> None:
        self.boundary = FakeBoundary()
        self.child = FakeChild(
            self.boundary, bytes(seconds * BYTES_PER_SECOND) if output is None else output
        )
        self.source = FakeInput()
        self.audio, self.slots = AudioLedger(), Slots()
        self.gate: asyncio.Event | None = None
        runtime = VerifiedNativeRuntime(
            executable=tmp / "not-executed.exe",
            manifest_sha256="a" * 64,
            target="windows-amd64",
            source_bundle=SourceArchive(name="owned.zip", sha256="b" * 64, size=1),
        )
        self.supervisor = DecoderSupervisor(
            runtime,
            lambda: self.boundary,
            envelope=Envelope(
                AudioBytes(
                    pcm=(frames + 8) * FRAME_BYTES, other=5 * 256 * 1024 + 4096, native=100_000
                ),
                frames,
            ),
            slots=self.slots,
            spawn=self.spawn,
        )
        self.track = discovered(duration=seconds).with_credits(credit())
        program = Program(
            CatalogPlan(title="Direction", query=CatalogQuery(tags=("jazz",))), "requester", None
        )
        self.deck = TrackDeck(
            self.track, program, self.audio, self.supervisor, input_factory=self.input
        )

    def input(self, track: CatalogTrack) -> FakeInput:
        return self.source

    async def spawn(self, runtime: VerifiedNativeRuntime, boundary: Boundary) -> FakeChild:
        boundary.prepare()
        if self.gate is not None:
            await self.gate.wait()
        return self.child


async def settle(rig: Rig) -> None:
    await asyncio.wait_for(rig.deck.wait_ended(), 5)
    await asyncio.gather(*tuple(rig.supervisor.cleanups))


async def test_verified_short_complete_is_ready_then_audibly_drained(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    source: Source = rig.deck
    source.start()
    await settle(rig)
    assert rig.deck.completed
    assert source.ready
    assert source.state is State.ENDED
    assert rig.deck.end_reason is EndReason.FINISHED
    assert rig.child.reaped
    assert rig.child.bound
    assert rig.child.pipes_closed
    assert rig.boundary.closed
    assert not rig.deck.drained
    assert rig.slots.count == 0
    assert rig.deck.credits == rig.track.credits
    before = rig.audio.reserved
    source.frames.clear()
    assert rig.deck.drained
    assert rig.audio.reserved == before
    assert before.total > 0  # PCM/mixer copy lease is not a process-slot refund.


@pytest.mark.parametrize("split", [1, 3, 4, 17, 3839, 3840])
async def test_arbitrary_pipe_splits_and_one_verified_final_padding(
    tmp_path: Path, split: int
) -> None:
    # Two frames cover every split and final sample without 192,000 scheduled
    # one-byte reads turning this framing check into a throughput deadline.
    raw = bytes(2 * FRAME_BYTES - 4)
    # Catalog durations are whole seconds; this strict short output is within
    # the existing one-second completion allowance, with both EOFs verified.
    rig = Rig(tmp_path, seconds=1, output=raw)
    rig.child.split = split
    rig.deck.start()
    await settle(rig)
    assert rig.deck.completed
    assert rig.source.complete
    assert all(rig.deck._eof.values())  # pyright: ignore[reportPrivateUsage]
    assert rig.deck.exit_status == 0
    assert len(rig.deck.frames) == 2
    assert all(len(frame) == FRAME_BYTES for frame in rig.deck.frames)
    assert b"".join(rig.deck.frames) == raw + bytes(4)


@pytest.mark.parametrize(
    "output",
    [b"", bytes(BYTES_PER_SECOND - 1), bytes(3 * BYTES_PER_SECOND)],
    ids=["empty", "unaligned", "duration-mismatch"],
)
async def test_empty_unaligned_or_zero_exit_wrong_duration_never_complete(
    tmp_path: Path, output: bytes
) -> None:
    rig = Rig(tmp_path, seconds=1, output=output)
    rig.deck.start()
    await settle(rig)
    assert rig.deck.end_reason is EndReason.FAILED
    assert not rig.deck.ready
    assert rig.child.exit_code == 0
    assert rig.slots.count == 0


async def test_incomplete_http_and_nonzero_child_are_failures(tmp_path: Path) -> None:
    for failure in ("http", "exit"):
        rig = Rig(tmp_path)
        if failure == "http":
            rig.source.succeed = False
        else:
            rig.child.exit_code = 1
        rig.deck.start()
        await settle(rig)
        assert not rig.deck.completed
        assert rig.deck.end_reason is EndReason.FAILED


async def test_binding_failure_prevents_first_untrusted_write(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    rig.boundary.bind_error = True
    rig.deck.start()
    await settle(rig)
    assert rig.deck.failure is TrackFailure.CAPACITY
    assert not rig.child.writes
    assert rig.child.reaped
    assert rig.slots.count == 0


async def test_unknown_envelope_and_existing_unknown_debt_never_spawn(tmp_path: Path) -> None:
    for kind in ("unknown-envelope", "unknown-debt"):
        rig = Rig(tmp_path)
        if kind == "unknown-envelope":
            rig.supervisor.envelope = Envelope()
        else:
            rig.audio.unbounded()
        rig.deck.start()
        await rig.deck.wait_ended()
        assert rig.deck.failure is TrackFailure.CAPACITY
        assert not rig.boundary.prepared
        assert not rig.child.writes
        assert rig.slots.count == 0


async def test_retirement_before_task_start_cannot_lose_reserved_slot(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    rig.deck.start()
    rig.deck.retire()
    rig.deck.retire()
    await settle(rig)
    assert rig.deck.end_reason is EndReason.RETIRED
    assert rig.slots.count == 0
    assert rig.audio.reserved.total == 0
    assert not rig.boundary.prepared


async def test_retired_unstarted_source_cannot_be_started_by_stale_owner(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    rig.deck.retire()
    rig.deck.start()
    await rig.deck.wait_ended()
    assert rig.deck.end_reason is EndReason.RETIRED
    assert rig.deck.state is State.ENDED
    assert not rig.boundary.prepared
    assert not rig.child.writes
    assert rig.slots.count == 0
    assert rig.audio.reserved.total == 0


async def test_pending_spawn_debt_survives_deadline_and_later_releases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tracks, "CLEANUP_SECONDS", 0.03)
    rig = Rig(tmp_path)
    rig.gate = asyncio.Event()
    rig.deck.start()
    await asyncio.sleep(0.001)
    rig.deck.retire()
    await rig.deck.wait_ended()
    assert rig.deck.cleanup_failed
    assert rig.slots.count == 1
    assert rig.supervisor.debt_count == 1
    assert rig.audio.reserved.total > 0
    rig.gate.set()
    await asyncio.wait_for(asyncio.gather(*tuple(rig.supervisor.cleanups)), 2)
    assert rig.child.reaped
    assert rig.slots.count == 0
    assert rig.supervisor.debt_count == 0
    assert rig.deck.failure is TrackFailure.CLEANUP  # later closure does not turn timeout into pass


async def test_waiter_cancellation_never_cancels_cleanup_owner(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    rig.source.gate = asyncio.Event()
    rig.deck.start()
    await asyncio.sleep(0.001)
    waiter = asyncio.create_task(rig.deck.wait_ended())
    await asyncio.sleep(0)
    waiter.cancel()
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    rig.deck.retire()
    await settle(rig)
    assert rig.child.reaped
    assert rig.slots.count == 0


async def test_retire_during_shared_stdin_close_keeps_owner_and_slot_until_callback(
    tmp_path: Path,
) -> None:
    rig = Rig(tmp_path)
    child = SharedCloseChild(rig.boundary, rig.child.output)
    rig.child = child
    rig.deck.start()
    await child.writer.waiting.wait()
    rig.deck.retire()
    await asyncio.sleep(0.01)
    assert not child.writer.close_waiter.cancelled()
    assert rig.slots.count == 1
    assert rig.supervisor.debt_count == 1
    child.writer.complete_close()
    await settle(rig)
    assert rig.deck.end_reason is EndReason.RETIRED
    assert not rig.deck.cleanup_failed
    assert child.writer.wait_calls == 1
    assert rig.slots.count == 0
    assert rig.supervisor.debt_count == 0


async def test_tree_debt_is_not_refunded_for_direct_child_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tracks, "CLEANUP_SECONDS", 0.03)
    rig = Rig(tmp_path)
    rig.child.tree_empty = False
    rig.deck.start()
    await rig.deck.wait_ended()
    assert rig.child.reaped
    assert rig.slots.count == 1
    assert rig.supervisor.debt_count == 1
    assert rig.deck.cleanup_failed
    assert not rig.deck.completed
    rig.child.tree_empty = True
    await asyncio.wait_for(asyncio.gather(*tuple(rig.supervisor.cleanups)), 2)
    assert rig.slots.count == 0
    assert rig.audio.reserved.total > 0


async def test_summary_shape_and_fixed_failure_text(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    rig = Rig(tmp_path, output=b"invalid")
    with caplog.at_level(logging.INFO):
        rig.deck.start()
        await settle(rig)
    assert any(
        "Deck " + str(rig.deck.number) + " failed" in record.message for record in caplog.records
    )
    assert not any(
        "storage.jamendo" in record.message or "requester" in record.message
        for record in caplog.records
    )


async def test_streaming_ready_does_not_imply_positive_completion(tmp_path: Path) -> None:
    rig = Rig(tmp_path, seconds=6)
    rig.child.eof_gate = asyncio.Event()
    rig.child.exit_code = 1
    rig.deck.start()
    for _ in range(1000):
        if rig.deck.ready:
            break
        await asyncio.sleep(0)
    assert rig.deck.ready
    assert rig.deck.buffered_seconds == 6
    assert not rig.deck.completed
    # A late nonzero exit after an already playable prefix is still failed.
    rig.child.eof_gate.set()
    await settle(rig)
    assert rig.deck.end_reason is EndReason.FAILED
    assert not rig.deck.ready
    assert len(rig.deck.frames) == 300


async def test_short_exact_full_buffer_can_verify_eof_without_consumption(tmp_path: Path) -> None:
    rig = Rig(tmp_path, seconds=1, output=bytes(2 * BYTES_PER_SECOND), frames=100)
    rig.deck.start()
    await settle(rig)
    assert rig.deck.completed
    assert rig.deck.ready
    assert len(rig.deck.frames) == 100


async def test_backpressure_preserves_cap_until_owner_consumes(tmp_path: Path) -> None:
    rig = Rig(tmp_path, seconds=8, frames=300)
    rig.deck.start()
    for _ in range(1000):
        if len(rig.deck.frames) == 300:
            break
        await asyncio.sleep(0)
    assert len(rig.deck.frames) == 300
    assert rig.deck.ready
    assert not rig.deck.completed
    rig.deck.frames.clear()
    await rig.deck.regulate()
    await settle(rig)
    assert rig.deck.completed
    assert len(rig.deck.frames) == 100


async def test_tail_only_read_reaches_eof_before_waiting_for_final_padding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw = b"a" * (6 * BYTES_PER_SECOND + 4)
    rig = Rig(tmp_path, seconds=6, output=raw, frames=300)
    padding = asyncio.Event()
    wait_for_room = rig.deck._room  # pyright: ignore[reportPrivateUsage]

    async def room() -> None:
        if rig.deck._eof[1]:  # pyright: ignore[reportPrivateUsage]
            padding.set()
        await wait_for_room()

    monkeypatch.setattr(rig.deck, "_room", room)
    rig.deck.start()
    await asyncio.wait_for(padding.wait(), 5)
    assert len(rig.deck.frames) == 300
    assert not rig.deck.completed
    assert rig.deck._raw_bytes == len(raw)  # pyright: ignore[reportPrivateUsage]
    rig.deck.frames.clear()
    await rig.deck.regulate()
    await settle(rig)
    assert rig.deck.completed
    assert list(rig.deck.frames) == [b"a" * 4 + bytes(FRAME_BYTES - 4)]


@pytest.mark.parametrize("split", [FRAME_BYTES - 1, FRAME_BYTES])
async def test_cancellation_while_waiting_for_room_closes_frame_iterator(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, split: int
) -> None:
    rig = Rig(tmp_path, seconds=8, frames=300)
    rig.child.split = split
    blocked = asyncio.Event()
    wait_for_room = rig.deck._room  # pyright: ignore[reportPrivateUsage]

    async def room() -> None:
        if len(rig.deck.frames) == 300:
            blocked.set()
        await wait_for_room()

    monkeypatch.setattr(rig.deck, "_room", room)
    rig.deck.start()
    await asyncio.wait_for(blocked.wait(), 5)
    assert len(rig.deck.frames) == 300
    splitter = rig.deck._splitter  # pyright: ignore[reportPrivateUsage]
    rig.deck.retire()
    await settle(rig)
    assert rig.deck.end_reason is EndReason.RETIRED
    assert len(rig.deck.frames) == 300
    # A suspended iterator would reject this: cancellation must close it, and
    # the unqueued frame/unread suffix must not contaminate later consumption.
    assert list(splitter.split(b"new" + bytes(FRAME_BYTES - 3))) == [
        b"new" + bytes(FRAME_BYTES - 3)
    ]


async def test_first_pcm_and_terminal_idle_are_bounded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(tracks, "PROGRESS_SECONDS", 0.01)
    rig = Rig(tmp_path)
    rig.child.block_output = asyncio.Event()
    rig.deck.start()
    await asyncio.sleep(0.07)
    rig.child.block_output.set()
    await rig.deck.wait_ended()
    assert rig.deck.failure is TrackFailure.DEADLINE
    await asyncio.gather(*tuple(rig.supervisor.cleanups))


async def test_absolute_deadline_includes_wait_for_final_padding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rig = Rig(tmp_path, seconds=6, output=bytes(6 * BYTES_PER_SECOND + 4), frames=300)
    padding = asyncio.Event()
    cancelled = asyncio.Event()
    room = rig.deck._room  # pyright: ignore[reportPrivateUsage] - observe the exact padding wait
    deadline = asyncio.timeout(None)
    delays: list[float] = []

    def controlled_timeout(delay: float) -> asyncio.Timeout:
        assert 0 < delay <= tracks.LIFETIME_SECONDS - tracks.CLEANUP_SECONDS
        assert not delays  # The producer must enter this one real timeout exactly once.
        delays.append(delay)
        return deadline

    async def wait_for_room() -> None:
        if len(rig.deck.frames) == 300:
            assert asyncio.current_task() is rig.deck._task  # pyright: ignore[reportPrivateUsage]
            padding.set()
        try:
            await room()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    monkeypatch.setattr(tracks.asyncio, "timeout", controlled_timeout)
    monkeypatch.setattr(rig.deck, "_room", wait_for_room)
    rig.deck.start()
    try:
        await asyncio.wait_for(padding.wait(), 1)
        assert len(delays) == 1
        assert deadline.when() is None
        assert not deadline.expired()
        assert rig.deck._raw_bytes == len(rig.child.output)  # pyright: ignore[reportPrivateUsage]
        assert rig.child.offset == len(rig.child.output)
        assert rig.source.complete
        assert all(rig.deck._eof.values())  # pyright: ignore[reportPrivateUsage]
        assert rig.deck.exit_status == 0
        assert all(task.done() for task in rig.deck._workers)  # pyright: ignore[reportPrivateUsage]
        assert rig.deck._backpressure  # pyright: ignore[reportPrivateUsage]
        assert not rig.deck._space.is_set()  # pyright: ignore[reportPrivateUsage]
        assert len(rig.deck.frames) == 300
        assert not rig.deck._verified  # pyright: ignore[reportPrivateUsage]
        assert not rig.deck.completed
        assert rig.deck.failure is None
        # Expire only after reaching final padding: fixture throughput must not
        # decide whether this test exercises output production or the padding wait.
        deadline.reschedule(asyncio.get_running_loop().time())
        await settle(rig)
        assert deadline.expired()
        assert cancelled.is_set()
        assert rig.deck.failure is TrackFailure.DEADLINE
        assert not rig.deck.completed
        assert len(rig.deck.frames) == 300  # no extra final frame past retained capacity
        assert not rig.deck.cleanup_failed
        assert rig.child.reaped
        assert rig.child.pipes_closed
        assert rig.source.closed
        assert rig.boundary.closed
        assert rig.slots.count == rig.supervisor.debt_count == 0
    finally:
        rig.deck.retire()
        await settle(rig)


async def test_pipe_closure_failure_is_sticky_and_retains_slot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(tracks, "CLEANUP_SECONDS", 0.02)
    rig = Rig(tmp_path)
    rig.child.close_error = True
    rig.deck.start()
    await rig.deck.wait_ended()
    assert rig.deck.failure is TrackFailure.CLEANUP
    assert rig.slots.count == 1
    assert rig.supervisor.debt_count == 1
    rig.child.close_error = False
    await asyncio.gather(*tuple(rig.supervisor.cleanups))
    assert rig.slots.count == 0
    assert rig.deck.end_reason is EndReason.FAILED


def test_process_and_per_server_slots_include_pending_debt() -> None:
    slots = Slots()
    one, two, three = AudioLedger(), AudioLedger(), AudioLedger()
    first = slots.acquire(one)
    assert first is not None
    assert slots.acquire(one) is not None
    assert slots.acquire(one) is None
    assert slots.acquire(two) is not None
    assert slots.acquire(two) is not None
    assert slots.acquire(three) is None
    slots.release(first)
    assert slots.acquire(three) is not None


def test_envelope_requires_transport_and_temporary_copy_reservations() -> None:
    with pytest.raises(ValueError, match="reservations"):
        Envelope(AudioBytes(pcm=3000 * FRAME_BYTES, other=1), 3000)
    for frames in (0, 3001, True):
        with pytest.raises(ValueError, match="capacity"):
            Envelope(buffer_frames=frames)


async def test_infeasible_preroll_capacity_is_rejected_before_allocation(tmp_path: Path) -> None:
    rig = Rig(tmp_path, seconds=60, frames=50)
    rig.deck.start()
    await rig.deck.wait_ended()
    assert rig.deck.failure is TrackFailure.CAPACITY
    assert not rig.boundary.prepared
    assert rig.slots.count == 0
    assert rig.audio.reserved.total == 0


@pytest.mark.parametrize("extra", [0, 4])
async def test_raw_stereo_sample_limit_counts_before_retaining_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    extra: int,
) -> None:
    monkeypatch.setattr(tracks, "MAX_SAMPLES", 48000)
    rig = Rig(tmp_path, seconds=1, output=bytes(BYTES_PER_SECOND + extra))
    rig.deck.start()
    await settle(rig)
    assert rig.deck.completed is (extra == 0)
    if extra:
        assert rig.deck.failure is TrackFailure.OUTPUT


async def test_final_padding_cannot_cross_sample_cap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(tracks, "MAX_SAMPLES", 959)
    rig = Rig(tmp_path, seconds=1, output=bytes(3836))
    rig.deck.start()
    await settle(rig)
    assert rig.deck.failure is TrackFailure.OUTPUT
    assert not rig.deck.frames


async def test_zero_encoded_body_and_later_retire_remain_failure(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    rig.source.blocks = []
    rig.deck.start()
    await settle(rig)
    rig.deck.retire()
    assert rig.deck.end_reason is EndReason.FAILED
    assert rig.deck.failure is TrackFailure.TRANSFER


async def test_completed_source_releases_without_cycle_collection(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    child, boundary, stream = rig.child, rig.boundary, rig.source

    async def spawn(runtime: VerifiedNativeRuntime, owner: Boundary) -> FakeChild:
        owner.prepare()
        return child

    # Keep the injected factories independent of the test Rig's source reference,
    # as the production module functions are; a bound Rig method creates its own cycle.
    rig.supervisor.spawn = spawn
    rig.supervisor.boundary = lambda: boundary

    def input_factory(track: CatalogTrack) -> FakeInput:
        return stream

    rig.deck._input_factory = input_factory  # pyright: ignore[reportPrivateUsage]
    rig.deck.start()
    await settle(rig)
    await asyncio.sleep(0)
    observed = weakref.ref(rig.deck)
    supervisor = rig.supervisor
    enabled = gc.isenabled()
    gc.disable()
    try:
        del rig
        assert observed() is None
        assert supervisor.debt_count == 0
    finally:
        if enabled:
            gc.enable()
