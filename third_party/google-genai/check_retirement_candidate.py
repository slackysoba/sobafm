"""Installed .3 SDK/Deck retirement checks with owned fake I/O only."""

# ruff: noqa: PT009, S101
from __future__ import annotations

import asyncio
import base64
import importlib.util
import json
import unittest
import weakref
from importlib.metadata import version
from pathlib import Path
from typing import Any
from unittest.mock import patch

from websockets.client import ClientProtocol
from websockets.datastructures import Headers
from websockets.frames import Frame, Opcode
from websockets.http11 import Response
from websockets.protocol import OPEN
from websockets.uri import parse_uri

from sobafm.audio_budget import AudioLedger
from sobafm.deck import Deck
from sobafm.plan import MusicPlan

_spec = importlib.util.spec_from_file_location(
    "base_retirement_checks", Path(__file__).with_name("check_candidate.py")
)
assert _spec is not None
assert _spec.loader is not None
base = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(base)


class Transport(asyncio.Transport):
    def set_write_buffer_limits(self, *args: Any) -> None:
        pass

    def pause_reading(self) -> None:
        pass

    def resume_reading(self) -> None:
        pass

    def write(self, data: Any) -> None:
        pass

    def is_closing(self) -> bool:
        return False

    def can_write_eof(self) -> bool:
        return False

    def close(self) -> None:
        pass

    def abort(self) -> None:
        pass


def feed(connection: Any, data: bytes) -> None:
    for start in range(0, len(data), 4096):
        part = data[start : start + 4096]
        view = connection.get_buffer(-1)
        view[: len(part)] = part
        connection.buffer_updated(len(part))


def frame(data: bytes, *, fin: bool = True, opcode: Opcode = Opcode.BINARY) -> bytes:
    return Frame(opcode, data, fin=fin).serialize(mask=False)


class Network:
    def __init__(
        self,
        *,
        setup_failure: bool = False,
        bad_receive: bool = False,
        partial: bool = False,
        parser_failure: bool = False,
        waiting: bool = False,
        setup_waiting: bool = False,
        pcm: bool = False,
    ) -> None:
        self.setup_failure, self.bad_receive = setup_failure, bad_receive
        self.partial, self.parser_failure, self.waiting = partial, parser_failure, waiting
        self.setup_waiting, self.pcm = setup_waiting, pcm
        self.setup_started = asyncio.Event()
        self.connections: list[Any] = []  # weakrefs only
        self.frames: list[Any] = []
        self.closing = asyncio.Event()
        self.release = asyncio.Event()
        self.release.set()

    def connect(self, uri: str, **kwargs: Any) -> Any:
        owner = self

        class Context:
            connection: Any

            async def __aenter__(self) -> Any:
                self.connection = connection = kwargs["create_connection"](
                    ClientProtocol(parse_uri(uri), state=OPEN, max_size=524288),
                    max_queue=1,
                    ping_interval=None,
                )
                transport = Transport()
                # Model SSLProtocol's retained buffered callback after loss.
                transport.buffer_callback = connection.get_buffer
                connection.connection_made(transport)
                connection.response = Response(101, "Switching Protocols", Headers())
                owner.connections.append(weakref.ref(connection))
                if owner.setup_waiting:
                    owner.setup_started.set()
                    try:
                        await asyncio.Event().wait()
                    except asyncio.CancelledError:
                        owner.closing.set()
                        await owner.release.wait()
                        connection.connection_lost(None)
                        raise
                connection.ping_interval = 20
                connection.start_keepalive()
                feed(connection, frame(b"{}"))
                if owner.pcm:
                    feed(
                        connection,
                        frame(
                            json.dumps(
                                {
                                    "serverContent": {
                                        "audioChunks": [
                                            {
                                                "data": base64.b64encode(bytes(3840)).decode(),
                                                "mimeType": "audio/l16;rate=48000;channels=2",
                                            }
                                        ]
                                    }
                                }
                            ).encode()
                        ),
                    )
                if owner.setup_failure:
                    feed(connection, frame(b"queued setup audio"))
                    owner.frames.append(weakref.ref(connection.recv_messages.frames.queue[-1]))
                    feed(connection, b"\x88\x02\x03\xe8")
                    connection.connection_lost(None)
                    raise ValueError("owned setup failure")
                if owner.partial:
                    feed(
                        connection,
                        b"".join(
                            frame(b"a", fin=False, opcode=Opcode.BINARY if i == 0 else Opcode.CONT)
                            for i in range(65)
                        ),
                    )
                elif owner.bad_receive:
                    feed(connection, frame(b"not json"))
                elif not owner.waiting:
                    feed(connection, frame(b'{"filteredPrompt":{"filteredReason":"OWNED"}}'))
                return connection

            async def __aexit__(self, *_: Any) -> None:
                owner.closing.set()
                await owner.release.wait()
                connection = self.connection
                feed(
                    connection,
                    frame(
                        b"independent queued audio",
                        opcode=Opcode.CONT if owner.partial else Opcode.BINARY,
                    ),
                )
                owner.frames.append(weakref.ref(connection.recv_messages.frames.queue[-1]))
                feed(connection, b"\x83\x01x" if owner.parser_failure else b"\x88\x02\x03\xe8")
                connection.connection_lost(None)
                await connection.close()

        return Context()


async def settle() -> None:
    for _ in range(30):
        await asyncio.sleep(0)


class RetirementTests(unittest.IsolatedAsyncioTestCase):
    def test_identity(self) -> None:
        self.assertEqual(version("google-genai"), "2.26.0+sobafm.3")

    async def deck(self, network: Network, audio: AudioLedger) -> Deck:
        module = base.music.AsyncLiveMusic(base.ApiClient())

        def connect(options: Any) -> Any:
            return module.connect(model="owned-model", compression=None, bounded_receive=options)

        deck = Deck(connect, MusicPlan.from_request("ambient"), audio=audio)
        deck.start()
        await settle()
        return deck

    async def test_actual_deck_context_retires_queued_audio_before_reuse(self) -> None:
        network, audio = Network(), AudioLedger()
        with patch.object(base.music, "connect", network.connect):
            for _ in range(3):
                deck = await self.deck(network, audio)
                await deck.wait_ended()
                await settle()
                self.assertTrue(deck.quiescent)
                self.assertFalse(deck.cleanup_failed)
                self.assertTrue(all(ref() is None for ref in network.frames))
                self.assertTrue(all(ref() is None for ref in network.connections))
                self.assertTrue(
                    all(backing.owner is None for backing in audio.lyria_receive.backings)
                )

    async def test_actual_deck_receive_failure_retires_without_gc(self) -> None:
        network, audio = Network(bad_receive=True), AudioLedger()
        with patch.object(base.music, "connect", network.connect):
            deck = await self.deck(network, audio)
            await deck.wait_ended()
            await settle()
        self.assertTrue(deck.quiescent)
        self.assertTrue(all(ref() is None for ref in network.frames + network.connections))

    async def test_partial_stream_and_parser_failure_release_terminal_audio(self) -> None:
        for network in (Network(partial=True), Network(parser_failure=True)):
            with (
                self.subTest(partial=network.partial),
                patch.object(base.music, "connect", network.connect),
            ):
                deck = await self.deck(network, AudioLedger())
                await deck.wait_ended()
                await settle()
                self.assertTrue(deck.quiescent)
                self.assertTrue(all(ref() is None for ref in network.frames + network.connections))

    async def test_retirement_cancels_actual_receive_before_backing_reuse(self) -> None:
        network, audio = Network(waiting=True), AudioLedger()
        with patch.object(base.music, "connect", network.connect):
            deck = await self.deck(network, audio)
            self.assertFalse(deck.quiescent)
            deck.retire()
            await deck.wait_closed()
            await settle()
        self.assertTrue(deck.quiescent)
        self.assertTrue(all(ref() is None for ref in network.frames + network.connections))

    async def test_setup_cancellation_joins_actual_loss_before_reuse(self) -> None:
        network, audio = Network(setup_waiting=True), AudioLedger()
        network.release.clear()
        with patch.object(base.music, "connect", network.connect):
            deck = await self.deck(network, audio)
            await network.setup_started.wait()
            deck.retire()
            await network.closing.wait()
            self.assertFalse(deck.quiescent)
            self.assertTrue(any(backing.owner is deck for backing in audio.lyria_receive.backings))
            network.release.set()
            await deck.wait_closed()
            await settle()
        self.assertTrue(deck.quiescent)
        self.assertTrue(all(ref() is None for ref in network.connections))

    async def test_admitted_pcm_survives_terminal_connection_retirement(self) -> None:
        network, audio = Network(waiting=True, pcm=True), AudioLedger()
        with patch.object(base.music, "connect", network.connect):
            deck = await self.deck(network, audio)
            self.assertEqual(len(deck.frames), 1)
            reserved = audio.reserved
            deck.retire()
            await deck.wait_closed()
            await settle()
        self.assertTrue(deck.quiescent)
        self.assertEqual(len(deck.frames), 1)
        self.assertEqual(audio.reserved, reserved)
        self.assertTrue(all(ref() is None for ref in network.frames + network.connections))

    async def test_unsupported_retirement_retains_actual_backing_and_debt(self) -> None:
        network, audio = Network(), AudioLedger()
        with (
            patch.object(base.music, "connect", network.connect),
            patch("google.genai._buffered_input.installed_version", return_value="unsupported"),
        ):
            deck = await self.deck(network, audio)
            await deck.wait_ended()
            await settle()
        self.assertTrue(deck.cleanup_failed)
        self.assertFalse(deck.quiescent)
        self.assertTrue(any(backing.owner is deck for backing in audio.lyria_receive.backings))
        self.assertTrue(any(ref() is not None for ref in network.frames))

    async def test_actual_setup_failure_retires_created_connection(self) -> None:
        network, audio = Network(setup_failure=True), AudioLedger()
        with patch.object(base.music, "connect", network.connect):
            deck = await self.deck(network, audio)
            await deck.wait_ended()
            await settle()
        self.assertTrue(deck.quiescent)
        self.assertTrue(all(ref() is None for ref in network.frames + network.connections))

    async def test_delayed_close_repeated_waiter_cancellation_and_sticky_deadline(self) -> None:
        network, audio = Network(), AudioLedger()
        network.release.clear()
        with (
            patch.object(base.music, "connect", network.connect),
            patch("sobafm.deck.CLEANUP_SECONDS", 0.02),
        ):
            deck = await self.deck(network, audio)
            await network.closing.wait()
            for _ in range(2):
                waiter = asyncio.create_task(deck.wait_ended())
                await asyncio.sleep(0)
                waiter.cancel()
                await asyncio.gather(waiter, return_exceptions=True)
            await asyncio.sleep(0.03)
            self.assertTrue(deck.cleanup_failed)
            self.assertFalse(deck.quiescent)
            self.assertTrue(any(backing.owner is deck for backing in audio.lyria_receive.backings))
            network.release.set()
            await deck.wait_closed()
            await settle()
        self.assertTrue(deck.cleanup_failed)
        self.assertTrue(deck.quiescent)
        self.assertTrue(all(ref() is None for ref in network.frames + network.connections))


if __name__ == "__main__":
    suite = unittest.TestSuite()
    loader = unittest.TestLoader()
    for name in loader.getTestCaseNames(base.CandidateTests):
        if name != "test_installed_identity_and_public_exports":
            suite.addTest(base.CandidateTests(name))
    suite.addTests(loader.loadTestsFromTestCase(RetirementTests))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    raise SystemExit(not result.wasSuccessful())
