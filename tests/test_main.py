import asyncio
import logging
import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Self
from unittest.mock import MagicMock

import discord
import pytest
from google.genai import live_music

import sobafm.__main__
from sobafm.__main__ import main, run
from sobafm.config import Settings, load_settings

KEY = "gemini-key-value"


@pytest.fixture(autouse=True)
def opus(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Stand in for libopus, so the tests do not depend on the system's copy."""
    encoder = MagicMock()
    monkeypatch.setattr(discord.opus, "Encoder", encoder)
    return encoder


@pytest.fixture(autouse=True)
def logging_levels() -> Iterator[None]:
    """Restore the levels `main()` sets, so other tests keep the defaults."""
    loggers = [logging.getLogger(name) for name in ("", "sobafm", "google_genai")]
    levels = [logger.level for logger in loggers]
    yield
    for logger, level in zip(loggers, levels, strict=True):
        logger.setLevel(level)
    logging.captureWarnings(False)  # pytest restores the filters, but not this


@pytest.fixture
def started(monkeypatch: pytest.MonkeyPatch) -> list[Settings]:
    """Replace the bot's run loop, recording the settings it would have started with."""
    runs: list[Settings] = []

    async def run(settings: Settings) -> None:
        runs.append(settings)

    monkeypatch.setattr(sobafm.__main__, "run", run)
    return runs


def test_exits_naming_missing_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", KEY)

    with pytest.raises(SystemExit) as caught:
        main()

    assert caught.value.code == "sobafm: DISCORD_TOKEN is not set"


def test_treats_empty_secrets_as_not_set(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("DISCORD_TOKEN=\nGEMINI_API_KEY=\n", encoding="utf-8")

    with pytest.raises(SystemExit) as caught:
        main()

    assert caught.value.code == "sobafm: DISCORD_TOKEN is not set; GEMINI_API_KEY is not set"


def test_exits_on_invalid_option(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DISCORD_TOKEN", "token")
    monkeypatch.setenv("GEMINI_API_KEY", KEY)
    monkeypatch.setenv("SOBAFM_LOG_LEVEL", "LOUD")

    with pytest.raises(SystemExit) as caught:
        main()

    assert caught.value.code == "sobafm: SOBAFM_LOG_LEVEL is invalid"
    assert KEY not in str(caught.value.code)


def test_exits_naming_a_malformed_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DISCORD_TOKEN", "token")
    monkeypatch.setenv("GEMINI_API_KEY", f"\u201c{KEY}\u201d")  # pasted with curly quotes

    with pytest.raises(SystemExit) as caught:
        main()

    assert caught.value.code == "sobafm: GEMINI_API_KEY is invalid"


def test_exits_when_env_file_is_not_utf8(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("DISCORD_TOKEN=token\n", encoding="utf-16")

    with pytest.raises(SystemExit) as caught:
        main()

    assert caught.value.code == "sobafm: .env could not be read as UTF-8"


def test_runs_the_bot_with_valid_configuration(
    monkeypatch: pytest.MonkeyPatch, opus: MagicMock, started: list[Settings]
) -> None:
    monkeypatch.setenv("DISCORD_TOKEN", "token")
    monkeypatch.setenv("GEMINI_API_KEY", KEY)

    main()

    opus.assert_called_once_with()  # no arguments, so only loading the library can fail
    assert [settings.discord_token.get_secret_value() for settings in started] == ["token"]


def test_exits_when_the_opus_library_is_missing(
    monkeypatch: pytest.MonkeyPatch, opus: MagicMock, started: list[Settings]
) -> None:
    monkeypatch.setenv("DISCORD_TOKEN", "token")
    monkeypatch.setenv("GEMINI_API_KEY", KEY)
    opus.side_effect = discord.opus.OpusNotLoaded

    with pytest.raises(SystemExit) as caught:
        main()

    assert caught.value.code == (
        "sobafm: the Opus library could not be loaded; install libopus "
        "(libopus0 on Debian and Ubuntu, opus on Homebrew)"
    )
    assert not started


def test_exits_when_discord_rejects_the_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DISCORD_TOKEN", "token")
    monkeypatch.setenv("GEMINI_API_KEY", KEY)

    async def rejected(settings: Settings) -> None:
        raise discord.LoginFailure

    monkeypatch.setattr(sobafm.__main__, "run", rejected)

    with pytest.raises(SystemExit) as caught:
        main()

    assert caught.value.code == "sobafm: Discord rejected DISCORD_TOKEN"


def test_debug_logging_applies_to_sobafm_only(
    monkeypatch: pytest.MonkeyPatch, started: list[Settings]
) -> None:
    monkeypatch.setenv("DISCORD_TOKEN", "token")
    monkeypatch.setenv("GEMINI_API_KEY", KEY)
    monkeypatch.setenv("SOBAFM_LOG_LEVEL", "DEBUG")

    main()

    assert logging.getLogger("sobafm").level == logging.DEBUG
    assert logging.getLogger("websockets").getEffectiveLevel() == logging.INFO
    # the SDK's own logger, which logs Lyria's setup reply at INFO
    assert not live_music.logger.isEnabledFor(logging.INFO)
    assert live_music.logger.isEnabledFor(logging.WARNING)


def run_with_logging(code: str, **env: str) -> subprocess.CompletedProcess[str]:
    """Run `code` after SobaFM's logging setup, in a fresh interpreter.

    pytest's own logging handlers would keep basicConfig() idle in this process, and inherited
    warning settings would add output of their own.
    """
    script = (
        "import logging\nfrom sobafm.__main__ import configure_logging\n"
        f"configure_logging('INFO')\n{code}"
    )
    child = {k: v for k, v in os.environ.items() if k not in {"PYTHONWARNINGS", "PYTHONDEVMODE"}}
    return subprocess.run(  # noqa: S603 - a fixed script, run by this interpreter
        [sys.executable, "-c", script],
        capture_output=True,
        encoding="ascii",  # explicit, as the child's records are escaped to ASCII here
        errors="backslashreplace",
        check=True,
        timeout=60,
        env=child | env,
    )


def test_logs_to_standard_output() -> None:
    logged = run_with_logging("logging.getLogger('sobafm.bot').info('Connected')")

    assert logged.stdout.rstrip().endswith("INFO sobafm.bot: Connected")
    assert logged.stderr == ""


def test_escapes_what_standard_output_cannot_encode() -> None:
    # As when output is redirected on Windows, whose locale encoding has no emoji.
    logged = run_with_logging(
        "logging.getLogger('sobafm.bot').info('Left voice in \\U0001f3b5 Lounge')",
        PYTHONIOENCODING="ascii",
    )

    assert logged.stdout.rstrip().endswith("INFO sobafm.bot: Left voice in \\U0001f3b5 Lounge")
    assert logged.stderr == ""


def test_logs_warnings_to_standard_output() -> None:
    logged = run_with_logging(
        "import warnings; warnings.warn('Something to know', stacklevel=1); "
        # The SDK's enum warning is ignored only from the module that raises it.
        "warnings.warn_explicit('0 is not a valid count', UserWarning, 'types.py', 1, "
        "module='google.genai.types')"
    )

    assert "WARNING py.warnings:" in logged.stdout
    assert "UserWarning: Something to know" in logged.stdout
    assert "UserWarning: 0 is not a valid count" in logged.stdout
    assert logged.stderr == ""


# Unknown enum values, one spanning lines, and the first Lyria RealTime connection
SDK_WARNINGS = (
    "from google.genai import types; from sobafm.deck import lyria; "
    "types.Scale('AIzaFakeKey'); types.Scale('AIzaFakeKey\\nmore'); lyria('placeholder-key')()"
)


@pytest.mark.parametrize("option", [None, "default"], ids=["no option", "PYTHONWARNINGS"])
def test_ignores_the_sdks_warnings_that_quote_what_it_receives(option: str | None) -> None:
    logged = run_with_logging(SDK_WARNINGS, **({"PYTHONWARNINGS": option} if option else {}))

    assert "AIzaFakeKey" not in logged.stdout + logged.stderr
    assert "experimental" not in logged.stdout + logged.stderr
    if option is None:  # otherwise, imports warn before the setup
        assert logged.stderr == ""


# A deck over the SDK's own session, receiving one frame whose metadata has unknown values
UNKNOWN_VALUES_FROM_LYRIA = """
import asyncio, base64, contextlib, json, types
from google.genai import live_music
from sobafm.deck import Deck
from sobafm.plan import MusicPlan

config = {"scale": "AIzaFakeKey\\nmore", "musicGenerationMode": "AIzaFakeKey"}
chunk = {
    "data": base64.b64encode(bytes(3840)).decode(),
    "mimeType": "audio/l16;rate=48000;channels=2",
    "sourceMetadata": {"musicGenerationConfig": config},
}
frames = [json.dumps({"serverContent": {"audioChunks": [chunk]}}).encode()]


class Socket:
    async def send(self, message):
        pass

    async def recv(self, decode=None):
        if not frames:
            await asyncio.Event().wait()
        return frames.pop()


@contextlib.asynccontextmanager
async def connect():
    client = types.SimpleNamespace(vertexai=False)
    yield live_music.AsyncMusicSession(api_client=client, websocket=Socket())


async def run():
    deck = Deck(connect, MusicPlan.from_request("ambient"))
    deck.start()
    for _ in range(200):
        await asyncio.sleep(0.01)
        if deck.frames:
            break
    print("deck:", deck.state, len(deck.frames))


asyncio.run(run())
"""


def test_parses_a_message_from_lyria_without_quoting_its_unknown_values() -> None:
    logged = run_with_logging(UNKNOWN_VALUES_FROM_LYRIA)

    assert "deck: generating 1" in logged.stdout  # it keeps the audio and keeps generating
    assert "AIzaFakeKey" not in logged.stdout + logged.stderr
    assert logged.stderr == ""


class NeverReady:
    """A client whose gateway never becomes ready; `close()` ends its session."""

    def __init__(self, *_: object) -> None:
        self.closed = asyncio.Event()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_: object) -> None:
        pass

    async def start(self, token: str) -> None:
        await self.closed.wait()

    async def wait_until_ready(self) -> None:
        await asyncio.Event().wait()

    async def close(self) -> None:
        self.closed.set()


async def test_exits_when_the_gateway_is_not_ready_in_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DISCORD_TOKEN", "token")
    monkeypatch.setenv("GEMINI_API_KEY", KEY)
    clients: list[NeverReady] = []

    def client(*args: object) -> NeverReady:
        clients.append(NeverReady(*args))
        return clients[-1]

    monkeypatch.setattr(sobafm.__main__, "SobaFM", client)
    monkeypatch.setattr(sobafm.__main__, "READY_TIMEOUT_S", 0.01)

    with pytest.raises(SystemExit) as caught:
        await run(load_settings(env_file=None))

    assert caught.value.code == 1
    assert clients[0].closed.is_set()
