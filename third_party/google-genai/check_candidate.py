"""Owned mock checks for an installed candidate; never constructs an SDK Client."""

# The standalone candidate environment needs only stdlib unittest, not pytest.
# This entire file is owned test code, including the fake I/O assertions.
# ruff: noqa: PT009, PT027, S101

from __future__ import annotations

import asyncio
import importlib
import json
import sys
import unittest
from collections.abc import AsyncIterator
from importlib.metadata import distribution, version
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal
from unittest.mock import patch

music = importlib.import_module("google.genai.live_music")
helper = importlib.import_module("google.genai._bounded_receive")
shape = importlib.import_module("google.genai.json_shape_preflight")
AUDIO = b'{"serverContent":{"audioChunks":[{"data":"AA==","mimeType":"audio/pcm"}]}}'


class Connection:
    def __init__(
        self,
        *,
        streamed: tuple[tuple[bytes, ...], ...] = (),
        received: tuple[str, ...] = (),
    ) -> None:
        self.streamed = list(streamed)
        self.received = list(received)
        self.stream_calls: int = 0
        self.yielded = 0
        self.recv_calls: list[bool] = []
        self.sent: list[str] = []
        self.close_calls = 0

    def recv_streaming(self, decode: Literal[False]) -> AsyncIterator[bytes]:
        assert decode is False
        self.stream_calls += 1
        fragments = self.streamed.pop(0)

        async def stream() -> AsyncIterator[bytes]:
            for fragment in fragments:
                self.yielded += 1
                yield fragment

        return stream()

    async def recv(self, *, decode: bool = True) -> str:
        self.recv_calls.append(decode)
        return self.received.pop(0)

    async def send(self, value: str) -> None:
        self.sent.append(value)

    async def close(self) -> None:
        self.close_calls += 1

    async def __aenter__(self) -> Connection:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()


class ApiClient:
    """Only the already-owned constructor inputs; no real/default auth client."""

    api_key = "owned-sdk-test-value"
    vertexai = False
    _http_options = SimpleNamespace(api_version="v1alpha", headers={"x-owned": "test"})

    def _websocket_base_url(self) -> str:
        return "wss://owned.invalid"


def options(*, size: int = 128, shape_limits: Any = None) -> Any:
    return music.BoundedReceiveOptions(bytearray(size), size, 4, 0, size, 3, shape_limits)


class CandidateTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.connections: list[Connection] = []
        self.connect_calls: list[dict[str, object]] = []
        self.legacy_headers = False

        def connect(uri: str, **kwargs: object) -> Connection:
            self.assertTrue(uri.startswith("wss://owned.invalid/"))
            self.connect_calls.append(kwargs)
            if self.legacy_headers and "additional_headers" in kwargs:
                raise TypeError("owned older header spelling")
            return self.connections.pop(0)

        # Scoped test I/O replacement in this isolated process, always restored.
        # Neither an installed file nor the application's SDK integration is changed.
        replacement = patch.object(music, "connect", connect)
        replacement.start()
        self.addCleanup(replacement.stop)
        self.module = music.AsyncLiveMusic(ApiClient())

    def test_installed_identity_and_public_exports(self) -> None:
        self.assertEqual(version("google-genai"), "2.26.0+sobafm.1")
        for module in (music, helper, shape):
            assert module.__file__ is not None
            self.assertIn(Path(sys.prefix).resolve(), Path(module.__file__).resolve().parents)
        self.assertIs(music.BoundedReceiveOptions, helper.BoundedReceiveOptions)
        self.assertIs(music.ShapeLimits, shape.ShapeLimits)
        sdk_version = importlib.import_module("google.genai.version")
        self.assertEqual(sdk_version.__version__, "2.26.0+sobafm.1")
        installed = distribution("google-genai")
        self.assertEqual(installed.metadata["License-Expression"], "Apache-2.0 AND MIT")
        direct = installed.read_text("direct_url.json")
        assert direct is not None
        self.assertTrue(json.loads(direct)["url"].endswith(".whl"))

    async def test_omitted_option_keeps_default_receive_and_actual_model_conversion(self) -> None:
        connection = Connection(received=('{"setupComplete":true}', AUDIO.decode()))
        self.connections.append(connection)
        async with self.module.connect(model="owned-model") as session:
            result = await anext(session.receive())
            self.assertEqual(result.server_content.audio_chunks[0].data, b"\0")
            self.assertEqual(connection.stream_calls, 0)
            self.assertEqual(connection.recv_calls, [False, False])
            self.assertEqual(self.connect_calls[-1]["compression"], "deflate")
            self.assertNotIn("max_size", self.connect_calls[-1])
            self.assertNotIn("max_queue", self.connect_calls[-1])
        self.assertEqual(connection.close_calls, 1)

    async def test_setup_and_later_streaming_forward_limits_and_ignore_unused_tail(self) -> None:
        replacement_audio = AUDIO.replace(b"AA==", b"AQ==")
        connection = Connection(
            streamed=((b"setup",), (AUDIO[:17], AUDIO[17:]), (replacement_audio,))
        )
        self.connections.append(connection)
        limits = music.ShapeLimits(128, 128, 8, 8)
        config = options(shape_limits=limits)
        config.storage[:] = b"!" * 128
        async with self.module.connect(
            model="owned-model", compression=None, bounded_receive=config
        ) as session:
            self.assertEqual(connection.stream_calls, 1)
            self.assertEqual(connection.recv_calls, [])
            result = await anext(session.receive())
            self.assertEqual(result.server_content.audio_chunks[0].data, b"\0")
            self.assertEqual(connection.stream_calls, 2)
            self.assertEqual(config.storage[: len(AUDIO)], AUDIO)
            self.assertEqual(config.storage[len(AUDIO) :], b"!" * (128 - len(AUDIO)))
            self.assertEqual(connection.close_calls, 0)
            self.assertEqual(self.connect_calls[-1]["max_size"], 128)
            self.assertEqual(self.connect_calls[-1]["max_queue"], 3)
            self.assertIsNone(self.connect_calls[-1]["compression"])
            self.assertEqual(self.connect_calls[-1]["additional_headers"], {"x-owned": "test"})
            replacement = await anext(session.receive())
            self.assertEqual(replacement.server_content.audio_chunks[0].data, b"\1")
            self.assertEqual(result.server_content.audio_chunks[0].data, b"\0")
            self.assertEqual(connection.stream_calls, 3)
        self.assertEqual(connection.close_calls, 1)

    async def test_older_header_fallback_preserves_finite_options(self) -> None:
        self.legacy_headers = True
        connection = Connection(streamed=((b"setup",), (AUDIO,)))
        self.connections.append(connection)
        async with self.module.connect(
            model="owned-model", compression=None, bounded_receive=options()
        ) as session:
            result = await anext(session.receive())
            self.assertEqual(result.server_content.audio_chunks[0].data, b"\0")
            self.assertEqual(len(self.connect_calls), 2)
            forwarded = self.connect_calls[-1]
            self.assertNotIn("additional_headers", forwarded)
            self.assertEqual(forwarded["extra_headers"], {"x-owned": "test"})
            self.assertEqual(forwarded["max_size"], 128)
            self.assertEqual(forwarded["max_queue"], 3)
            self.assertIsNone(forwarded["compression"])
        self.assertEqual(connection.close_calls, 1)

    async def test_shape_rejection_precedes_parser_and_poison_prevents_next_receive(self) -> None:
        for raw, limits, code in (
            (b"[0,0,0,0]", (128, 8, 8, 8), "json_shape_units"),
            (b"[[0]]", (128, 128, 1, 8), "json_shape_depth"),
            (b'{"sampleRate":24000}', (128, 128, 8, 4), "json_shape_scalar_run"),
        ):
            with self.subTest(code=code):
                connection = Connection(streamed=((b"setup",), (raw,)))
                self.connections.append(connection)
                config = options(shape_limits=music.ShapeLimits(*limits))
                async with self.module.connect(
                    model="owned-model", bounded_receive=config
                ) as session:
                    with patch.object(
                        helper.json, "loads", side_effect=AssertionError("parser ran")
                    ) as parse:
                        with self.assertRaisesRegex(ValueError, code):
                            await anext(session.receive())
                        parse.assert_not_called()
                    with self.assertRaises(helper.ReceiveUnavailableError):
                        await anext(session.receive())
                    self.assertEqual(connection.stream_calls, 2)
                    self.assertEqual(connection.close_calls, 0)
                self.assertEqual(connection.close_calls, 1)

    async def test_utf8_and_json_diagnostics_drop_payload_fields(self) -> None:
        for raw, message in ((b"\xff", "Invalid UTF-8"), (b"not-json", "Failed to parse")):
            with self.subTest(raw=raw):
                connection = Connection(streamed=((b"setup",), (raw,)))
                self.connections.append(connection)
                async with self.module.connect(
                    model="owned-model", bounded_receive=options()
                ) as session:
                    with self.assertRaisesRegex(ValueError, message) as caught:
                        await anext(session.receive())
                    context = caught.exception.__context__
                    if isinstance(context, UnicodeDecodeError):
                        self.assertEqual(context.object, b"")
                        self.assertEqual(context.args[1], b"")
                    elif isinstance(context, json.JSONDecodeError):
                        self.assertEqual(context.doc, "")
                    else:
                        self.fail("unexpected bounded diagnostic context")
                    with self.assertRaises(helper.ReceiveUnavailableError):
                        await anext(session.receive())
                    self.assertEqual(connection.close_calls, 0)
                self.assertEqual(connection.close_calls, 1)

    async def test_byte_fragment_and_empty_excess_are_terminal_before_further_read(self) -> None:
        for fragments, config, code, copied, reads in (
            (
                (b"ab", b"cd", b"never"),
                music.BoundedReceiveOptions(bytearray(3), 3, 4, 0, 3, 2),
                "byte_capacity",
                2,
                2,
            ),
            (
                (b"a", b"b", b"c", b"never"),
                music.BoundedReceiveOptions(bytearray(16), 16, 2, 0, 16, 2),
                "fragment_allowance",
                2,
                3,
            ),
            ((b"", b"never"), options(), "empty_fragment_allowance", 0, 1),
        ):
            with self.subTest(code=code):
                connection = Connection(streamed=(fragments,))
                receiver = helper.BoundedReceiveSession(connection, config)
                with self.assertRaisesRegex(helper.AssemblyLimitError, code):
                    await receiver.receive_json()
                self.assertEqual(receiver.owner.used, copied)
                self.assertEqual(connection.yielded, reads)
                self.assertIs(receiver.owner.connection, connection)
                self.assertIs(receiver.owner.buffer, config.storage)
                with self.assertRaises(helper.ReceiveUnavailableError):
                    await receiver.receive_json()
                self.assertEqual(connection.stream_calls, 1)
                self.assertEqual(connection.close_calls, 0)

    async def test_busy_borrow_and_partial_cancellation_preserve_owner(self) -> None:
        entered = asyncio.Event()
        unblock = asyncio.Event()

        class Gated(Connection):
            def recv_streaming(self, decode: Literal[False]) -> AsyncIterator[bytes]:
                assert decode is False
                self.stream_calls += 1

                async def stream() -> AsyncIterator[bytes]:
                    yield b'{"x":'
                    entered.set()
                    await unblock.wait()
                    yield b"1}"

                return stream()

        connection = Gated()
        config = options()
        receiver = helper.BoundedReceiveSession(connection, config)
        task = asyncio.create_task(receiver.receive_json())
        await asyncio.wait_for(entered.wait(), timeout=1)
        with self.assertRaises(helper.ReceiveUnavailableError):
            await receiver.receive_json()
        with self.assertRaises(BufferError):
            config.storage.extend(b"x")
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(receiver.unusable)
        self.assertIs(receiver.owner.connection, connection)
        self.assertIs(receiver.owner.buffer, config.storage)
        self.assertEqual(receiver.owner.used, 5)
        with self.assertRaises(helper.ReceiveUnavailableError):
            await receiver.receive_json()
        self.assertEqual(connection.stream_calls, 1)
        self.assertEqual(connection.close_calls, 0)

    async def test_actual_model_failure_poison_and_context_owns_close(self) -> None:
        raw = b'{"serverContent":{"audioChunks":[{"data":{}}]}}'
        connection = Connection(streamed=((b"setup",), (raw,)))
        self.connections.append(connection)
        async with self.module.connect(model="owned-model", bounded_receive=options()) as session:
            with self.assertRaises((ValueError, TypeError)):
                await anext(session.receive())
            with self.assertRaises(helper.ReceiveUnavailableError):
                await anext(session.receive())
            self.assertEqual(connection.stream_calls, 2)
            self.assertEqual(len(self.connect_calls), 1)
            self.assertEqual(connection.close_calls, 0)
        self.assertEqual(connection.close_calls, 1)

    async def test_partial_source_type_error_does_not_reconnect_or_reuse(self) -> None:
        class Failing(Connection):
            def recv_streaming(self, decode: Literal[False]) -> AsyncIterator[bytes]:
                assert decode is False
                self.stream_calls += 1
                setup = self.stream_calls == 1

                async def stream() -> AsyncIterator[bytes]:
                    if setup:
                        yield b"setup"
                    else:
                        yield b'{"x":'
                        raise TypeError("owned partial source failure")

                return stream()

        connection = Failing()
        self.connections.append(connection)
        async with self.module.connect(model="owned-model", bounded_receive=options()) as session:
            with self.assertRaisesRegex(TypeError, "owned partial source failure"):
                await anext(session.receive())
            with self.assertRaises(helper.ReceiveUnavailableError):
                await anext(session.receive())
            self.assertEqual(len(self.connect_calls), 1)
            self.assertEqual(connection.stream_calls, 2)
            self.assertEqual(connection.close_calls, 0)
        self.assertEqual(connection.close_calls, 1)

    async def test_legacy_decode_fallback_and_explicit_limits(self) -> None:
        class Legacy:
            def __init__(self) -> None:
                self.calls: list[bool] = []

            async def recv(self, *, decode: bool = True) -> str:
                self.calls.append(decode)
                if decode is False:
                    raise TypeError("owned older connection")
                return '{"ok":true}'

        legacy = Legacy()
        self.assertEqual(await helper.receive_json_compat(legacy, None), {"ok": True})
        self.assertEqual(legacy.calls, [False, True])
        with self.assertRaisesRegex(ValueError, "shape_byte_limit_exceeds_receive_allowance"):
            options(shape_limits=music.ShapeLimits(129, 129, 8, 8))


if __name__ == "__main__":
    unittest.main(verbosity=2)
