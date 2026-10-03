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


def log_in_child(message: str, **env: str) -> subprocess.CompletedProcess[str]:
    """Log `message`, a Python string literal, through SobaFM's logging in a fresh interpreter.

    pytest's own logging handlers would keep basicConfig() idle in this process, and inherited
    warning settings would add output of their own.
    """
    script = (
        "import logging; from sobafm.__main__ import configure_logging; "
        f"configure_logging('INFO'); logging.getLogger('sobafm.bot').info({message})"
    )
    child = {k: v for k, v in os.environ.items() if k not in {"PYTHONWARNINGS", "PYTHONDEVMODE"}}
    return subprocess.run(  # noqa: S603 - a fixed script, run by this interpreter
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
        env=child | env,
    )


def test_logs_to_standard_output() -> None:
    logged = log_in_child("'Connected'")

    assert logged.stdout.rstrip().endswith("INFO sobafm.bot: Connected")
    assert logged.stderr == ""


def test_escapes_what_standard_output_cannot_encode() -> None:
    # As when output is redirected on Windows, whose locale encoding has no emoji.
    logged = log_in_child("'Left voice in \\U0001f3b5 Lounge'", PYTHONIOENCODING="ascii")

    assert logged.stdout.rstrip().endswith("INFO sobafm.bot: Left voice in \\U0001f3b5 Lounge")
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
