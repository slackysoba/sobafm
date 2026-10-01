from pathlib import Path

import pytest

from sobafm.config import Settings


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Run every test in an empty directory, without the developer's SobaFM settings."""
    monkeypatch.chdir(tmp_path)
    for field in Settings.model_fields:
        monkeypatch.delenv(Settings.env_name(field), raising=False)
