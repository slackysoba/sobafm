"""Offline JSON-and-prompt preview of reviewed catalog metadata; no playback."""

import argparse
import json
from dataclasses import asdict
from pathlib import Path

from sobafm.catalog import (
    MAX_CATALOG_BYTES,
    CatalogInputError,
    parse_catalog,
    parse_reviews,
    select,
)
from sobafm.catalog_prompt import normalize_prompt


def _read(path: Path, maximum: int) -> str:
    with path.open("rb") as file:
        payload = file.read(maximum + 1)
    if len(payload) > maximum:
        raise CatalogInputError("input file exceeds byte limit")
    try:
        return payload.decode("utf-8")
    except UnicodeError as exc:
        raise CatalogInputError("input file must be UTF-8") from exc


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("prompt")
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument(
        "--reviews",
        type=Path,
        required=True,
        help="separate trusted admission decisions; never provider/model output",
    )
    parser.add_argument("--limit", type=int, default=5)
    args = parser.parse_args()
    try:
        catalog = parse_catalog(_read(args.catalog, MAX_CATALOG_BYTES))
        reviews = parse_reviews(_read(args.reviews, 256_000))
        interpretation = normalize_prompt(args.prompt)
        result = (
            None
            if interpretation.query is None
            else select(
                catalog,
                interpretation.query,
                reviews,
                limit=args.limit,
            )
        )
        output = {
            "synthetic": catalog.synthetic,
            "notice": (
                "Offline prepared metadata only; no audio, live source search "
                "or music-quality verdict."
            ),
            "interpretation": asdict(interpretation),
            "selection": None if result is None else asdict(result),
        }
        print(json.dumps(output, ensure_ascii=False, indent=2))
    except (CatalogInputError, OSError) as exc:
        parser.error(str(exc) if isinstance(exc, CatalogInputError) else "input file unavailable")


if __name__ == "__main__":
    main()
