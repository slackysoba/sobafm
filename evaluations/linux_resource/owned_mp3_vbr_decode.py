"""Opt-in decode of the single generated, authenticated 44.1 kHz mono VBR fixture."""

from __future__ import annotations

import argparse
from pathlib import Path

from .mp3_vbr_contract import gapless_output_frames
from .owned_mp3_decode import DecodeFixture, decode_fixture
from .probe import ProbeError, emit, observe

# Generated once under the recorded issue #7 release; no arbitrary input binding.
FIXTURE = DecodeFixture(
    path=Path("/fixtures/owned-vbr-44100-mono-120s.mp3"),
    file_bytes=916_598,
    sha256=(
        # pragma: allowlist nextline secret
        "ceec82e5c69025593e17afe0d8f327036258f2afc93bbbbc961a87eba507595e"
    ),
    pcm_bytes=gapless_output_frames(4595, 576, 864) * 4,
    output_copy_offset=3_000_000,
    format_line="MPEG 1.0 L III vbr 44100 mono",
    mono_output=True,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute-owned-vbr-decode", action="store_true", required=True)
    parser.parse_args()
    try:
        emit({"event": "owned_decode_preflight", "observation": observe()})
        result = decode_fixture(FIXTURE)
        emit(result)
        return 0 if result["verdict"] == "owned_fixture_decode_pass" else 1
    except (OSError, ProbeError, ValueError) as error:
        emit({"event": "owned_decode_incomplete", "error": str(error)})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
