import logging
from pathlib import Path

import pytest

from sobafm.__main__ import main

KEY = "gemini-key-value"


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


def test_debug_logging_applies_to_sobafm_only(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("DISCORD_TOKEN", "token")
    monkeypatch.setenv("GEMINI_API_KEY", KEY)
    monkeypatch.setenv("SOBAFM_LOG_LEVEL", "DEBUG")
    for logger in (logging.getLogger(), logging.getLogger("sobafm")):
        monkeypatch.setattr(logger, "level", logger.level)  # restored after the test

    main()

    assert "Configuration loaded" in caplog.messages
    assert logging.getLogger("sobafm").level == logging.DEBUG
    assert logging.getLogger("websockets").getEffectiveLevel() == logging.INFO
