"""Operator configuration, read from the environment and an optional `.env` file."""

from pathlib import Path
from typing import Annotated, Literal

from pydantic import BeforeValidator, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

type LogLevel = Annotated[
    Literal["DEBUG", "INFO", "WARNING", "ERROR"], BeforeValidator(lambda value: str(value).upper())
]

ENV_PREFIX = "SOBAFM_"


class Settings(BaseSettings):
    """Settings for one SobaFM process. Secrets are `SecretStr`, so they never appear in output."""

    model_config = SettingsConfigDict(
        env_prefix=ENV_PREFIX,
        env_file_encoding="utf-8",
        env_ignore_empty=True,
        extra="ignore",
        hide_input_in_errors=True,
    )

    discord_token: SecretStr = Field(validation_alias="DISCORD_TOKEN", min_length=1)
    gemini_api_key: SecretStr = Field(validation_alias="GEMINI_API_KEY", min_length=1)
    log_level: LogLevel = "INFO"
    data_dir: Path = Path("data")
    dev_guild_id: int | None = None
    max_sessions: int = Field(default=4, ge=2, multiple_of=2)  # each playing server uses two

    @classmethod
    def env_name(cls, field: str) -> str:
        """The environment variable that sets `field`."""
        alias = cls.model_fields[field].validation_alias
        return alias if isinstance(alias, str) else f"{ENV_PREFIX}{field.upper()}"


def load_settings(env_file: Path | None = Path(".env")) -> Settings:
    """Read settings from the environment and, when it exists, `env_file`."""
    return Settings(_env_file=env_file)  # pyright: ignore[reportCallIssue] - values come from the environment
