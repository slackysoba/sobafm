import asyncio
import base64
import contextlib
import json
import logging
from collections import deque
from collections.abc import AsyncGenerator, AsyncIterator
from types import SimpleNamespace
from typing import Any, Literal, cast

import pytest
from google import genai
from google.genai import live_music
from google.genai.json_shape_preflight import ShapeLimits

import sobafm.audio_budget
import sobafm.deck
from sobafm.audio_budget import (
    LYRIA_INPUT_BATCH_BYTES,
    LYRIA_RECEIVE_BYTES,
    LYRIA_RECEIVE_RESERVATION_BYTES,
    PCM_LIMIT,
    AudioBytes,
    AudioLedger,
    ReceiveBacking,
)
from sobafm.deck import (
    BoundedReceiveOptions,  # pyright: ignore[reportPrivateImportUsage]
    Deck,
    EndReason,
    MusicSession,
    lyria,
)
from sobafm.lyria_engine import LyriaEngine, SessionPool
from sobafm.pcm import FRAME_BYTES, MIME_TYPE
from sobafm.plan import MusicPlan
from sobafm.program import Program
from tests.doubles import FakeLyria, FakeSession, settle

# Production logging suppresses this same expected ADR-0004 warning.
pytestmark = pytest.mark.filterwarnings("ignore:Realtime music generation is experimental")


def make_deck(connect: sobafm.deck.Connect, audio: AudioLedger) -> Deck:
    return Deck(connect, MusicPlan.from_request("ambient"), audio=audio)


async def test_real_connector_requires_and_forwards_public_bounded_options(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[dict[str, Any]] = []

    @contextlib.asynccontextmanager
    async def connect(**kwargs: Any) -> AsyncGenerator[MusicSession]:
        seen.append(kwargs)
        yield FakeSession()

    client = SimpleNamespace(
        aio=SimpleNamespace(live=SimpleNamespace(music=SimpleNamespace(connect=connect)))
    )

    def owned_client(**kwargs: Any) -> SimpleNamespace:
        return client

    monkeypatch.setattr(genai, "Client", owned_client)
    options = BoundedReceiveOptions(
        bytearray(128),
        128,
        4,
        0,
        128,
        1,
        ShapeLimits(128, 64, 8, 16),
        input_storage=bytearray(LYRIA_INPUT_BATCH_BYTES),
    )
    async with lyria("owned-fake-key")(options):
        pass
    assert seen == [
        {"model": "models/lyria-realtime-exp", "compression": None, "bounded_receive": options}
    ]


async def test_each_connection_receives_its_own_prepaid_input_pair() -> None:
    seen: list[BoundedReceiveOptions] = []

    @contextlib.asynccontextmanager
    async def connect(options: BoundedReceiveOptions) -> AsyncGenerator[MusicSession]:
        seen.append(options)
        yield FakeSession()

    audio = AudioLedger()
    storage = audio.lyria_receive
    assert storage is not None
    decks = [make_deck(connect, audio) for _ in range(2)]
    for deck in decks:
        deck.start()
    try:
        await settle()
        assert len(seen) == 2
        for index, options in enumerate(seen):
            backing = storage.backings[index]
            assert options.storage is backing.storage
            assert options.input_storage is backing.input_storage
            assert backing.owner is decks[index]
            assert len(backing.input_storage) == LYRIA_INPUT_BATCH_BYTES
            assert options.max_bytes == options.max_size == LYRIA_RECEIVE_BYTES
            assert options.max_queue == 1
            assert options.max_fragments == 64
            assert options.max_empty_fragments == 8
        assert seen[0].input_storage is not seen[1].input_storage
        assert audio.reserved.other == 2 * LYRIA_RECEIVE_RESERVATION_BYTES
    finally:
        for deck in decks:
            deck.retire()
        for deck in decks:
            await deck.wait_ended()


def test_payload_allocation_is_prepaid_and_replacement_reuses_only_paid_storage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    audio = AudioLedger()
    allocated: list[AudioBytes] = []

    class ObservedBacking(ReceiveBacking):
        def __init__(self) -> None:
            allocated.append(audio.reserved)
            super().__init__()

    monkeypatch.setattr(sobafm.audio_budget, "ReceiveBacking", ObservedBacking)
    storage = audio.lyria_receive
    assert storage is not None
    assert allocated == [AudioBytes(other=2 * LYRIA_RECEIVE_RESERVATION_BYTES)] * 2
    assert all(
        len(backing.input_storage) == LYRIA_INPUT_BATCH_BYTES for backing in storage.backings
    )
    audio.unbounded()
    assert audio.lyria_receive is storage
    assert len(allocated) == 2
    assert audio.reserve(AudioBytes(other=1)) is None


async def test_unknown_unfunded_ledger_refuses_before_allocation_or_connect(
    monkeypatch: pytest.MonkeyPatch,
    lyria: FakeLyria,
) -> None:
    audio = AudioLedger()
    _ = audio.pcm
    audio.unbounded()

    def forbidden() -> ReceiveBacking:
        pytest.fail("unfunded payload allocated")

    monkeypatch.setattr(sobafm.audio_budget, "ReceiveBacking", forbidden)
    deck = make_deck(lyria.connect, audio)
    deck.start()
    await deck.wait_ended()
    assert deck.end_reason is EndReason.FAILED
    assert deck.quiescent
    assert lyria.sessions == []
    assert audio.reserved == AudioBytes(pcm=PCM_LIMIT)


def test_partial_allocation_keeps_charge_and_owner_without_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    audio = AudioLedger()
    calls = 0

    def allocate() -> ReceiveBacking:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise MemoryError("owned allocation failure")
        return ReceiveBacking()

    monkeypatch.setattr(sobafm.audio_budget, "ReceiveBacking", allocate)
    with pytest.raises(MemoryError, match="owned allocation failure"):
        _ = audio.lyria_receive
    storage = audio.lyria_receive
    assert storage is not None
    assert storage.uncertain
    assert len(storage.backings) == 1
    assert storage.claim(object()) is None
    assert not audio.bounded
    assert audio.reserved == AudioBytes(other=2 * LYRIA_RECEIVE_RESERVATION_BYTES)
    assert calls == 2


def test_receive_pair_refuses_duplicate_allocation_and_duplicate_owner() -> None:
    audio = AudioLedger()
    storage = audio.lyria_receive
    assert storage is not None
    owner = object()
    first = storage.claim(owner)
    assert first is not None
    assert storage.claim(owner) is None
    original = tuple(storage.backings)
    with pytest.raises(RuntimeError, match="already started"):
        storage.allocate()
    assert tuple(storage.backings) == original
    assert audio.reserved == AudioBytes(other=2 * LYRIA_RECEIVE_RESERVATION_BYTES)
    storage.released(first, owner)
    replacement = object()
    assert storage.claim(replacement) is first
    with pytest.raises(RuntimeError, match="ownership changed"):
        storage.released(first, owner)


def test_input_allocation_failure_retains_full_prepaid_pair_without_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    audio = AudioLedger()
    calls: list[int] = []

    def allocate(size: int) -> bytearray:
        calls.append(size)
        assert audio.reserved == AudioBytes(other=2 * LYRIA_RECEIVE_RESERVATION_BYTES)
        if size == LYRIA_INPUT_BATCH_BYTES:
            raise MemoryError("owned input allocation failure")
        return bytearray(size)

    monkeypatch.setattr(sobafm.audio_budget, "bytearray", allocate, raising=False)
    with pytest.raises(MemoryError, match="owned input allocation failure"):
        _ = audio.lyria_receive
    storage = audio.lyria_receive
    assert storage is not None
    assert storage.uncertain
    assert storage.backings == []
    assert storage.claim(object()) is None
    assert calls == [LYRIA_RECEIVE_BYTES, LYRIA_INPUT_BATCH_BYTES]
    assert not audio.bounded


async def test_active_receivers_are_distinct_no_third_store_and_reuse_follows_close(
    lyria: FakeLyria,
) -> None:
    audio = AudioLedger()
    _ = audio.pcm
    storage = audio.lyria_receive
    assert storage is not None
    audio.unbounded()
    decks = [make_deck(lyria.connect, audio) for _ in range(3)]
    for deck in decks:
        deck.start()
    await settle()
    assert len(lyria.sessions) == 2
    assert [backing.owner for backing in storage.backings] == decks[:2]
    assert storage.backings[0].storage is not storage.backings[1].storage
    assert storage.backings[0].input_storage is not storage.backings[1].input_storage
    assert decks[2].end_reason is EndReason.FAILED
    decks[0].retire()
    await decks[0].wait_ended()
    assert lyria.sessions[0].closed
    assert storage.backings[0].owner is None
    replacement = make_deck(lyria.connect, audio)
    replacement.start()
    await settle()
    assert storage.backings[0].owner is replacement
    assert len(storage.backings) == 2
    for deck in (decks[1], replacement):
        deck.retire()
        await deck.wait_ended()
    assert audio.reserved == AudioBytes(pcm=PCM_LIMIT, other=2 * LYRIA_RECEIVE_RESERVATION_BYTES)
    assert not audio.bounded


class GatedContext:
    def __init__(self, *, setup: bool = False, fail_close: bool = False) -> None:
        self.session = FakeSession()
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.setup = setup
        self.fail_close = fail_close
        self.cancellations = 0
        self.closes = 0

    async def __aenter__(self) -> MusicSession:
        self.entered.set()
        if self.setup:
            while not self.release.is_set():
                try:
                    await self.release.wait()
                except asyncio.CancelledError:
                    self.cancellations += 1
        return self.session

    async def __aexit__(self, *_: object) -> None:
        self.closes += 1
        await self.release.wait()
        if self.fail_close:
            raise RuntimeError("owned close payload must not escape")
        self.session.closed = True


@pytest.mark.parametrize("setup", [False, True], ids=["closing", "late-setup"])
async def test_original_deadline_is_sticky_but_late_actual_close_releases_borrow(
    monkeypatch: pytest.MonkeyPatch,
    setup: bool,
) -> None:
    monkeypatch.setattr(sobafm.deck, "CLEANUP_SECONDS", 0.02)
    audio = AudioLedger()
    context = GatedContext(setup=setup)
    deck = make_deck(lambda _: context, audio)
    deck.start()
    await context.entered.wait()
    if not setup:
        await settle()
        context.session.send_audio(data=bytes(FRAME_BYTES))
        await settle()
    deck.retire()
    first_waiter = asyncio.create_task(deck.wait_ended())
    await asyncio.sleep(0)
    first_waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first_waiter
    deck.retire()
    async with asyncio.timeout(1):
        await deck.wait_ended()
    assert deck.cleanup_failed
    assert not deck.quiescent
    assert deck.end_reason is EndReason.FAILED
    storage = audio.lyria_receive
    assert storage is not None
    assert storage.backings[0].owner is deck
    assert context.closes == (0 if setup else 1)
    context.release.set()
    async with asyncio.timeout(1):
        await deck.wait_closed()
    assert deck.cleanup_failed
    assert deck.end_reason is EndReason.FAILED
    assert deck.quiescent
    assert storage.backings[0].owner is None
    assert context.closes == 1
    assert context.cancellations <= 1
    if not setup:
        assert len(deck.frames) == 1
    deck.discard_pcm()


async def test_failed_close_retains_backing_and_process_session_slots(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sobafm.deck, "CLEANUP_SECONDS", 0.02)
    contexts = [GatedContext(fail_close=True), GatedContext(fail_close=True)]
    for context in contexts:
        context.release.set()
    remaining = deque(contexts)
    audio = AudioLedger()
    pool = SessionPool(2)
    engine = LyriaEngine(lambda _: remaining.popleft(), pool, audio=audio)
    program = Program(MusicPlan.from_request("ambient"), "owned-requester", 600.0)
    assert engine.reserve(program)
    engine.generate(program)
    await settle()
    await engine.close()
    storage = audio.lyria_receive
    assert storage is not None
    assert all(deck.cleanup_failed and not deck.quiescent for deck in engine.decks)
    assert all(backing.owner is not None for backing in storage.backings)
    replacement = LyriaEngine(lambda _: pytest.fail("connect after debt"), pool, audio=audio)
    assert not replacement.reserve(program)
    assert not audio.bounded


class Connection:
    """The public SDK's WebSocket I/O, with no socket or authentication client."""

    def __init__(self, messages: list[tuple[bytes, ...]]) -> None:
        self.messages = deque(messages)
        self.calls: list[dict[str, object]] = []
        self.streams = 0
        self.yielded: list[int] = []
        self.legacy = 0
        self.closes = 0
        self.blocked = asyncio.Event()
        self.close_release = asyncio.Event()
        self.close_release.set()

    def recv_streaming(self, decode: Literal[False]) -> AsyncIterator[bytes]:
        assert decode is False
        self.streams += 1

        async def stream() -> AsyncIterator[bytes]:
            if not self.messages:
                self.blocked.set()
                await asyncio.Future[None]()
            else:
                fragments = self.messages.popleft()
                self.yielded.append(0)
                for fragment in fragments:
                    self.yielded[-1] += 1
                    yield fragment

        return stream()

    async def recv(self, **_: object) -> bytes:
        self.legacy += 1
        pytest.fail("legacy whole-message receive")

    async def send(self, _: str) -> None:
        pass

    async def __aenter__(self) -> Connection:
        return self

    async def __aexit__(self, *_: object) -> None:
        self.closes += 1
        await self.close_release.wait()


class ApiClient:
    """Exact public constructor inputs of a fake API client, never a real Client."""

    api_key = "owned-fake-key"
    vertexai = False
    _http_options = SimpleNamespace(api_version="v1beta", headers={})

    def _websocket_base_url(self) -> str:
        return "wss://owned.invalid"


def installed_connector(
    monkeypatch: pytest.MonkeyPatch, connection: Connection
) -> sobafm.deck.Connect:
    def connect(uri: str, **kwargs: object) -> Connection:
        assert uri.startswith("wss://owned.invalid/")
        connection.calls.append(kwargs)
        return connection

    monkeypatch.setattr(live_music, "connect", connect)
    module = live_music.AsyncLiveMusic(cast(Any, ApiClient()))
    client = SimpleNamespace(aio=SimpleNamespace(live=SimpleNamespace(music=module)))

    def owned_client(**_: object) -> SimpleNamespace:
        return client

    monkeypatch.setattr(genai, "Client", owned_client)
    return lyria("owned-fake-key")


@pytest.mark.parametrize(
    ("fragments", "consumed"),
    [
        ((b"x" * (LYRIA_RECEIVE_BYTES + 1),), 1),
        ((b" " * LYRIA_RECEIVE_BYTES, b"x"), 2),
        ((b" ",) * 65, 65),
        ((b"",) * 9, 9),
        ((b"[" * 17,), 1),
        ((b"[" + b"0," * 512 + b"0]",), 1),
        ((b"1" * 65,), 1),
        ((b"not-json-owned-secret",), 1),
        ((b"\xff",), 1),
        ((b'{"serverContent":{"audioChunks":[{"data":{}}]}}',), 1),
    ],
    ids=[
        "single-byte-excess",
        "exact-byte-plus-one",
        "fragments",
        "empty-fragments",
        "depth",
        "units",
        "scalar",
        "json",
        "utf8",
        "model",
    ],
)
async def test_installed_bounded_path_fails_safely_and_preserves_admitted_pcm(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    fragments: tuple[bytes, ...],
    consumed: int,
) -> None:
    loads = json.loads
    parsed: list[int] = []

    def parse(value: Any, **kwargs: Any) -> Any:
        parsed.append(len(value))
        return loads(value, **kwargs)

    monkeypatch.setattr(json, "loads", parse)
    audio = bytes(FRAME_BYTES)
    valid = json.dumps(
        {
            "serverContent": {
                "audioChunks": [{"data": base64.b64encode(audio).decode(), "mimeType": MIME_TYPE}]
            }
        }
    ).encode()
    connection = Connection([(b"setup",), (valid[:20], valid[20:]), fragments])
    deck = make_deck(installed_connector(monkeypatch, connection), AudioLedger())
    with caplog.at_level(logging.WARNING, logger="sobafm.deck"):
        deck.start()
        async with asyncio.timeout(1):
            await deck.wait_ended()
    assert deck.end_reason is EndReason.FAILED
    assert deck.detail == "LyriaMessageError: unreadable message"
    assert deck.quiescent
    assert connection.closes == 1
    assert connection.legacy == 0
    assert connection.streams == 3
    assert connection.yielded == [1, 2, consumed]
    # Byte, fragment, empty and shape rejection never reaches stock JSON parsing.
    assert len(parsed) == (
        2
        if fragments[0]
        in (b"not-json-owned-secret", b'{"serverContent":{"audioChunks":[{"data":{}}]}}')
        else 1
    )
    assert len(connection.calls) == 1
    assert connection.calls[0]["compression"] is None
    assert connection.calls[0]["max_size"] == LYRIA_RECEIVE_BYTES
    assert connection.calls[0]["max_queue"] == 1
    assert callable(connection.calls[0]["create_connection"])
    assert [frame.data for frame in deck.frames] == [audio]
    assert all(record.exc_info is None for record in caplog.records)
    assert "owned-secret" not in caplog.text
    deck.discard_pcm()


async def test_setup_limits_apply_before_any_session_can_play(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = Connection([(b"x" * (LYRIA_RECEIVE_BYTES + 1),)])
    deck = make_deck(installed_connector(monkeypatch, connection), AudioLedger())
    deck.start()
    await deck.wait_ended()
    assert deck.end_reason is EndReason.FAILED
    assert deck.quiescent
    assert deck.detail == "LyriaMessageError: unreadable setup message"
    assert connection.streams == 1
    assert connection.closes == 1
    assert connection.legacy == 0


async def test_partial_receive_cancellation_waits_for_context_close_before_reuse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = Connection([(b"setup",)])
    connection.close_release.clear()
    audio = AudioLedger()
    deck = make_deck(installed_connector(monkeypatch, connection), audio)
    deck.start()
    await connection.blocked.wait()
    deck.retire()
    await settle()
    storage = audio.lyria_receive
    assert storage is not None
    assert not deck.quiescent
    assert storage.backings[0].owner is deck
    held_input = storage.backings[0].input_storage
    assert callable(connection.calls[0]["create_connection"])
    assert connection.closes == 1
    connection.close_release.set()
    await deck.wait_ended()
    assert deck.quiescent
    assert storage.backings[0].owner is None
    assert storage.backings[0].input_storage is held_input
    assert connection.streams == 2
    assert connection.legacy == 0


@pytest.mark.parametrize(
    "setup",
    [
        (b" " * LYRIA_RECEIVE_BYTES,),
        (b"s",) * 64,
        (b"",) * 8 + (b"setup",),
    ],
    ids=["exact-bytes", "exact-fragments", "exact-empty-fragments"],
)
async def test_exact_setup_boundary_still_allows_receiving(
    monkeypatch: pytest.MonkeyPatch,
    setup: tuple[bytes, ...],
) -> None:
    connection = Connection([setup])
    deck = make_deck(installed_connector(monkeypatch, connection), AudioLedger())
    deck.start()
    async with asyncio.timeout(1):
        await connection.blocked.wait()
    assert deck.end_reason is None
    deck.retire()
    await deck.wait_ended()
    assert deck.end_reason is EndReason.RETIRED
    assert deck.quiescent
    assert connection.closes == 1
    assert connection.legacy == 0


async def test_iterator_finalizer_remains_owned_before_context_and_borrow_release(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sobafm.deck, "CLEANUP_SECONDS", 0.02)
    iterator_closing = asyncio.Event()
    release = asyncio.Event()
    context = GatedContext()
    context.release.set()

    class Session(FakeSession):
        async def receive(self) -> AsyncGenerator[genai.types.LiveMusicServerMessage]:
            try:
                await asyncio.Future[None]()
                yield genai.types.LiveMusicServerMessage()
            finally:
                iterator_closing.set()
                await release.wait()

    context.session = Session()
    audio = AudioLedger()
    deck = make_deck(lambda _: context, audio)
    deck.start()
    await settle()
    deck.retire()
    await iterator_closing.wait()
    deck.retire()
    await deck.wait_ended()
    storage = audio.lyria_receive
    assert storage is not None
    assert deck.cleanup_failed
    assert not deck.quiescent
    assert context.closes == 0
    assert storage.backings[0].owner is deck
    release.set()
    async with asyncio.timeout(1):
        await deck.wait_closed()
    assert context.closes == 1
    assert deck.quiescent
    assert deck.cleanup_failed


def test_insufficient_ordinary_capacity_refuses_without_payload_allocation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    audio = AudioLedger()
    assert audio.reserve(AudioBytes(other=69_000_000)) is not None

    def forbidden() -> ReceiveBacking:
        pytest.fail("unfunded payload allocated")

    monkeypatch.setattr(sobafm.audio_budget, "ReceiveBacking", forbidden)
    assert audio.lyria_receive is None
    assert audio.bounded
    assert audio.reserved == AudioBytes(other=69_000_000)


async def test_engine_close_survives_caller_cancellation_and_holds_slots_until_actual_release(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sobafm.deck, "CLEANUP_SECONDS", 0.02)
    contexts = [GatedContext(), GatedContext()]
    remaining = deque(contexts)
    pool, audio = SessionPool(2), AudioLedger()
    engine = LyriaEngine(lambda _: remaining.popleft(), pool, audio=audio)
    program = Program(MusicPlan.from_request("ambient"), "owned-requester", 600.0)
    assert engine.reserve(program)
    engine.generate(program)
    await settle()
    caller = asyncio.create_task(engine.close())
    await settle()
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller
    await engine.close()
    assert len(engine.decks) == 2
    assert all(deck.cleanup_failed for deck in engine.decks)
    assert not pool.reserve(object())
    for context in contexts:
        context.release.set()
    await settle()
    assert engine.decks == []
    assert pool.reserve(object())
    storage = audio.lyria_receive
    assert storage is not None
    assert all(backing.owner is None for backing in storage.backings)
    assert audio.reserved == AudioBytes(pcm=PCM_LIMIT, other=2 * LYRIA_RECEIVE_RESERVATION_BYTES)
    assert not audio.bounded


async def test_observed_remote_close_keeps_its_category_when_retirement_starts_iterator_close(
    monkeypatch: pytest.MonkeyPatch,
    lyria: FakeLyria,
) -> None:
    observed = asyncio.Event()
    original = Deck._start_cleanup_clock  # pyright: ignore[reportPrivateUsage]

    def starting(deck: Deck) -> None:
        original(deck)
        observed.set()

    monkeypatch.setattr(Deck, "_start_cleanup_clock", starting)
    deck = make_deck(lyria.connect, AudioLedger())
    deck.start()
    await settle()
    lyria.sessions[0].close(1011, "RESOURCE_EXHAUSTED")
    await observed.wait()
    deck.retire()
    await deck.wait_ended()
    assert deck.end_reason is EndReason.CLOSED
    assert deck.close_code == 1011
    assert deck.failure is sobafm.deck.Failure.EXHAUSTED
    assert deck.quiescent


@pytest.mark.parametrize("filtered", [True, False], ids=["filtered", "invalid-MIME"])
async def test_observed_terminal_result_survives_retirement_during_held_iterator_finalizer(
    filtered: bool,
) -> None:
    closing, release = asyncio.Event(), asyncio.Event()

    class Session(FakeSession):
        async def receive(self) -> AsyncGenerator[genai.types.LiveMusicServerMessage]:
            try:
                if filtered:
                    yield genai.types.LiveMusicServerMessage(
                        filtered_prompt=genai.types.LiveMusicFilteredPrompt(
                            text="owned", filtered_reason="PROHIBITED_CONTENT"
                        )
                    )
                else:
                    for mime in (MIME_TYPE, "owned/invalid"):
                        yield genai.types.LiveMusicServerMessage(
                            server_content=genai.types.LiveMusicServerContent(
                                audio_chunks=[
                                    genai.types.AudioChunk(data=bytes(FRAME_BYTES), mime_type=mime)
                                ]
                            )
                        )
            finally:
                closing.set()
                await release.wait()

    session = Session()

    @contextlib.asynccontextmanager
    async def connect(_: BoundedReceiveOptions) -> AsyncGenerator[MusicSession]:
        try:
            yield session
        finally:
            session.closed = True

    audio = AudioLedger()
    deck = make_deck(connect, audio)
    deck.start()
    try:
        async with asyncio.timeout(1):
            await closing.wait()
        deck.retire()
        await settle()
        assert not deck.quiescent
        release.set()
        async with asyncio.timeout(1):
            await deck.wait_ended()
        assert deck.end_reason is (EndReason.FILTERED if filtered else EndReason.FAILED)
        assert deck.detail == (
            "PROHIBITED_CONTENT" if filtered else "LyriaMessageError: unexpected audio format"
        )
        assert deck.quiescent
        assert not deck.cleanup_failed
        assert session.closed
        assert len(deck.frames) == (0 if filtered else 1)
        storage = audio.lyria_receive
        assert storage is not None
        assert all(backing.owner is None for backing in storage.backings)
    finally:
        release.set()
        deck.retire()
        async with asyncio.timeout(1):
            await deck.wait_closed()
        deck.discard_pcm()
