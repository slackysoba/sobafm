"""Pending owned FLAC decode; execution remains closed until exact evidence is bound."""

from __future__ import annotations

import argparse
import re
from dataclasses import dataclass
from pathlib import Path

from .flac_contract import (
    ARGV,
    BANNER,
    MAX_FILE_BYTES,
    RAW_BYTES,
    RAW_SHA256,
    checksum,
    inspect_fixture,
    validate_diagnostics,
)
from .owned_mp3_decode import DecodeFixture, NativeDecoder, decode_fixture
from .probe import ProbeError, emit, observe


@dataclass(frozen=True)
class FlacBinding:
    image_id: str
    review_url: str
    decoder: NativeDecoder
    fixture: DecodeFixture
    raw_md5: str
    metadata_sha256: str

    def __post_init__(self) -> None:
        if (
            not re.fullmatch(r"sha256:[0-9a-f]{64}", self.image_id)
            or not self.review_url.startswith("https://github.com/slackysoba/sobafm/")
            or self.decoder.argv != ARGV
            or self.decoder.banner != BANNER
            or self.decoder.diagnostics is not validate_diagnostics
            or self.fixture.path != Path("/fixtures/owned-48000-stereo-120s.flac")
            or not 42 < self.fixture.file_bytes <= MAX_FILE_BYTES
            or not checksum(self.fixture.sha256, 64)
            or self.fixture.pcm_bytes != RAW_BYTES
            or self.fixture.pcm_sha256 != RAW_SHA256
            or self.fixture.output_copy_offset != MAX_FILE_BYTES
            or self.fixture.format_line != ""
            or self.fixture.mono_output
            or not checksum(self.raw_md5, 32)
            or not checksum(self.metadata_sha256, 64)
        ):
            raise ProbeError("incomplete or changed owned FLAC evidence binding")

    def validate_input(self, payload: memoryview) -> None:
        header = inspect_fixture(payload, self.raw_md5)
        if header.metadata_sha256 != self.metadata_sha256:
            raise ProbeError("FLAC metadata differs from the owned generation receipt")


# No actual installed image/ELF closure, generated file hash or version acceptance is invented.
# Populate only in a separately reviewed source change after those receipts exist.
BINDING: FlacBinding | None = None


def run_bound_decode() -> dict[str, object]:
    binding = BINDING
    if binding is None:
        raise ProbeError("FLAC installed image/native/fixture execution binding is not available")
    emit({"event": "owned_flac_preflight", "observation": observe()})
    result = decode_fixture(
        binding.fixture, decoder=binding.decoder, validate_input=binding.validate_input
    )
    return result | {"bound_image_id": binding.image_id, "review_url": binding.review_url}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute-owned-flac-decode", action="store_true", required=True)
    parser.parse_args()
    try:
        result = run_bound_decode()
        emit(result)
        return 0 if result["verdict"] == "owned_fixture_decode_pass" else 1
    except (OSError, ProbeError, ValueError) as error:
        emit({"event": "owned_flac_incomplete", "error": str(error)})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
