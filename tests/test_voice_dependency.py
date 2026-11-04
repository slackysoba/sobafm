"""Check that the locked Discord voice dependency loads its native voice components.

disnake imports without PyNaCl or dave.py and refuses voice only at connect time
(a logged warning, then RuntimeError from VoiceClient). Discord rejects non-E2EE
voice calls since 2026-03-01 (see docs/decisions/0005-discord-voice-library.md),
so a missing wheel on either CI architecture must fail here rather than surface
later as a voice failure.
This makes no network, gateway or voice connection.
"""

import subprocess
import sys
from pathlib import Path

PROBE = """
import disnake.voice_client as voice
import dave
assert voice.has_nacl, "PyNaCl native module did not load"
assert voice.has_dave, "dave.py native module did not load"
assert "aead_xchacha20_poly1305_rtpsize" in voice.VoiceClient.supported_modes
assert dave.get_max_supported_protocol_version() >= 1
"""


def test_installed_voice_dependency_loads_native_components(tmp_path: Path) -> None:
    result = subprocess.run(
        [sys.executable, "-I", "-c", PROBE],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
