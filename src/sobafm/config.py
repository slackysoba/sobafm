"""Operator configuration, read from the environment and an optional `.env` file."""

from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

type LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR"]

ENV_PREFIX = "SOBAFM_"


class Settings(BaseSettings):
    """Settings for one SobaFM process. Secrets are `SecretStr`, so they never appear in output."""

    model_config = SettingsConfigDict(
        env_prefix=ENV_PREFIX,
        env_file_encoding="utf-8",
        extra="ignore",
        hide_input_in_errors=True,
    )

    discord_token: SecretStr = Field(validation_alias="DISCORD_TOKEN", min_length=1)
    gemini_api_key: SecretStr = Field(validation_alias="GEMINI_API_KEY", min_length=1)
    log_level: LogLevel = "INFO"

    @classmethod
    def env_name(cls, field: str) -> str:
        """The environment variable that sets `field`."""
        alias = cls.model_fields[field].validation_alias
        return alias if isinstance(alias, str) else f"{ENV_PREFIX}{field.upper()}"


def load_settings(env_file: Path | None = Path(".env")) -> Settings:
    """Read settings from the environment and, when it exists, `env_file`."""
    return Settings(_env_file=env_file)  # pyright: ignore[reportCallIssue] - values come from the environment
