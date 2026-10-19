from pathlib import Path

import pytest

from scripts.soak import report


def test_catalog_summary_uses_existing_soak_shape(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Synthetic parser input is not a live/soak qualification result.
    (tmp_path / "sobafm.log").write_text(
        "2026-10-19 00:00:00,000 INFO sobafm.station: Program started\n"
        "2026-10-19 00:00:01,000 INFO sobafm.deck: Deck 1 retired after 1 s: 6 s of audio\n"
        "2026-10-19 00:00:02,000 INFO sobafm.track_deck: Deck 2 finished after 2 s: 2 s of audio\n",
        encoding="utf-8",
    )
    report(tmp_path, "synthetic", "test")
    output = capsys.readouterr().out
    assert "Decks ended: 2" in output
    assert "deck 1: retired" in output
    assert "deck 2: finished" in output
