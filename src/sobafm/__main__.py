"""Command-line entry point: `sobafm` or `python -m sobafm`."""

import logging
import sys

from pydantic import ValidationError

from sobafm.config import Settings, load_settings

log = logging.getLogger("sobafm")


def main() -> None:
    try:
        settings = load_settings()
    except ValidationError as error:
        sys.exit(f"sobafm: {describe(error)}")
    except UnicodeDecodeError:
        sys.exit("sobafm: .env could not be read as UTF-8")
    configure_logging(settings.log_level)
    log.info("Configuration loaded")


def configure_logging(level: str) -> None:
    """Apply `level` to SobaFM's own loggers only.

    Libraries stay at INFO: at DEBUG, the websockets library logs request headers, which
    include the Gemini API key.
    """
    logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger().setLevel(logging.INFO)
    log.setLevel(level)


def describe(error: ValidationError) -> str:
    """Name each missing or invalid setting by its environment variable, without its value."""
    problems: list[str] = []
    for detail in error.errors(include_input=False, include_url=False, include_context=False):
        location = str(detail["loc"][0]) if detail["loc"] else ""
        name = Settings.env_name(location) if location in Settings.model_fields else location
        problems.append(f"{name} {'is not set' if detail['type'] == 'missing' else 'is invalid'}")
    return "; ".join(problems)


if __name__ == "__main__":
    main()
