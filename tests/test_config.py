from pathlib import Path

import pytest
from pydantic import ValidationError

from sobafm.config import Settings, load_settings

TOKEN = "discord-token-value"
KEY = "gemini-key-value"


def write_env_file(path: Path, text: str) -> Path:
    env_file = path / ".env"
    env_file.write_text(text, encoding="utf-8")
    return env_file


def test_reads_secrets_and_defaults_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DISCORD_TOKEN", TOKEN)
    monkeypatch.setenv("GEMINI_API_KEY", KEY)

    settings = load_settings(env_file=None)

    assert settings.discord_token.get_secret_value() == TOKEN
    assert settings.gemini_api_key.get_secret_value() == KEY
    assert settings.log_level == "INFO"
    assert settings.gemini_model == "gemini-3.5-flash-lite"


def test_reads_prefixed_options_case_insensitively(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DISCORD_TOKEN", TOKEN)
    monkeypatch.setenv("GEMINI_API_KEY", KEY)
    monkeypatch.setenv("SOBAFM_LOG_LEVEL", "debug")

    assert load_settings(env_file=None).log_level == "DEBUG"


def test_reads_env_file(tmp_path: Path) -> None:
    env_file = write_env_file(tmp_path, f"DISCORD_TOKEN={TOKEN}\nGEMINI_API_KEY={KEY}\n")

    assert load_settings(env_file=env_file).discord_token.get_secret_value() == TOKEN


def test_environment_overrides_env_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    env_file = write_env_file(tmp_path, f"DISCORD_TOKEN={TOKEN}\nGEMINI_API_KEY={KEY}\n")
    monkeypatch.setenv("GEMINI_API_KEY", "from-environment")

    assert load_settings(env_file=env_file).gemini_api_key.get_secret_value() == "from-environment"


def test_ignores_empty_environment_variables(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    env_file = write_env_file(tmp_path, f"DISCORD_TOKEN={TOKEN}\nGEMINI_API_KEY={KEY}\n")
    monkeypatch.setenv("DISCORD_TOKEN", "")

    assert load_settings(env_file=env_file).discord_token.get_secret_value() == TOKEN


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


def test_reads_server_options(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("DISCORD_TOKEN", TOKEN)
    monkeypatch.setenv("GEMINI_API_KEY", KEY)
    defaults = load_settings(env_file=None)
    assert (defaults.data_dir, defaults.dev_guild_id, defaults.max_sessions) == (
        Path("data"),
        None,
        4,
    )

    monkeypatch.setenv("SOBAFM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SOBAFM_DEV_GUILD_ID", "1234")
    monkeypatch.setenv("SOBAFM_MAX_SESSIONS", "8")
    settings = load_settings(env_file=None)

    assert (settings.data_dir, settings.dev_guild_id, settings.max_sessions) == (tmp_path, 1234, 8)


@pytest.mark.parametrize("cap", ["1", "3"])
def test_rejects_a_session_cap_that_is_not_whole_programs(
    monkeypatch: pytest.MonkeyPatch, cap: str
) -> None:
    monkeypatch.setenv("DISCORD_TOKEN", TOKEN)
    monkeypatch.setenv("GEMINI_API_KEY", KEY)
    monkeypatch.setenv("SOBAFM_MAX_SESSIONS", cap)

    with pytest.raises(ValidationError):
        load_settings(env_file=None)


@pytest.mark.parametrize(
    ("field", "variable"),
    [
        ("discord_token", "DISCORD_TOKEN"),
        ("gemini_api_key", "GEMINI_API_KEY"),
        ("gemini_model", "SOBAFM_GEMINI_MODEL"),
        ("log_level", "SOBAFM_LOG_LEVEL"),
        ("data_dir", "SOBAFM_DATA_DIR"),
        ("dev_guild_id", "SOBAFM_DEV_GUILD_ID"),
        ("max_sessions", "SOBAFM_MAX_SESSIONS"),
    ],
)
def test_env_name(field: str, variable: str) -> None:
    assert Settings.env_name(field) == variable
