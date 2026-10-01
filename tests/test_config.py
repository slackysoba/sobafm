from pathlib import Path

import pytest
from pydantic import ValidationError

from sobafm.config import Settings, load_settings

TOKEN = "discord-token-value"
KEY = "gemini-key-value"


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("DISCORD_TOKEN", "GEMINI_API_KEY", "SOBAFM_LOG_LEVEL"):
        monkeypatch.delenv(name, raising=False)


def test_reads_secrets_and_defaults_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DISCORD_TOKEN", TOKEN)
    monkeypatch.setenv("GEMINI_API_KEY", KEY)

    settings = load_settings(env_file=None)

    assert settings.discord_token.get_secret_value() == TOKEN
    assert settings.gemini_api_key.get_secret_value() == KEY
    assert settings.log_level == "INFO"


def test_reads_prefixed_options(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DISCORD_TOKEN", TOKEN)
    monkeypatch.setenv("GEMINI_API_KEY", KEY)
    monkeypatch.setenv("SOBAFM_LOG_LEVEL", "DEBUG")

    assert load_settings(env_file=None).log_level == "DEBUG"


def test_reads_env_file(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(f"DISCORD_TOKEN={TOKEN}\nGEMINI_API_KEY={KEY}\n", encoding="utf-8")

    settings = load_settings(env_file=env_file)

    assert settings.discord_token.get_secret_value() == TOKEN


def test_environment_overrides_env_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(f"DISCORD_TOKEN={TOKEN}\nGEMINI_API_KEY={KEY}\n", encoding="utf-8")
    monkeypatch.setenv("GEMINI_API_KEY", "from-environment")

    assert load_settings(env_file=env_file).gemini_api_key.get_secret_value() == "from-environment"


def test_secrets_never_appear_in_output(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DISCORD_TOKEN", TOKEN)
    monkeypatch.setenv("GEMINI_API_KEY", KEY)

    settings = load_settings(env_file=None)

    for text in (repr(settings), str(settings), settings.model_dump_json()):
        assert TOKEN not in text
        assert KEY not in text


def test_validation_errors_hide_input(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", KEY)

    with pytest.raises(ValidationError) as caught:
        load_settings(env_file=None)

    assert KEY not in str(caught.value)


@pytest.mark.parametrize(
    ("field", "variable"),
    [
        ("discord_token", "DISCORD_TOKEN"),
        ("gemini_api_key", "GEMINI_API_KEY"),
        ("log_level", "SOBAFM_LOG_LEVEL"),
    ],
)
def test_env_name(field: str, variable: str) -> None:
    assert Settings.env_name(field) == variable
