"""Finite synthetic TLS WebSocket peer for L2. Run outside the measured cgroup."""

import argparse
import asyncio
import base64
import json
import logging
import socket
import ssl
from pathlib import Path

from aiohttp import web

from sobafm.lyria import PLAY_MESSAGE, SETUP_MESSAGE, MusicRequest, Prompt
from sobafm.pcm import BYTES_PER_SECOND


async def serve(cert: Path, key: Path, port: int) -> None:
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(cert, key)
    counts: dict[int, int] = {}
    music = MusicRequest((Prompt("fixture"),), bpm=90)

    async def handle(request: web.Request) -> web.WebSocketResponse:
        pair = int(request.match_info["pair"])
        number = counts.get(pair, 0) + 1
        if number > 2:
            raise web.HTTPForbidden()
        counts[pair] = number
        ws = web.WebSocketResponse(max_msg_size=4096, compress=False)
        await ws.prepare(request)
        if await ws.receive_json() != SETUP_MESSAGE:
            raise ValueError("setup")
        await ws.send_json({"setupComplete": {}})
        for expected in (music.prompts_message(), music.config_message(), PLAY_MESSAGE):
            if await ws.receive_json() != expected:
                raise ValueError("control")
        # Two seconds per message. Distinct samples identify each side of the handoff.
        pcm = (1000 * number).to_bytes(2, "little") * BYTES_PER_SECOND
        chunk = {
            "data": base64.b64encode(pcm).decode("ascii"),
            "mimeType": "audio/l16;rate=48000;channels=2",
        }
        message = json.dumps({"serverContent": {"audioChunks": [chunk]}})
        for _ in range(8):
            await ws.send_str(message)
        # Leave the full bank and paused real reader under pressure until client close.
        await ws.receive()
        return ws

    app = web.Application()
    app.router.add_get(r"/{pair:[1-5]}", handle)
    runner = web.AppRunner(app, access_log=None, shutdown_timeout=1)
    await runner.setup()
    with socket.socket() as sock:
        try:
            sock.bind(("127.0.0.1", port))
            await web.SockSite(runner, sock, ssl_context=tls).start()
            print(f"ready {sock.getsockname()[1]}", flush=True)
            await asyncio.sleep(120)
        finally:
            await runner.cleanup()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("cert", type=Path)
    parser.add_argument("key", type=Path)
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    logging.disable(logging.CRITICAL)
    asyncio.run(serve(args.cert, args.key, args.port))
