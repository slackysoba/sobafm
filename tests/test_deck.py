import asyncio
import contextlib
import logging
from collections.abc import AsyncGenerator
from types import SimpleNamespace
from typing import Any, cast

import pytest
from google.genai import live_music, types
from websockets.exceptions import ConnectionClosed, ConnectionClosedError, ConnectionClosedOK
from websockets.frames import Close

import sobafm.deck
from sobafm.deck import (
    PAUSE_AT_S,
    PREROLL_S,
    RESUME_BELOW_S,
    Deck,
    EndReason,
    MusicSession,
    State,
)
from sobafm.failures import Failure
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
    assert deck.detail == "code 1011"  # the reason is free text
    assert deck.failure is Failure.UNAVAILABLE
    assert deck.buffered_seconds == pytest.approx(4)


async def test_records_a_failed_connection(lyria: FakeLyria, clock: FakeClock) -> None:
    lyria.failure = OSError("unreachable")
    deck = Deck(lyria.connect, MusicPlan.from_request("ambient"), clock=clock)

    deck.start()
    await ended(deck)

    assert (deck.end_reason, deck.detail) == (EndReason.FAILED, "OSError: unreachable")
    assert deck.failure is Failure.UNAVAILABLE


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
    # An unexpected error keeps its traceback.
    assert any(record.exc_info for record in caplog.records if record.levelno == logging.WARNING)


async def test_accepts_its_format_in_any_case_and_order(lyria: FakeLyria, clock: FakeClock) -> None:
    deck, session = await start_deck(lyria, clock)

    session.send_audio(2, mime_type="AUDIO/L16; channels=2; rate=48000")
    await settle()

    assert deck.state is State.GENERATING
    assert deck.buffered_seconds == pytest.approx(2)


async def test_fails_on_an_unexpected_audio_format(
    lyria: FakeLyria, clock: FakeClock, caplog: pytest.LogCaptureFixture
) -> None:
    deck, session = await start_deck(lyria, clock)

    with caplog.at_level(logging.DEBUG):
        session.send_audio(2, mime_type="audio/l16;rate=24000;key=AIzaFakeKey")
        await ended(deck)

    assert (deck.end_reason, deck.detail) == (
        EndReason.FAILED,
        "LyriaMessageError: unexpected audio format",
    )
    assert not deck.frames
    assert deck.failure is None  # not one a requester can act on
    assert "AIzaFakeKey" not in caplog.text  # the format Lyria sent is free text


class EndingSession(FakeSession):
    """A session whose stream ends without an error."""

    async def receive(self) -> AsyncGenerator[types.LiveMusicServerMessage]:
        for message in ():
            yield message


async def test_records_a_stream_that_ends_as_closed(clock: FakeClock) -> None:
    @contextlib.asynccontextmanager
    async def connect() -> AsyncGenerator[MusicSession]:
        yield EndingSession()

    deck = Deck(connect, MusicPlan.from_request("ambient"), clock=clock)

    deck.start()
    await ended(deck)

    assert (deck.end_reason, deck.detail) == (EndReason.CLOSED, None)


class OneFrameSocket:
    """A WebSocket that takes what a session sends, answers with one raw frame, then waits."""

    def __init__(self, frame: bytes) -> None:
        self.frames = [frame]

    async def send(self, message: str) -> None:
        pass

    async def recv(self, decode: bool | None = None) -> bytes:
        if not self.frames:
            await asyncio.Event().wait()
        return self.frames.pop()


@pytest.mark.parametrize(
    "frame",
    [b"not JSON: api_key:AIzaFakeKey", b'{"serverContent": {"audioChunks": "AIzaFakeKey"}}'],
    ids=["unparseable", "invalid"],
)
async def test_never_logs_a_frame_it_cannot_read(
    clock: FakeClock, caplog: pytest.LogCaptureFixture, frame: bytes
) -> None:
    @contextlib.asynccontextmanager
    async def connect() -> AsyncGenerator[MusicSession]:  # the SDK's own session
        client, socket = SimpleNamespace(vertexai=False), OneFrameSocket(frame)
        yield live_music.AsyncMusicSession(
            api_client=cast(Any, client), websocket=cast(Any, socket)
        )

    deck = Deck(connect, MusicPlan.from_request("ambient"), clock=clock)

    with caplog.at_level(logging.DEBUG):
        deck.start()
        await ended(deck)

    assert (deck.end_reason, deck.detail) == (
        EndReason.FAILED,
        "LyriaMessageError: unreadable message",
    )
    assert "AIzaFakeKey" not in caplog.text  # in neither the messages nor a traceback
    logged = next(record.exc_info[1] for record in caplog.records if record.exc_info)
    assert logged is not None
    assert logged.__context__ is None  # the SDK's error, which quotes the frame, isn't kept


@pytest.mark.parametrize(
    ("reason", "detail"),
    [
        ("Prompt 'rainy lo-fi by A. Artist' is not allowed.", None),
        ("PROHIBITED_CONTENT", "PROHIBITED_CONTENT"),
    ],
    ids=["free text", "token"],
)
async def test_records_a_prompt_refused_before_any_audio(
    lyria: FakeLyria,
    clock: FakeClock,
    caplog: pytest.LogCaptureFixture,
    reason: str,
    detail: str | None,
) -> None:
    deck, session = await start_deck(lyria, clock)

    with caplog.at_level(logging.DEBUG, logger="sobafm.deck"):
        session.refuse(reason)
        await ended(deck)

    assert (deck.end_reason, deck.detail) == (EndReason.FILTERED, detail)
    assert session.closed
    # The reason could quote the request, so only the debug log may carry it (OPS-5).
    above_debug = [
        record.getMessage() for record in caplog.records if record.levelno > logging.DEBUG
    ]
    assert not any("A. Artist" in message for message in above_debug)
    assert any(reason in record.getMessage() for record in caplog.records)


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
        f"Deck {deck.number} closed (code 1011) after 20 s: 20 s of audio at 1.00x real time"
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


SUSPENDED = "Consumer 'api_key:AIzaFakeKey' has been suspended."  # free text that quotes a key


@pytest.mark.parametrize(
    ("reason", "detail", "summary"),
    [
        (
            "RESOURCE_EXHAUSTED",
            "code 1008: RESOURCE_EXHAUSTED",
            "(code 1008: RESOURCE_EXHAUSTED, exhausted)",
        ),
        (SUSPENDED, "code 1008", "(code 1008, rejected)"),
    ],
    ids=["token", "free text"],
)
async def test_logs_a_close_reason_only_as_a_token(
    lyria: FakeLyria,
    clock: FakeClock,
    caplog: pytest.LogCaptureFixture,
    reason: str,
    detail: str,
    summary: str,
) -> None:
    deck, session = await start_deck(lyria, clock)

    with caplog.at_level(logging.DEBUG):
        session.close(1008, reason)
        await ended(deck)

    assert deck.detail == detail
    assert summary in caplog.text  # with the cause, since the session never played
    assert "AIzaFakeKey" not in caplog.text


async def test_names_the_cause_of_a_session_that_ended_short(
    lyria: FakeLyria, clock: FakeClock, caplog: pytest.LogCaptureFixture
) -> None:
    deck, session = await start_deck(lyria, clock)

    with caplog.at_level(logging.INFO, logger="sobafm.deck"):
        session.send_audio(2)
        session.close(1011, "You exceeded your current quota, please check your plan.")
        await ended(deck)

    assert "(code 1011, exhausted) after" in caplog.text  # it never held the pre-roll
    assert not deck.delivered_preroll


async def test_delivers_the_preroll_with_exactly_its_audio(
    lyria: FakeLyria, clock: FakeClock
) -> None:
    deck, session = await start_deck(lyria, clock)

    session.send_audio(PREROLL_S - FRAME_SECONDS)
    await settle()
    assert not deck.delivered_preroll

    session.send_audio(FRAME_SECONDS)
    await settle()
    assert deck.delivered_preroll


async def test_records_a_quota_close_while_generating(
    lyria: FakeLyria, clock: FakeClock, caplog: pytest.LogCaptureFixture
) -> None:
    deck, session = await start_deck(lyria, clock)

    with caplog.at_level(logging.DEBUG):
        session.close(1011, "You exceeded your current quota, please check your plan.")
        await ended(deck)

    assert (deck.end_reason, deck.close_code, deck.failure) == (
        EndReason.CLOSED,
        1011,
        Failure.EXHAUSTED,
    )
    assert "(code 1011, exhausted)" in caplog.text
    assert "exceeded" not in caplog.text  # the reason is matched, never logged


def received(close: Close | None, error: type[ConnectionClosed] = ConnectionClosedError) -> Any:
    """How websockets reports a close it received, and answered, during setup."""
    return error(close, close, rcvd_then_sent=True) if close else error(None, None)


@pytest.mark.parametrize(
    ("closed", "code", "detail", "failure"),
    [
        (
            received(Close(1008, SUSPENDED)),
            1008,
            "ConnectionClosedError: code 1008",
            Failure.REJECTED,
        ),
        (
            received(Close(1008, "RESOURCE_EXHAUSTED")),
            1008,
            "ConnectionClosedError: code 1008: RESOURCE_EXHAUSTED",
            Failure.EXHAUSTED,
        ),
        (
            received(Close(1007, "API key not valid. Please pass a valid API key.")),
            1007,
            "ConnectionClosedError: code 1007",
            Failure.REJECTED,
        ),
        (
            received(Close(1000, SUSPENDED), ConnectionClosedOK),
            1000,
            "ConnectionClosedOK: code 1000",
            None,
        ),
        (
            received(None),
            None,
            "ConnectionClosedError: no close frame received",
            Failure.UNAVAILABLE,
        ),
    ],
    ids=["free text", "token", "rejected key", "normal close", "no close frame"],
)
async def test_records_a_close_during_setup_without_free_text(
    lyria: FakeLyria,
    clock: FakeClock,
    caplog: pytest.LogCaptureFixture,
    closed: ConnectionClosed,
    code: int | None,
    detail: str,
    failure: Failure | None,
) -> None:
    lyria.failure = closed
    deck = Deck(lyria.connect, MusicPlan.from_request("ambient"), clock=clock)

    with caplog.at_level(logging.DEBUG):
        deck.start()
        await ended(deck)

    assert (deck.end_reason, deck.close_code, deck.detail) == (EndReason.FAILED, code, detail)
    assert deck.failure is failure
    assert f"Deck {deck.number} failed: {detail}" in caplog.messages  # a warning, no traceback
    assert "suspended" not in caplog.text


async def test_retiring_while_lyria_closes_keeps_the_close_code(
    lyria: FakeLyria, clock: FakeClock
) -> None:
    deck, session = await start_deck(lyria, clock)
    retire_during_close(lyria, deck)

    session.close(1008, "Project denied")
    await ended(deck)

    assert (deck.end_reason, deck.close_code) == (EndReason.CLOSED, 1008)
    assert session.closed
