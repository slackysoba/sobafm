"""Check required platform build types in Buildx's .Provenance inspection output."""

import argparse
import json
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import cast


def object_fields(value: object, location: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError(f"{location} must be an object")
    return cast("dict[str, object]", value)


def verify(value: object) -> None:
    platforms = object_fields(value, "Provenance")
    for platform in ("linux/amd64", "linux/arm64"):
        entry = object_fields(platforms.get(platform), platform)
        slsa = object_fields(entry.get("SLSA"), f"{platform}.SLSA")
        # Buildx exposes the predicate, without its statement's predicateType.
        # A present v1 field must be valid; never fall back from malformed v1 to v0.2.
        if "buildDefinition" in slsa:
            definition = object_fields(slsa["buildDefinition"], f"{platform}.SLSA.buildDefinition")
            build_type = definition.get("buildType")
        else:
            build_type = slsa.get("buildType")  # SLSA v0.2
        if not isinstance(build_type, str) or not build_type.strip():
            raise ValueError(f"{platform}.SLSA must have a nonempty string buildType")


@dataclass
class Arguments(argparse.Namespace):
    provenance: Path = Path()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("provenance", type=Path)
    args = parser.parse_args(argv, namespace=Arguments())
    try:
        verify(json.loads(args.provenance.read_text(encoding="utf-8")))
    except (ValueError, OSError) as error:
        print(f"Platform provenance validation failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
