"""Non-PCM worker bootstrap; the finite fit check uses only owned fake I/O.

Keep this entry module light: dependency initialization belongs inside the
creation-time Job, and a failed import must produce a fixed, non-sensitive result.
No production admission or complete footprint profile is supplied here.
"""

from __future__ import annotations

import asyncio
import sys
from typing import BinaryIO

REQUEST = b"\x01"
FRAME_SIZE = 3840
BOOTED = b"B"
IMPORTED = b"I"
ALLOCATION_FAILED = b"M"
BOOTSTRAP_FAILED = b"E"


async def fake_receive() -> bytes:
    """Exercise the installed SDK's bounded JSON/model/audio conversion offline."""
    import base64
    import json
    from collections.abc import AsyncGenerator, AsyncIterator
    from types import SimpleNamespace
    from typing import Any, Literal, cast

    from google.genai import live_music, types
    from google.genai._bounded_receive import BoundedReceiveOptions, BoundedReceiveSession

    # The intended worker owns both producer families. Import their real current
    # dependencies here without opening HTTP, a decoder, a socket or credentials.
    from sobafm.catalog_audio import AudioStream

    if not callable(AudioStream):
        raise RuntimeError("catalog bootstrap unavailable")
    frame = json.dumps(
        {
            "serverContent": {
                "audioChunks": [
                    {
                        "data": base64.b64encode(bytes(FRAME_SIZE)).decode(),
                        "mimeType": "audio/l16;rate=48000;channels=2",
                    }
                ]
            }
        },
        separators=(",", ":"),
    ).encode()

    class Socket:
        async def recv_streaming(self, decode: Literal[False]) -> AsyncIterator[bytes]:
            yield frame

    socket = Socket()
    options = BoundedReceiveOptions(
        storage=bytearray(524288),
        max_bytes=524288,
        max_fragments=1024,
        max_empty_fragments=16,
        max_size=524288,
        max_queue=1,
        input_storage=bytearray(4096),
    )
    receiver = BoundedReceiveSession(socket, options)
    session = live_music.AsyncMusicSession(
        api_client=cast(Any, SimpleNamespace(vertexai=False)),
        websocket=cast(Any, socket),
        bounded_receiver=receiver,
    )
    stream = cast(AsyncGenerator[types.LiveMusicServerMessage], session.receive())
    try:
        message = await anext(stream)
        content = message.server_content
        if content is None or content.audio_chunks is None or len(content.audio_chunks) != 1:
            raise RuntimeError("unexpected fake result")
        pcm = content.audio_chunks[0].data
        if pcm is None or pcm != bytes(FRAME_SIZE):
            raise RuntimeError("unexpected fake PCM")
        return pcm
    finally:
        await stream.aclose()


def bootstrap(source: BinaryIO, sink: BinaryIO) -> int:
    """One fixed request and one frame; never accept commands or provider input."""
    sink.write(BOOTED)
    sink.flush()
    try:
        if source.read(2) != REQUEST:
            return 72
        pcm = asyncio.run(fake_receive())
        sink.write(IMPORTED + pcm)
        sink.flush()
        return 0
    except MemoryError:
        sink.write(ALLOCATION_FAILED)
        sink.flush()
        return 70
    except Exception:  # noqa: BLE001 - fixed code, never serialize errors or raw input
        sink.write(BOOTSTRAP_FAILED)
        sink.flush()
        return 71


def main() -> int:
    if sys.argv[1:] != ["--bootstrap-check"]:
        return 72
    return bootstrap(sys.stdin.buffer, sys.stdout.buffer)


if __name__ == "__main__":
    raise SystemExit(main())
