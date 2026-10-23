"""Owned installed-candidate checks; no Client, socket, TLS engine or provider."""

# ruff: noqa: PT009, PT027, S101
from __future__ import annotations

import asyncio
import importlib
import importlib.util
import ssl
import unittest
from asyncio import sslproto
from importlib.metadata import distribution, version
from pathlib import Path
from typing import Any
from unittest.mock import patch

from websockets.asyncio.client import ClientConnection
from websockets.client import ClientProtocol
from websockets.datastructures import Headers
from websockets.frames import Frame, Opcode
from websockets.http11 import Response
from websockets.protocol import OPEN
from websockets.uri import parse_uri

_spec = importlib.util.spec_from_file_location(
    "sobafm_base_candidate_checks", Path(__file__).with_name("check_candidate.py")
)
assert _spec is not None
assert _spec.loader is not None
base = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(base)
buffered = importlib.import_module("google.genai._buffered_input")


class OwnedTransport(asyncio.Transport):
    def __init__(self) -> None:
        self.pauses = 0
        self.resumes = 0
        self.closed = False

    def set_write_buffer_limits(self, high: int | None = None, low: int | None = None) -> None:
        pass

    def pause_reading(self) -> None:
        self.pauses += 1

    def resume_reading(self) -> None:
        self.resumes += 1

    def write(self, data: Any) -> None:
        pass

    def is_closing(self) -> bool:
        return self.closed

    def can_write_eof(self) -> bool:
        return False

    def close(self) -> None:
        self.closed = True


class BufferedTests(unittest.IsolatedAsyncioTestCase):
    def connection(self) -> tuple[Any, bytearray, OwnedTransport]:
        storage = bytearray(4096)
        factory = buffered.buffered_connection_factory(storage)
        protocol = ClientProtocol(parse_uri("wss://owned.invalid"), state=OPEN, max_size=524288)
        connection = factory(protocol, max_queue=1)
        transport = OwnedTransport()
        connection.connection_made(transport)
        connection.response = Response(101, "Switching Protocols", Headers())
        self.addCleanup(connection.connection_lost, None)
        return connection, storage, transport

    def test_installed_identity(self) -> None:
        self.assertEqual(version("google-genai"), "2.26.0+sobafm.2")
        self.assertEqual(
            importlib.import_module("google.genai.version").__version__, "2.26.0+sobafm.2"
        )
        self.assertIs(base.music.BoundedReceiveOptions, base.helper.BoundedReceiveOptions)
        self.assertIs(base.music.ShapeLimits, base.shape.ShapeLimits)
        self.assertEqual(
            distribution("google-genai").metadata["License-Expression"], "Apache-2.0 AND MIT"
        )
        self.assertIn(
            Path(__import__("sys").prefix).resolve(), Path(buffered.__file__).resolve().parents
        )

    async def test_public_options_forward_only_explicit_buffered_factory(self) -> None:
        seen: list[dict[str, Any]] = []

        def connect(uri: str, **kwargs: Any) -> Any:
            seen.append(kwargs)
            return base.Connection(streamed=((b"setup",), (base.AUDIO,)))

        options = base.music.BoundedReceiveOptions(
            bytearray(128), 128, 4, 0, 128, 1, input_storage=bytearray(4096)
        )
        with patch.object(base.music, "connect", connect):
            async with base.music.AsyncLiveMusic(base.ApiClient()).connect(
                model="owned-model", compression=None, bounded_receive=options
            ) as session:
                result = await anext(session.receive())
                self.assertEqual(result.server_content.audio_chunks[0].data, b"\0")
        self.assertTrue(callable(seen[0]["create_connection"]))
        self.assertIsNone(seen[0]["compression"])
        self.assertEqual(seen[0]["max_size"], 128)
        self.assertEqual(seen[0]["max_queue"], 1)

    async def test_every_prefix_is_copied_once_with_no_unused_tail_or_fallback(self) -> None:
        connection, storage, _ = self.connection()
        self.assertIsInstance(connection, asyncio.BufferedProtocol)
        self.assertIs(connection.get_buffer(10**9).obj, storage)
        self.assertEqual(len(connection.get_buffer(-1)), 4096)
        storage[:] = b"z" * 4096
        seen: list[int] = []

        def receive(instance: object, data: bytes) -> None:
            self.assertIs(type(data), bytes)
            self.assertEqual(data, b"z" * len(data))
            seen.append(len(data))

        with patch.object(ClientConnection, "data_received", receive):
            for size in range(4097):
                connection.buffer_updated(size)
            for invalid in (-1, 4097, True, 1.5):
                with self.assertRaisesRegex(ValueError, "invalid_buffered_input_prefix"):
                    connection.buffer_updated(invalid)
            with self.assertRaisesRegex(RuntimeError, "buffered_input_transport_required"):
                connection.data_received(b"not a buffered callback")
        self.assertEqual(seen, list(range(4097)))

    async def test_one_factory_cannot_open_another_owner_after_success_or_failure(self) -> None:
        storage = bytearray(4096)
        factory = buffered.buffered_connection_factory(storage)
        with self.assertRaises(TypeError):
            factory()  # failed construction still consumes its one claim
        with self.assertRaisesRegex(RuntimeError, "already_claimed"):
            factory()
        successful = buffered.buffered_connection_factory(bytearray(4096))
        with patch.object(buffered, "BufferedClientConnection", return_value=object()):
            successful()
            with self.assertRaisesRegex(RuntimeError, "already_claimed"):
                successful()
        connection, _, _ = self.connection()
        self.assertIsInstance(connection, ClientConnection)
        for storage in (bytearray(4095), bytearray(4097), b"x" * 4096):
            with self.assertRaisesRegex(ValueError, "4096_byte_input_storage_required"):
                buffered.buffered_connection_factory(storage)
        shared = bytearray(4096)
        with self.assertRaisesRegex(ValueError, "distinct_4096"):
            base.music.BoundedReceiveOptions(shared, 4096, 4, 0, 4096, 1, input_storage=shared)

    async def test_queue_pause_and_resume_preserve_the_ordinary_watermarks(self) -> None:
        connection, storage, transport = self.connection()
        wire = b"".join(Frame(Opcode.BINARY, value).serialize(mask=False) for value in (b"a", b"b"))
        storage[: len(wire)] = wire
        connection.buffer_updated(len(wire))
        self.assertEqual(transport.pauses, 1)
        self.assertEqual(await connection.recv(decode=False), b"a")
        self.assertEqual(transport.resumes, 0)
        self.assertEqual(await connection.recv(decode=False), b"b")
        self.assertEqual(transport.resumes, 1)

    async def test_standard_ssl_buffered_dispatch_bounds_each_drain_and_obeys_pause(self) -> None:
        connection, storage, transport = self.connection()
        # No SSLProtocol constructor, context, SSLObject or native read is used.
        owner = object.__new__(sslproto.SSLProtocol)
        owner._closed = True
        owner._set_app_protocol(connection)
        self.assertTrue(owner._app_protocol_is_buffer)
        wire = Frame(Opcode.BINARY, b"a" * 12000).serialize(mask=False)
        remaining = bytearray(wire)
        requests: list[int] = []
        scheduled: list[object] = []

        class FakeSSL:
            def read(self, size: int, buffer: memoryview) -> int:
                requests.append(size)
                if not remaining:
                    raise ssl.SSLWantReadError()
                count = min(size, len(remaining))
                buffer[:count] = remaining[:count]
                del remaining[:count]
                return count

        owner._sslobj = FakeSSL()
        owner._loop = type(
            "OwnedLoop", (), {"call_soon": lambda _, callback: scheduled.append(callback)}
        )()
        owner._get_read_buffer_size = lambda: 262144
        while remaining:
            owner._do_read__buffered()
        self.assertLessEqual(max(requests), 4096)
        self.assertGreaterEqual(len(scheduled), 2)
        self.assertEqual(await connection.recv(decode=False), b"a" * 12000)
        self.assertEqual(len(storage), 4096)
        # The dispatcher must not call either read path while the app is paused.
        owner._state = sslproto.SSLProtocolState.WRAPPED
        owner._app_reading_paused = True
        owner._control_ssl_reading = lambda: None
        owner._do_read__copied = lambda: self.fail("copied TLS path selected")
        before = len(requests)
        owner._do_read()
        self.assertEqual(len(requests), before)
        self.assertEqual(transport.pauses, 0)

    async def test_tiny_audio_fragments_are_batch_bounded_before_collector_failure(self) -> None:
        connection, storage, transport = self.connection()
        payload = b'{"serverContent":{"audioChunks":[{"data":"' + b"A" * 1300 + b'"}]}}'
        frames = [
            Frame(
                Opcode.BINARY if index == 0 else Opcode.CONT,
                bytes([value]),
                fin=index == len(payload) - 1,
            )
            for index, value in enumerate(payload)
        ]
        wire = b"".join(frame.serialize(mask=False) for frame in frames)
        self.assertLessEqual(len(wire), 4096)
        storage[: len(wire)] = wire
        connection.buffer_updated(len(wire))
        queued = len(connection.recv_messages.frames)
        self.assertGreater(queued, 64)
        self.assertLessEqual(queued + 4, 1369)
        self.assertEqual(transport.pauses, 1)
        options = base.music.BoundedReceiveOptions(bytearray(4096), 4096, 64, 8, 4096, 1)
        receiver = base.helper.BoundedReceiveSession(connection, options)
        with self.assertRaisesRegex(RuntimeError, "fragment_allowance"):
            await receiver.receive_json()
        self.assertTrue(receiver.unusable)
        self.assertGreater(len(connection.recv_messages.frames), 0)
        # Close owns the remaining queued audio; failure is not a queue refund.
        assert receiver.owner is not None
        assert receiver.owner._stream is not None
        await receiver.owner._stream.aclose()


if __name__ == "__main__":
    suite = unittest.TestSuite()
    loader = unittest.TestLoader()
    for name in loader.getTestCaseNames(base.CandidateTests):
        if name != "test_installed_identity_and_public_exports":
            suite.addTest(base.CandidateTests(name))
    suite.addTests(loader.loadTestsFromTestCase(BufferedTests))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    raise SystemExit(not result.wasSuccessful())
