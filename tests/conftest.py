from pathlib import Path

import pytest

from sobafm.config import Settings
from tests.doubles import FakeClock, FakeLyria


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Run every test in an empty directory, without the developer's SobaFM settings."""
    monkeypatch.chdir(tmp_path)
    for field in Settings.model_fields:
        monkeypatch.delenv(Settings.env_name(field), raising=False)


@pytest.fixture
def lyria() -> FakeLyria:
    return FakeLyria()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()
