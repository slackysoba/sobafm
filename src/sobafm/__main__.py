"""Command-line entry point: `sobafm` or `python -m sobafm`."""

import asyncio
import contextlib
import logging
import signal
import sys

import discord
from pydantic import ValidationError

from sobafm.bot import SobaFM
from sobafm.config import Settings, load_settings
from sobafm.store import Store

# A process supervisor restarts SobaFM if the gateway never becomes ready.
READY_TIMEOUT_S = 120

log = logging.getLogger("sobafm")


def main() -> None:
    try:
        settings = load_settings()
    except ValidationError as error:
        sys.exit(f"sobafm: {describe(error)}")
    except UnicodeDecodeError:
        sys.exit("sobafm: .env could not be read as UTF-8")
    configure_logging(settings.log_level)
    try:
        discord.opus.Encoder()  # loads libopus as voice playback does
    except discord.opus.OpusNotLoaded:
        sys.exit(
            "sobafm: the Opus library could not be loaded; install libopus "
            "(libopus0 on Debian and Ubuntu, opus on Homebrew)"
        )
    try:
        asyncio.run(run(settings))
    except discord.LoginFailure:
        sys.exit("sobafm: Discord rejected DISCORD_TOKEN")
    except KeyboardInterrupt:
        log.info("Stopped")


def configure_logging(level: str) -> None:
    """Apply `level` to SobaFM's own loggers only.

    Libraries stay at INFO: at DEBUG, the websockets library logs request headers, which
    include the Gemini API key. The Google Gen AI SDK logs only warnings, since at INFO it logs
    Lyria RealTime's setup reply verbatim.
    """
    logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger().setLevel(logging.INFO)
    logging.getLogger("google_genai").setLevel(logging.WARNING)
    log.setLevel(level)


async def run(settings: Settings) -> None:
    """Run SobaFM until it is stopped, exiting if the gateway is not ready in time."""
    async with SobaFM(settings, Store(settings.data_dir / "sobafm.db")) as bot:
        loop = asyncio.get_running_loop()
        for stop in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):  # not supported on Windows
                loop.add_signal_handler(stop, lambda: loop.create_task(bot.close()))
        session = asyncio.create_task(bot.start(settings.discord_token.get_secret_value()))
        ready = asyncio.create_task(bot.wait_until_ready())
        await asyncio.wait(
            {session, ready}, timeout=READY_TIMEOUT_S, return_when=asyncio.FIRST_COMPLETED
        )
        if not ready.done():
            ready.cancel()
            if not session.done():
                log.error("Discord gateway not ready after %d seconds", READY_TIMEOUT_S)
                await bot.close()
                sys.exit(1)
        await session


def describe(error: ValidationError) -> str:
    """Name each missing or invalid setting by its environment variable, without its value."""
    problems: list[str] = []
    for detail in error.errors(include_input=False, include_url=False, include_context=False):
        location = str(detail["loc"][0]) if detail["loc"] else ""
        name = Settings.env_name(location) if location in Settings.model_fields else location
        problems.append(f"{name} {'is not set' if detail['type'] == 'missing' else 'is invalid'}")
    return "; ".join(problems)


if __name__ == "__main__":
    main()
