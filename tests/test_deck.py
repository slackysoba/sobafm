import asyncio
import logging

import pytest
from google.genai import types
from websockets.exceptions import ConnectionClosedError
from websockets.frames import Close

import sobafm.deck
from sobafm.deck import PAUSE_AT_S, RESUME_BELOW_S, Deck, EndReason, State
from sobafm.pcm import FRAME_BYTES, FRAME_SECONDS
from sobafm.plan import MusicPlan
from tests.doubles import FakeClock, FakeLyria, FakeSession, settle


async def start_deck(lyria: FakeLyria, clock: FakeClock) -> tuple[Deck, FakeSession]:
    deck = Deck(lyria.connect, MusicPlan.from_request("rainy lo-fi"), clock=clock)
    deck.start()
    await settle()
    return deck, lyria.sessions[-1]


async def ended(deck: Deck) -> None:
    async with asyncio.timeout(1):
        await deck.wait_ended()


def retire_during_close(lyria: FakeLyria, deck: Deck) -> None:
    """Retire `deck` while the SDK closes its session, as the station's next tick can."""

    async def closing() -> None:
        deck.retire()
        await asyncio.sleep(0)  # where a cancel would land

    lyria.closing = closing


def drain(deck: Deck, seconds: float) -> None:
    for _ in range(round(seconds / FRAME_SECONDS)):
        deck.frames.popleft()


async def test_sends_prompts_and_full_config_before_playing(
    lyria: FakeLyria, clock: FakeClock
) -> None:
    deck, session = await start_deck(lyria, clock)

    assert session.calls == ["prompts", "config", "play"]
    assert session.prompts == [types.WeightedPrompt(text="rainy lo-fi", weight=1.0)]
    assert session.config == deck.plan.to_config()
    assert deck.state is State.GENERATING


async def test_buffers_audio_as_whole_frames(lyria: FakeLyria, clock: FakeClock) -> None:
    deck, session = await start_deck(lyria, clock)
    audio = bytes(i % 251 for i in range(FRAME_BYTES * 3 + 100))

    session.send_audio(data=audio[: FRAME_BYTES * 2 + 100])
    session.send_audio(data=audio[FRAME_BYTES * 2 + 100 :])
    await settle()

    assert [len(frame) for frame in deck.frames] == [FRAME_BYTES] * 3
    assert b"".join(deck.frames) == audio[: FRAME_BYTES * 3]


async def test_is_ready_once_the_preroll_is_buffered(lyria: FakeLyria, clock: FakeClock) -> None:
    deck, session = await start_deck(lyria, clock)

    session.send_audio(4)
    await settle()
    assert not deck.ready

    session.send_audio(2)
    await settle()
    assert deck.ready
    assert deck.buffered_seconds == pytest.approx(6)


async def test_pauses_when_full_and_resumes_as_the_buffer_drains(
    lyria: FakeLyria, clock: FakeClock
) -> None:
    deck, session = await start_deck(lyria, clock)
    session.send_audio(PAUSE_AT_S)
    await settle()

    await deck.regulate()
    assert deck.paused
    assert session.calls[-1] == "pause"

    drain(deck, PAUSE_AT_S - RESUME_BELOW_S)
    await deck.regulate()
    assert deck.paused

    drain(deck, 1)
    await deck.regulate()
    assert not deck.paused
    assert session.calls[-1] == "play"


async def test_keeps_reading_while_paused(lyria: FakeLyria, clock: FakeClock) -> None:
    deck, session = await start_deck(lyria, clock)
    session.send_audio(PAUSE_AT_S)
    await settle()
    await deck.regulate()

    session.close(1011)
    await ended(deck)

    assert deck.end_reason is EndReason.CLOSED


async def test_regulating_a_closing_session_never_raises(
    lyria: FakeLyria, clock: FakeClock
) -> None:
    deck, session = await start_deck(lyria, clock)
    session.send_audio(PAUSE_AT_S)
    await settle()
    session.ending = True  # Lyria has started closing the socket

    await deck.regulate()

    assert not deck.paused


async def test_retiring_ends_the_session_and_keeps_its_audio(
    lyria: FakeLyria, clock: FakeClock
) -> None:
    deck, session = await start_deck(lyria, clock)
    session.send_audio(10)
    await settle()

    deck.retire()
    await ended(deck)
    session.send_audio(2)  # nothing receives once the session has ended
    await settle()

    assert (deck.state, deck.end_reason) == (State.ENDED, EndReason.RETIRED)
    assert session.closed
    assert deck.buffered_seconds == pytest.approx(10)


async def test_retired_while_connecting_never_starts_playback(
    lyria: FakeLyria, clock: FakeClock
) -> None:
    deck = Deck(lyria.connect, MusicPlan.from_request("ambient"), clock=clock)

    deck.start()
    deck.retire()  # before its task has run at all
    await ended(deck)

    assert (deck.state, deck.end_reason) == (State.ENDED, EndReason.RETIRED)
    assert lyria.sessions == []


async def test_fails_when_connecting_stalls(
    lyria: FakeLyria, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sobafm.deck, "CONNECT_TIMEOUT_S", 0.01)
    lyria.stall = True
    deck = Deck(lyria.connect, MusicPlan.from_request("ambient"), clock=clock)

    deck.start()
    await ended(deck)

    assert (deck.end_reason, deck.detail) == (EndReason.FAILED, "TimeoutError")


async def test_records_when_lyria_closes_the_session(lyria: FakeLyria, clock: FakeClock) -> None:
    deck, session = await start_deck(lyria, clock)
    session.send_audio(4)
    session.close(1011)

    await ended(deck)

    assert deck.end_reason is EndReason.CLOSED
    assert deck.detail == "code 1011: The service is currently unavailable."
    assert deck.buffered_seconds == pytest.approx(4)


async def test_records_a_failed_connection(lyria: FakeLyria, clock: FakeClock) -> None:
    lyria.failure = OSError("unreachable")
    deck = Deck(lyria.connect, MusicPlan.from_request("ambient"), clock=clock)

    deck.start()
    await ended(deck)

    assert (deck.end_reason, deck.detail) == (EndReason.FAILED, "OSError: unreachable")


async def test_keeps_its_audio_when_receiving_fails(
    lyria: FakeLyria, clock: FakeClock, caplog: pytest.LogCaptureFixture
) -> None:
    deck, session = await start_deck(lyria, clock)
    session.send_audio(4)
    session.fail(ConnectionResetError("reset by peer"))

    with caplog.at_level(logging.INFO, logger="sobafm.deck"):
        await ended(deck)

    assert (deck.end_reason, deck.detail) == (
        EndReason.FAILED,
        "ConnectionResetError: reset by peer",
    )
    assert deck.buffered_seconds == pytest.approx(4)
    assert "rainy" not in caplog.text  # the title can repeat the request


async def test_accepts_its_format_in_any_case_and_order(lyria: FakeLyria, clock: FakeClock) -> None:
    deck, session = await start_deck(lyria, clock)

    session.send_audio(2, mime_type="AUDIO/L16; channels=2; rate=48000")
    await settle()

    assert deck.state is State.GENERATING
    assert deck.buffered_seconds == pytest.approx(2)


async def test_fails_on_an_unexpected_audio_format(lyria: FakeLyria, clock: FakeClock) -> None:
    deck, session = await start_deck(lyria, clock)

    session.send_audio(2, mime_type="audio/l16;rate=24000;channels=1")
    await ended(deck)

    assert deck.end_reason is EndReason.FAILED
    assert not deck.frames


async def test_records_a_prompt_refused_before_any_audio(
    lyria: FakeLyria, clock: FakeClock
) -> None:
    deck, session = await start_deck(lyria, clock)

    session.refuse("Not allowed.")
    await ended(deck)

    assert (deck.end_reason, deck.detail) == (EndReason.FILTERED, "Not allowed.")
    assert session.closed


async def test_ignores_a_refusal_after_audio_started(lyria: FakeLyria, clock: FakeClock) -> None:
    deck, session = await start_deck(lyria, clock)
    session.send_audio(2)
    session.refuse()
    session.send_audio(2)
    await settle()

    assert deck.state is State.GENERATING
    assert deck.buffered_seconds == pytest.approx(4)


async def test_rate_excludes_paused_time(lyria: FakeLyria, clock: FakeClock) -> None:
    deck, session = await start_deck(lyria, clock)
    session.send_audio(PAUSE_AT_S)
    await settle()
    clock.now = 60.0
    assert deck.rate == pytest.approx(1.0)

    await deck.regulate()
    clock.now = 300.0
    assert deck.rate == pytest.approx(1.0)

    drain(deck, PAUSE_AT_S)
    await deck.regulate()
    clock.now = 330.0
    session.send_audio(15)
    await settle()

    assert deck.rate == pytest.approx(75 / 90)
    assert deck.age == pytest.approx(330.0)


async def test_logs_one_summary_without_the_title(
    lyria: FakeLyria, clock: FakeClock, caplog: pytest.LogCaptureFixture
) -> None:
    deck, session = await start_deck(lyria, clock)
    session.send_audio(20)
    await settle()
    clock.now = 20.0
    session.close(1011, "Deadline expired")

    with caplog.at_level(logging.INFO, logger="sobafm.deck"):
        await ended(deck)

    assert caplog.messages == [
        f"Deck {deck.number} closed (code 1011: Deadline expired) after 20 s: "
        "20 s of audio at 1.00x real time"
    ]


async def test_keeps_generating_past_the_connect_timeout(
    lyria: FakeLyria, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sobafm.deck, "CONNECT_TIMEOUT_S", 0.01)
    deck, session = await start_deck(lyria, clock)

    await asyncio.sleep(0.05)
    session.send_audio(1)
    await settle()

    assert deck.state is State.GENERATING  # the timeout covers only the setup


async def test_retiring_a_stalled_connection_stops_it_at_once(
    lyria: FakeLyria, clock: FakeClock
) -> None:
    lyria.stall = True
    deck = Deck(lyria.connect, MusicPlan.from_request("ambient"), clock=clock)
    deck.start()
    await settle()

    deck.retire()
    await ended(deck)

    assert (deck.state, deck.end_reason) == (State.ENDED, EndReason.RETIRED)
    assert lyria.sessions == []


async def test_retiring_again_lets_a_stalled_setup_close(
    lyria: FakeLyria, clock: FakeClock
) -> None:
    lyria.stall_sends = True  # setup stalls once the session is open
    deck = Deck(lyria.connect, MusicPlan.from_request("ambient"), clock=clock)
    deck.start()
    await settle()
    retire_during_close(lyria, deck)

    deck.retire()
    await ended(deck)

    assert deck.end_reason is EndReason.RETIRED
    assert lyria.sessions[0].closed


async def test_retiring_after_the_connect_timeout_lets_the_session_close(
    lyria: FakeLyria, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sobafm.deck, "CONNECT_TIMEOUT_S", 0.01)
    lyria.stall_sends = True  # setup stalls once the session is open
    deck = Deck(lyria.connect, MusicPlan.from_request("ambient"), clock=clock)
    retire_during_close(lyria, deck)  # while the timeout's cancellation is still pending

    deck.start()
    await ended(deck)

    assert (deck.end_reason, deck.detail) == (EndReason.FAILED, "TimeoutError")
    assert lyria.sessions[0].closed


async def test_regulating_returns_while_a_send_stalls(
    lyria: FakeLyria, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sobafm.deck, "REGULATE_TIMEOUT_S", 0.01)
    deck, session = await start_deck(lyria, clock)
    session.send_audio(PAUSE_AT_S)
    await settle()
    session.stall = True  # the socket's close hangs

    async with asyncio.timeout(1):
        await deck.regulate()

    assert not deck.paused


async def test_sends_nothing_while_its_session_closes(lyria: FakeLyria, clock: FakeClock) -> None:
    deck, session = await start_deck(lyria, clock)
    session.send_audio(PAUSE_AT_S)
    await settle()
    lyria.closing = deck.regulate  # the station's next tick, while the SDK closes the socket

    deck.retire()
    await ended(deck)

    assert "pause" not in session.calls


async def test_names_its_task_by_number_only(lyria: FakeLyria, clock: FakeClock) -> None:
    deck, _ = await start_deck(lyria, clock)

    names = [task.get_name() for task in asyncio.all_tasks()]

    assert f"deck {deck.number}" in names
    assert not any("rainy" in name for name in names)  # the title can repeat the request


async def test_records_a_close_code(lyria: FakeLyria, clock: FakeClock) -> None:
    deck, session = await start_deck(lyria, clock)

    session.close(1008, "Project denied")
    await ended(deck)

    assert (deck.end_reason, deck.close_code) == (EndReason.CLOSED, 1008)


async def test_records_a_close_code_during_setup(lyria: FakeLyria, clock: FakeClock) -> None:
    lyria.failure = ConnectionClosedError(Close(1008, "Project denied"), None)  # as the SDK raises
    deck = Deck(lyria.connect, MusicPlan.from_request("ambient"), clock=clock)

    deck.start()
    await ended(deck)

    assert (deck.end_reason, deck.close_code) == (EndReason.FAILED, 1008)


async def test_retiring_while_lyria_closes_keeps_the_close_code(
    lyria: FakeLyria, clock: FakeClock
) -> None:
    deck, session = await start_deck(lyria, clock)
    retire_during_close(lyria, deck)

    session.close(1008, "Project denied")
    await ended(deck)

    assert (deck.end_reason, deck.close_code) == (EndReason.CLOSED, 1008)
    assert session.closed
