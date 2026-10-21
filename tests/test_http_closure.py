import asyncio
import gc
import weakref
from pathlib import Path
from typing import cast

import aiohttp
import pytest
from aiohttp.client_proto import ResponseHandler

import sobafm.catalog_audio as audio
import sobafm.track_deck as tracks
from sobafm.catalog import CatalogTrack
from sobafm.catalog_audio import AudioStream, TrackError, TrackFailure
from sobafm.track_deck import EndReason, Input, TrackDeck
from tests.http_lifecycle import HTTPRig, LifecycleSession
from tests.test_catalog import credit, discovered
from tests.test_catalog_audio import collect
from tests.test_track_deck import Rig


async def wait_session_close(http: LifecycleSession) -> None:
    async with asyncio.timeout(1):
        await http.closed_event.wait()


def close_owner(source: AudioStream) -> asyncio.Task[None]:
    source.close()
    owner = source._close_task  # pyright: ignore[reportPrivateUsage] - assert retained task identity
    assert owner is not None
    return owner


@pytest.mark.parametrize("body", [b"", b"already buffered MP3 fixture"])
async def test_immediate_eof_before_get_returns_retains_original_observation(body: bytes) -> None:
    rig = HTTPRig(body=body)
    assert await collect(rig.source) == body
    response = rig.http.response
    assert response is not None
    assert response.closed
    assert response.connection is None
    assert rig.http.returned.is_set()
    assert not rig.http.connector._acquired  # pyright: ignore[reportPrivateUsage]
    await response.wait_for_close()  # A writer join/release cannot observe connection_lost.
    await wait_session_close(rig.http)
    original = rig.http.protocol.closed
    assert original is not None
    assert not original.done()
    assert rig.http.transport.closing
    assert not rig.http.transport.lost
    assert not rig.source.closed
    owner = close_owner(rig.source)
    assert not owner.done()
    rig.http.transport.finish()
    await rig.source.aclose()
    assert original.result() is None
    assert rig.source.closed
    assert owner.done()
    assert close_owner(rig.source) is owner
    assert rig.http.close_calls == 1


@pytest.mark.parametrize("ending", ["abort", "eof", "headers"])
async def test_real_response_abort_normal_eof_and_header_rejection_keep_owner(ending: str) -> None:
    rig = HTTPRig(status=403 if ending == "headers" else 200, immediate_eof=False)
    if ending == "headers":
        with pytest.raises(TrackError, match=r"^http$"):
            await collect(rig.source)
    else:
        iterator = rig.source.chunks()
        reading = asyncio.create_task(anext(iterator))
        await rig.http.returned.wait()
        if ending == "abort":
            reading.cancel()
            with pytest.raises(asyncio.CancelledError):
                await reading
        else:
            rig.http.protocol.data_received(rig.http.body)
            assert await reading == rig.http.body
            with pytest.raises(StopAsyncIteration):
                await anext(iterator)
            assert rig.source.complete
        await iterator.aclose()
    await wait_session_close(rig.http)
    assert rig.http.response is not None
    assert rig.http.response.closed
    assert not rig.source.closed
    rig.http.transport.finish()
    await rig.source.aclose()
    assert rig.source.closed


async def test_start_failure_keeps_capture_before_input_response_assignment() -> None:
    rig = HTTPRig()
    rig.http.start_error = True
    with pytest.raises(TrackError, match=r"^transfer$"):
        await collect(rig.source)
    assert not rig.http.returned.is_set()
    await wait_session_close(rig.http)
    observation = rig.http.protocol.closed
    assert observation is not None
    assert not observation.done()
    assert not rig.source.closed
    rig.http.transport.finish()
    await rig.source.aclose()
    assert rig.source.closed


async def test_pre_hook_failure_stays_unknown_after_logical_session_close() -> None:
    rig = HTTPRig()
    rig.http.before_start_error = True
    with pytest.raises(TrackError, match=r"^transfer$"):
        await collect(rig.source)
    rig.http.transport.finish()
    owner = close_owner(rig.source)
    for _ in range(2):
        with pytest.raises(TrackError, match=r"^cleanup$"):
            await rig.source.aclose()
        assert close_owner(rig.source) is owner
        assert rig.http.closed
        assert not rig.source.closed
    assert rig.http.close_calls == 1


async def test_start_failure_joins_original_response_writer_after_transport_closes() -> None:
    rig = HTTPRig()
    rig.http.start_error = rig.http.hold_writer = True
    try:
        with pytest.raises(TrackError, match=r"^transfer$"):
            await collect(rig.source)
        await rig.http.writer_cancelled.wait()
        rig.http.transport.finish()
        await wait_session_close(rig.http)
        owner = close_owner(rig.source)
        assert not owner.done()
        assert not rig.source.closed
        assert rig.http.writer is not None
        assert not rig.http.writer.done()
        rig.http.writer_release.set()
        await rig.source.aclose()
        assert rig.source.closed
        assert rig.http.writer.done()
        assert rig.http.close_calls == 1
    finally:
        rig.http.writer_release.set()
        rig.http.transport.finish()
        await rig.source.aclose()


async def test_cancelled_borrowers_cannot_poison_one_owner_or_original_future() -> None:
    rig = HTTPRig()
    await collect(rig.source)
    owner = close_owner(rig.source)
    observation = rig.http.protocol.closed
    assert observation is not None
    borrowers = [asyncio.create_task(rig.source.aclose()) for _ in range(3)]
    await wait_session_close(rig.http)
    for borrower in borrowers:
        borrower.cancel()
        borrower.cancel()
        with pytest.raises(asyncio.CancelledError):
            await borrower
        assert not owner.done()
        assert not observation.done()
        assert close_owner(rig.source) is owner
    rig.http.transport.finish()
    await rig.source.aclose()
    assert rig.source.closed
    assert rig.http.close_calls == 1


async def test_fixture_negative_control_unshielded_wait_cancels_shared_protocol_future() -> None:
    rig = HTTPRig()
    await collect(rig.source)
    observation = rig.http.protocol.closed
    assert observation is not None

    async def unshielded() -> None:
        await observation

    borrower = asyncio.create_task(unshielded())
    await asyncio.sleep(0)
    borrower.cancel()
    with pytest.raises(asyncio.CancelledError):
        await borrower
    assert observation.cancelled()
    rig.http.transport.finish()
    with pytest.raises(TrackError, match=r"^cleanup$"):
        await rig.source.aclose()
    assert not rig.source.closed


async def test_cancelled_original_owner_is_never_replaced_or_reported_closed() -> None:
    rig = HTTPRig()
    await collect(rig.source)
    owner = close_owner(rig.source)
    await wait_session_close(rig.http)
    owner.cancel()
    with pytest.raises(asyncio.CancelledError):
        await owner
    observation = rig.http.protocol.closed
    assert observation is not None
    assert not observation.done()
    rig.http.transport.finish()
    with pytest.raises(asyncio.CancelledError):
        await rig.source.aclose()
    assert close_owner(rig.source) is owner
    assert not rig.source.closed
    assert rig.http.close_calls == 1


async def test_cancelled_start_wait_retains_observation_before_get_returns() -> None:
    rig = HTTPRig()
    rig.http.hold_headers = True
    reading = asyncio.create_task(collect(rig.source))
    await rig.http.starting.wait()
    observation = rig.http.protocol.closed
    assert observation is not None
    reading.cancel()
    with pytest.raises(asyncio.CancelledError):
        await reading
    await wait_session_close(rig.http)
    assert not rig.http.returned.is_set()
    assert not observation.done()
    assert not rig.source.closed
    rig.http.transport.finish()
    await rig.source.aclose()
    assert rig.source.closed


@pytest.mark.parametrize("failure", ["cancelled", "failed", "result", "session", "response"])
async def test_failed_closure_never_retries_logical_closed_flags(
    failure: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    rig = HTTPRig(immediate_eof=failure != "response")
    if failure == "response":
        iterator = rig.source.chunks()
        reading = asyncio.create_task(anext(iterator))
        await rig.http.returned.wait()
        response = rig.http.response
        assert response is not None
        original_close = response.close

        def fail_close() -> None:
            original_close()
            raise OSError("private response diagnostic")

        monkeypatch.setattr(response, "close", fail_close)
        reading.cancel()
        with pytest.raises(asyncio.CancelledError):
            await reading
        await iterator.aclose()
    else:
        await collect(rig.source)
    observation = rig.http.protocol.closed
    assert observation is not None
    if failure == "cancelled":
        observation.cancel()
    elif failure == "result":
        cast(asyncio.Future[object], observation).set_result("malformed closure result")
    elif failure == "session":
        rig.http.close_error = True
    rig.http.transport.finish(
        OSError("private transport diagnostic") if failure == "failed" else None
    )
    owner = close_owner(rig.source)
    for _ in range(2):
        with pytest.raises(TrackError, match=r"^cleanup$") as caught:
            await rig.source.aclose()
        assert caught.value.__context__ is None
        assert caught.value.__cause__ is None
        assert not rig.source.closed
        assert rig.http.closed
        assert close_owner(rig.source) is owner
    assert rig.http.close_calls == 1
    assert "private" not in caplog.text


@pytest.mark.parametrize("shape", [None, False, "future", "closed_before_hook", "version"])
async def test_unknown_locked_signal_shapes_fail_closed(
    shape: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = HTTPRig()
    if shape == "version":
        monkeypatch.setattr(aiohttp, "__version__", "3.14.5")
    elif shape == "closed_before_hook":
        # A prior connection_lost with no captured future returns None forever.
        rig.http.transport.finish()
    else:
        monkeypatch.setattr(ResponseHandler, "closed", property(lambda _: shape))
    if shape == "closed_before_hook":
        with pytest.raises(TrackError, match=r"^transfer$"):
            await collect(rig.source)
    else:
        await collect(rig.source)
    rig.http.transport.finish()
    with pytest.raises(TrackError, match=r"^cleanup$"):
        await rig.source.aclose()
    assert rig.http.closed
    assert not rig.source.closed


async def test_additional_original_observation_is_awaited_but_never_accepted() -> None:
    rig = HTTPRig()
    await collect(rig.source)
    extra = LifecycleSession(rig.http.response_class)
    await extra.get("https://fixture.invalid/owned.mp3")
    await extra.close()
    rig.http.transport.finish()
    await wait_session_close(rig.http)
    owner = close_owner(rig.source)
    await asyncio.sleep(0)
    assert not owner.done()
    assert not rig.source.closed
    extra.transport.finish()
    with pytest.raises(TrackError, match=r"^cleanup$"):
        await rig.source.aclose()
    assert not rig.source.closed


async def test_default_session_keeps_credential_and_transport_settings() -> None:
    session: aiohttp.ClientSession | None = None

    def factory(response_class: type[aiohttp.ClientResponse]) -> aiohttp.ClientSession:
        nonlocal session
        session = audio.audio_session(response_class)
        return session

    source = AudioStream(discovered().with_credits(credit()), session_factory=factory)
    assert session is not None
    assert not session.trust_env
    assert not session.auto_decompress
    assert session.auth is None
    assert isinstance(session.cookie_jar, aiohttp.DummyCookieJar)
    assert session._read_bufsize == 16384  # pyright: ignore[reportPrivateUsage]
    assert session._response_class is not aiohttp.ClientResponse  # pyright: ignore[reportPrivateUsage]
    with pytest.raises(TrackError, match=r"^cleanup$"):
        await source.aclose()  # No request/observation is deliberately unknown.
    assert session.closed
    assert not source.closed
    with pytest.raises(TrackError, match=r"^transfer$"):
        await collect(source)


async def test_successful_close_does_not_retain_input_in_response_class_cycle() -> None:
    rig = HTTPRig()
    source = rig.source
    reference = weakref.ref(source)
    await collect(source)
    rig.http.transport.finish()
    await source.aclose()
    del source
    del rig.source
    gc.disable()
    try:
        await asyncio.sleep(0)  # release the completed task's scheduled callback
        assert reference() is None
    finally:
        gc.enable()


async def test_original_three_second_deadline_and_http_debt_survive_replacement(
    tmp_path: Path,
) -> None:
    assert tracks.CLEANUP_SECONDS == 3.0
    rig = Rig(tmp_path)
    http_rig = HTTPRig()

    def http_input(track: CatalogTrack) -> Input:
        return http_rig.source

    rig.deck = TrackDeck(
        rig.track, rig.deck.program, rig.audio, rig.supervisor, input_factory=http_input
    )
    other_slot = rig.slots.acquire(rig.audio)
    assert other_slot is not None
    rig.deck.start()
    await wait_session_close(http_rig.http)
    held_reservation = rig.audio.reserved
    assert held_reservation.total > 0
    first_waiter = asyncio.create_task(rig.deck.wait_ended())
    await asyncio.sleep(0)
    first_waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first_waiter
    rig.deck.retire()
    rig.deck.retire()
    await asyncio.wait_for(rig.deck.wait_ended(), 4)
    assert rig.deck.cleanup_failed
    assert rig.deck.failure is TrackFailure.CLEANUP
    assert rig.deck.end_reason is EndReason.FAILED
    assert not rig.deck.completed
    assert rig.child.reaped
    assert rig.child.pipes_closed
    assert rig.slots.count == 2
    assert rig.supervisor.debt_count == 1
    assert not http_rig.source.closed
    assert rig.audio.reserved == held_reservation
    replacement = TrackDeck(
        rig.track, rig.deck.program, rig.audio, rig.supervisor, input_factory=http_input
    )
    replacement.start()
    await replacement.wait_ended()
    assert replacement.failure is TrackFailure.CAPACITY
    assert rig.slots.count == 2
    assert rig.supervisor.debt_count == 1
    http_rig.http.transport.finish()
    await asyncio.wait_for(asyncio.gather(*tuple(rig.supervisor.cleanups)), 1)
    assert http_rig.source.closed
    assert rig.slots.count == 1
    assert rig.supervisor.debt_count == 0
    assert rig.deck.cleanup_failed
    assert rig.deck.failure is TrackFailure.CLEANUP
    assert rig.deck.end_reason is EndReason.FAILED
    assert not rig.deck.completed
    assert rig.audio.reserved == held_reservation  # Closure cannot refund audio storage.
    rig.slots.release(other_slot)
