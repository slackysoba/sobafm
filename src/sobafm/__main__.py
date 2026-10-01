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
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    log.info("Configuration loaded")


def describe(error: ValidationError) -> str:
    """Name each missing or invalid setting by its environment variable, without its value."""
    problems: list[str] = []
    for detail in error.errors():
        location = str(detail["loc"][0]) if detail["loc"] else ""
        name = Settings.env_name(location) if location in Settings.model_fields else location
        problem = "is not set" if detail["type"] in {"missing", "too_short"} else "is invalid"
        problems.append(f"{name} {problem}")
    return "; ".join(problems)


if __name__ == "__main__":
    main()
