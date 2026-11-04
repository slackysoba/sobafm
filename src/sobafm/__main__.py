"""Run SobaFM: check the container limits, read configuration, then play.

Startup order is fixed. The limit gate runs first and fails closed, so no
catalog, media, Discord or audio input is read unless the service runs under
the ADR 0004 whole-service limits. Configuration then comes from environment
variables only, and the decode-backed producer is connected to the adapter.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sobafm.discord_adapter import CATALOG_VARIABLE, Config, ConfigError, Producer, run
from sobafm.media import Media, MediaInputError, MediaProducer, load_media

log = logging.getLogger("sobafm")

MEDIA_VARIABLE = "SOBAFM_MEDIA"
CGROUP_ROOT = Path("/sys/fs/cgroup")
# ADR 0004: the whole-service group memory, no swap and 64 tasks.
REQUIRED_LIMITS = (("memory.max", "90177536"), ("memory.swap.max", "0"), ("pids.max", "64"))
MAX_LIMIT_FILE_BYTES = 64

EXIT_CONFIG = 2
EXIT_LIMITS = 3


class LimitError(Exception):
    """The effective container limits do not match ADR 0004; names only, no paths."""


def check_limits(root: Path = CGROUP_ROOT) -> None:
    """Refuse unless the cgroup v2 limits seen by this process are exactly ADR 0004's.

    Docker's default private cgroup namespace shows the container's own group at
    ``root``. A missing, unreadable or different value refuses startup.
    """
    for name, required in REQUIRED_LIMITS:
        try:
            with (root / name).open("rb") as file:
                raw = file.read(MAX_LIMIT_FILE_BYTES + 1)
        except OSError as exc:
            raise LimitError(f"{name} is unreadable") from exc
        if len(raw) > MAX_LIMIT_FILE_BYTES or raw.strip() != required.encode():
            raise LimitError(f"{name} must be {required}")


@dataclass(frozen=True)
class Settings:
    config: Config
    media: Media


def load_settings() -> Settings:
    """Read the token, catalog, reviews and media manifest from the environment."""
    config = Config.from_environ()
    location = os.environ.get(MEDIA_VARIABLE, "")
    if config.catalog is None:
        if location:
            raise ConfigError(f"{MEDIA_VARIABLE} requires {CATALOG_VARIABLE}")
        return Settings(config, {})
    if not location:
        raise ConfigError(f"{MEDIA_VARIABLE} is required with {CATALOG_VARIABLE}")
    try:
        media = load_media(Path(location))
    except (MediaInputError, OSError, UnicodeError) as exc:
        raise ConfigError("media manifest could not be loaded") from exc
    return Settings(config, media)


type Runner = Callable[[Config, Producer], Coroutine[Any, Any, int]]


def main(*, root: Path = CGROUP_ROOT, runner: Runner = run) -> int:
    """Gate, configure and run; return the process exit status."""
    logging.basicConfig(
        level=logging.INFO, stream=sys.stdout, format="%(levelname)s %(name)s %(message)s"
    )
    try:
        check_limits(root)
    except LimitError as exc:
        log.error("container limits refused: %s", exc)
        return EXIT_LIMITS
    try:
        settings = load_settings()
    except ConfigError as exc:
        log.error("configuration error: %s", exc)
        return EXIT_CONFIG
    return asyncio.run(runner(settings.config, MediaProducer(settings.media)))


if __name__ == "__main__":
    sys.exit(main())
