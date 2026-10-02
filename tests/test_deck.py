import logging

import pytest
from google.genai import types

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
    await deck.wait_ended()

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
    await deck.wait_ended()

    assert (deck.state, deck.end_reason) == (State.ENDED, EndReason.RETIRED)
    assert session.closed
    assert deck.buffered_seconds == pytest.approx(10)


async def test_retired_while_connecting_never_starts_playback(
    lyria: FakeLyria, clock: FakeClock
) -> None:
    deck = Deck(lyria.connect, MusicPlan.from_request("ambient"), clock=clock)

    deck.start()
    deck.retire()
    await deck.wait_ended()

    assert deck.end_reason is EndReason.RETIRED
    assert lyria.sessions[0].calls == []


async def test_fails_when_connecting_stalls(
    lyria: FakeLyria, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sobafm.deck, "CONNECT_TIMEOUT_S", 0.01)
    lyria.stall = True
    deck = Deck(lyria.connect, MusicPlan.from_request("ambient"), clock=clock)

    deck.start()
    await deck.wait_ended()

    assert (deck.end_reason, deck.detail) == (EndReason.FAILED, "TimeoutError: ")


async def test_records_when_lyria_closes_the_session(lyria: FakeLyria, clock: FakeClock) -> None:
    deck, session = await start_deck(lyria, clock)
    session.send_audio(4)
    session.close(1011)

    await deck.wait_ended()

    assert deck.end_reason is EndReason.CLOSED
    assert deck.detail == "code 1011: The service is currently unavailable."
    assert deck.buffered_seconds == pytest.approx(4)


async def test_records_a_failed_connection(lyria: FakeLyria, clock: FakeClock) -> None:
    lyria.failure = OSError("unreachable")
    deck = Deck(lyria.connect, MusicPlan.from_request("ambient"), clock=clock)

    deck.start()
    await deck.wait_ended()

    assert (deck.end_reason, deck.detail) == (EndReason.FAILED, "OSError: unreachable")


async def test_keeps_its_audio_when_receiving_fails(lyria: FakeLyria, clock: FakeClock) -> None:
    deck, session = await start_deck(lyria, clock)
    session.send_audio(4)
    session.fail(ConnectionResetError("reset by peer"))

    await deck.wait_ended()

    assert (deck.end_reason, deck.detail) == (
        EndReason.FAILED,
        "ConnectionResetError: reset by peer",
    )
    assert deck.buffered_seconds == pytest.approx(4)


async def test_fails_on_an_unexpected_audio_format(lyria: FakeLyria, clock: FakeClock) -> None:
    deck, session = await start_deck(lyria, clock)

    session.send_audio(2, mime_type="audio/l16;rate=24000;channels=1")
    await deck.wait_ended()

    assert deck.end_reason is EndReason.FAILED
    assert not deck.frames


async def test_records_a_prompt_refused_before_any_audio(
    lyria: FakeLyria, clock: FakeClock
) -> None:
    deck, session = await start_deck(lyria, clock)

    session.refuse("Not allowed.")
    await deck.wait_ended()

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
        await deck.wait_ended()

    assert caplog.messages == [
        f"Deck {deck.number} closed (code 1011: Deadline expired) after 20 s: "
        "20 s of audio at 1.00x real time"
    ]
