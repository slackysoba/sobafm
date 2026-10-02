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


def test_fades_in_along_the_equal_power_curve() -> None:
    mixer = Mixer(1.0)
    new = source(10)

    mixer.switch_to(new, 0.1)
    levels = [level_of(mixer.read()) for _ in range(5)]

    assert levels == pytest.approx([LEVEL * math.sin(a) for a in angles(5)], abs=2)
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


def test_fades_out_along_the_equal_power_curve() -> None:
    mixer = playing(source(10))

    mixer.switch_to(None, 0.06)
    levels = [level_of(mixer.read()) for _ in range(3)]

    assert levels == pytest.approx([LEVEL * math.cos(a) for a in angles(3)], abs=2)
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


def test_counts_one_underrun_per_silent_read() -> None:
    mixer = playing(source(5))
    mixer.switch_to(source(0), 0.1)  # the incoming source has nothing yet
    for _ in range(5):
        mixer.read()
    assert mixer.underruns == 0

    mixer.switch_to(source(0), 0)
    mixer.read()
    assert mixer.underruns == 1


def test_ramps_volume_changes() -> None:
    mixer = playing(source(60), volume=0.5)

    mixer.set_volume(1.0)
    levels = [level_of(mixer.read()) for _ in range(30)]

    expected = [LEVEL * min(1.0, 0.5 + 0.02 * (i + 1)) for i in range(30)]
    assert levels == pytest.approx(expected, abs=2)


def test_never_raises_and_logs_each_run_of_failures_once(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class Broken:
        @property
        def frames(self) -> deque[bytes]:
            raise RuntimeError("broken source")

    mixer = Mixer(1.0)
    mixer.switch_to(Broken(), 0)

    with caplog.at_level(logging.ERROR, logger="sobafm.mixer"):
        assert [mixer.read() for _ in range(3)] == [SILENCE] * 3

    assert mixer.errors == 3
    assert len(caplog.records) == 1
