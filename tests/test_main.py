import logging
from pathlib import Path

import pytest

from sobafm.__main__ import main

KEY = "gemini-key-value"


@pytest.fixture(autouse=True)
def isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Run each test in an empty directory, so a developer's `.env` is never read."""
    monkeypatch.chdir(tmp_path)
    for name in ("DISCORD_TOKEN", "GEMINI_API_KEY", "SOBAFM_LOG_LEVEL"):
        monkeypatch.delenv(name, raising=False)


def test_exits_naming_missing_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", KEY)

    with pytest.raises(SystemExit) as caught:
        main()

    message = str(caught.value.code)
    assert message == "sobafm: DISCORD_TOKEN is not set"
    assert KEY not in message


def test_treats_empty_secrets_as_not_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DISCORD_TOKEN", "")
    monkeypatch.setenv("GEMINI_API_KEY", "")

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


def test_starts_with_valid_configuration(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("DISCORD_TOKEN", "token")
    monkeypatch.setenv("GEMINI_API_KEY", KEY)

    with caplog.at_level(logging.INFO, logger="sobafm"):
        main()

    assert "Configuration loaded" in caplog.messages
