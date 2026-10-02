import logging
import math
from collections import deque
from dataclasses import dataclass, field

import pytest

from sobafm.mixer import Mixer
from sobafm.pcm import FRAME_BYTES, SILENCE

LEVEL = 10_000  # sample value of the test frames


@dataclass
class Source:
    frames: deque[bytes] = field(default_factory=deque[bytes])


def source(count: int, level: int = LEVEL) -> Source:
    frame = level.to_bytes(2, "little", signed=True) * (FRAME_BYTES // 2)
    return Source(deque([frame] * count))


def level_of(frame: bytes) -> int:
    assert len(frame) == FRAME_BYTES
    return int.from_bytes(frame[:2], "little", signed=True)


def playing(live: Source, volume: float = 1.0) -> Mixer:
    mixer = Mixer(volume)
    mixer.switch_to(live, 0)
    return mixer


def angles(frames: int) -> list[float]:
    """The point on the equal-power curve for each frame of a switch."""
    return [(i + 0.5) / frames * math.pi / 2 for i in range(frames)]


def test_plays_silence_without_a_source() -> None:
    mixer = Mixer(1.0)

    assert mixer.read() == SILENCE
    assert mixer.underruns == 0


def test_plays_the_live_source() -> None:
    mixer = playing(source(2))

    assert [level_of(mixer.read()) for _ in range(2)] == [LEVEL, LEVEL]


def test_scales_by_volume() -> None:
    assert level_of(playing(source(1), volume=0.5).read()) == LEVEL // 2


def test_plays_silence_and_counts_an_underrun_when_the_live_source_is_empty() -> None:
    mixer = playing(source(1))
    mixer.read()

    assert mixer.read() == SILENCE
    assert mixer.underruns == 1


def test_fades_in_along_the_equal_power_curve_at_the_volume() -> None:
    mixer = Mixer(0.5)
    new = source(10)

    mixer.switch_to(new, 0.1)
    levels = [level_of(mixer.read()) for _ in range(5)]

    assert levels == pytest.approx([0.5 * LEVEL * math.sin(a) for a in angles(5)], abs=2)
    assert mixer.live is new
    assert not mixer.switching


def test_crossfades_along_equal_power_curves_at_the_current_volume() -> None:
    mixer = playing(source(10, level=8_000), volume=0.5)
    new = source(10, level=2_000)

    mixer.switch_to(new, 0.1)
    levels = [level_of(mixer.read()) for _ in range(5)]

    expected = [0.5 * (8_000 * math.cos(a) + 2_000 * math.sin(a)) for a in angles(5)]
    assert levels == pytest.approx(expected, abs=2)
    assert mixer.live is new


def test_clamps_a_crossfade_to_the_outgoing_audio() -> None:
    mixer = playing(source(3))
    new = source(300)

    mixer.switch_to(new, 4.0)
    for _ in range(3):
        mixer.read()

    assert mixer.live is new
    assert mixer.underruns == 0


def test_fades_in_from_silence_when_the_live_source_has_run_dry() -> None:
    mixer = playing(source(0))
    new = source(10)

    mixer.switch_to(new, 0.1)
    levels = [level_of(mixer.read()) for _ in range(5)]

    assert levels == pytest.approx([LEVEL * math.sin(a) for a in angles(5)], abs=2)


def test_fades_out_along_the_equal_power_curve_at_the_volume() -> None:
    mixer = playing(source(10), volume=0.5)

    mixer.switch_to(None, 0.06)
    levels = [level_of(mixer.read()) for _ in range(3)]

    assert levels == pytest.approx([0.5 * LEVEL * math.cos(a) for a in angles(3)], abs=2)
    assert mixer.live is None
    assert mixer.read() == SILENCE
    assert mixer.underruns == 0


def test_a_switch_advances_only_with_reads() -> None:
    mixer = playing(source(10))
    new = source(10)

    mixer.switch_to(new, 0.1)
    first = [level_of(mixer.read()) for _ in range(2)]
    assert mixer.switching  # discord.py stops reading while voice reconnects
    rest = [level_of(mixer.read()) for _ in range(3)]

    expected = [LEVEL * (math.cos(a) + math.sin(a)) for a in angles(5)]
    assert first + rest == pytest.approx(expected, abs=2)
    assert mixer.live is new


def test_switching_to_the_current_target_changes_nothing() -> None:
    live = source(10)
    mixer = playing(live)
    mixer.switch_to(live, 0.1)
    assert not mixer.switching

    new = source(10)
    mixer.switch_to(new, 0.1)
    mixer.read()
    mixer.switch_to(new, 0.1)  # a repeated request must not restart the fade

    levels = [level_of(mixer.read()) for _ in range(4)]
    expected = [LEVEL * (math.cos(a) + math.sin(a)) for a in angles(5)[1:]]
    assert levels == pytest.approx(expected, abs=2)


def test_a_new_target_during_a_switch_fades_from_the_incoming_source() -> None:
    mixer = playing(source(10, level=8_000))
    incoming = source(10, level=2_000)
    mixer.switch_to(incoming, 0.1)
    mixer.read()

    mixer.switch_to(None, 0.06)

    levels = [level_of(mixer.read()) for _ in range(3)]
    assert levels == pytest.approx([2_000 * math.cos(a) for a in angles(3)], abs=2)


def test_fading_out_silence_is_instant() -> None:
    mixer = Mixer(1.0)

    mixer.switch_to(None, 3.0)

    assert not mixer.switching


def test_fading_out_a_dry_source_is_instant() -> None:
    mixer = playing(source(0))

    mixer.switch_to(None, 3.0)

    assert not mixer.switching
    assert mixer.live is None


def test_switching_a_dry_source_to_itself_changes_nothing() -> None:
    dry = source(0)
    mixer = playing(dry)

    mixer.switch_to(dry, 2.0)

    assert not mixer.switching
    assert mixer.live is dry


def test_counts_an_underrun_while_fading_in_an_empty_source() -> None:
    mixer = Mixer(1.0)
    mixer.switch_to(source(0), 0.1)

    assert mixer.read() == SILENCE
    assert mixer.underruns == 1


def test_counts_one_underrun_per_silent_read() -> None:
    mixer = playing(source(5))
    mixer.switch_to(source(0), 0.1)  # the incoming source has nothing yet
    for _ in range(5):
        mixer.read()
    assert mixer.underruns == 0

    mixer.switch_to(source(0), 0)
    mixer.read()
    assert mixer.underruns == 1


@pytest.mark.parametrize(("start", "end"), [(0.5, 1.0), (1.0, 0.5)], ids=["up", "down"])
def test_ramps_volume_changes(start: float, end: float) -> None:
    mixer = playing(source(60), volume=start)

    mixer.set_volume(end)
    levels = [level_of(mixer.read()) for _ in range(30)]

    step = 0.02 if end > start else -0.02
    gains = [start + step * (i + 1) for i in range(30)]
    expected = [LEVEL * (min(g, end) if end > start else max(g, end)) for g in gains]
    assert levels == pytest.approx(expected, abs=2)


def test_ramps_a_volume_change_during_a_crossfade() -> None:
    mixer = playing(source(10, level=8_000))
    mixer.switch_to(source(10, level=2_000), 0.1)

    mixer.set_volume(0.5)
    levels = [level_of(mixer.read()) for _ in range(5)]

    expected = [
        (1 - 0.02 * (i + 1)) * (8_000 * math.cos(a) + 2_000 * math.sin(a))
        for i, a in enumerate(angles(5))
    ]
    assert levels == pytest.approx(expected, abs=2)


class Flaky:
    """A source whose frames fail to read while `broken` is set."""

    def __init__(self, count: int) -> None:
        self.broken = True
        self._frames = source(count).frames

    @property
    def frames(self) -> deque[bytes]:
        if self.broken:
            raise RuntimeError("broken source")
        return self._frames


def test_never_raises_and_logs_each_run_of_failures_once(
    caplog: pytest.LogCaptureFixture,
) -> None:
    flaky = Flaky(10)
    mixer = Mixer(1.0)
    mixer.switch_to(flaky, 0)

    with caplog.at_level(logging.ERROR, logger="sobafm.mixer"):
        assert [mixer.read() for _ in range(3)] == [SILENCE] * 3
        flaky.broken = False
        assert level_of(mixer.read()) == LEVEL
        flaky.broken = True
        assert [mixer.read() for _ in range(2)] == [SILENCE] * 2

    assert mixer.errors == 5
    assert len(caplog.records) == 2


def test_a_failure_finishes_the_switch_in_progress() -> None:
    mixer = playing(source(10))
    flaky = Flaky(10)
    mixer.switch_to(flaky, 0.1)

    assert mixer.read() == SILENCE

    assert not mixer.switching
    assert mixer.live is flaky
