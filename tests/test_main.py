import asyncio
import logging
from pathlib import Path
from typing import Self

import discord
import pytest

import sobafm.__main__
from sobafm.__main__ import main, run
from sobafm.config import Settings, load_settings

KEY = "gemini-key-value"


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


def test_exits_when_env_file_is_not_utf8(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("DISCORD_TOKEN=token\n", encoding="utf-16")

    with pytest.raises(SystemExit) as caught:
        main()

    assert caught.value.code == "sobafm: .env could not be read as UTF-8"


def test_runs_the_bot_with_valid_configuration(
    monkeypatch: pytest.MonkeyPatch, started: list[Settings]
) -> None:
    monkeypatch.setenv("DISCORD_TOKEN", "token")
    monkeypatch.setenv("GEMINI_API_KEY", KEY)

    main()

    assert [settings.discord_token.get_secret_value() for settings in started] == ["token"]


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
    for logger in (logging.getLogger(), logging.getLogger("sobafm")):
        monkeypatch.setattr(logger, "level", logger.level)  # restored after the test

    main()

    assert logging.getLogger("sobafm").level == logging.DEBUG
    assert logging.getLogger("websockets").getEffectiveLevel() == logging.INFO


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
