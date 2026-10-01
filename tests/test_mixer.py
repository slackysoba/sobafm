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


def test_plays_silence_without_a_source() -> None:
    mixer = Mixer(1.0)

    assert mixer.read() == SILENCE
    assert mixer.underruns == 0
    assert not mixer.is_opus()


def test_plays_the_live_source() -> None:
    mixer = playing(source(2))

    assert [level_of(mixer.read()) for _ in range(2)] == [LEVEL, LEVEL]


def test_scales_by_volume() -> None:
    assert level_of(playing(source(1), volume=0.5).read()) == LEVEL // 2


def test_plays_silence_and_counts_an_underrun_when_the_live_source_is_empty() -> None:
    mixer = playing(source(0))

    assert mixer.read() == SILENCE
    assert mixer.underruns == 1


def test_fades_in_along_the_equal_power_curve() -> None:
    mixer = Mixer(1.0)
    new = source(10)

    mixer.switch_to(new, 0.1)
    levels = [level_of(mixer.read()) for _ in range(5)]

    expected = [LEVEL * math.sin((i + 0.5) / 5 * math.pi / 2) for i in range(5)]
    assert levels == pytest.approx(expected, abs=2)
    assert mixer.live is new
    assert not mixer.switching


def test_crossfades_with_constant_power() -> None:
    mixer = playing(source(10))
    new = source(10)

    mixer.switch_to(new, 0.1)
    levels = [level_of(mixer.read()) for _ in range(5)]

    expected = [
        LEVEL * (math.cos(p) + math.sin(p)) for p in ((i + 0.5) / 5 * math.pi / 2 for i in range(5))
    ]
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


def test_fades_out_to_silence() -> None:
    mixer = playing(source(10))

    mixer.switch_to(None, 0.06)
    levels = [level_of(mixer.read()) for _ in range(3)]

    assert levels[0] > levels[1] > levels[2] > 0
    assert mixer.live is None
    assert mixer.read() == SILENCE
    assert mixer.underruns == 0


def test_switch_progress_waits_for_reads() -> None:
    mixer = playing(source(10))

    mixer.switch_to(source(10), 0.1)
    mixer.read()

    assert mixer.switching


def test_ramps_volume_changes() -> None:
    mixer = playing(source(60), volume=0.5)

    mixer.set_volume(1.0)
    levels = [level_of(mixer.read()) for _ in range(30)]

    assert LEVEL // 2 < levels[0] < LEVEL * 0.6
    assert levels == sorted(levels)
    assert levels[-1] == LEVEL


def test_never_raises() -> None:
    class Broken:
        @property
        def frames(self) -> deque[bytes]:
            raise RuntimeError("broken source")

    mixer = Mixer(1.0)
    mixer.switch_to(Broken(), 0)

    assert mixer.read() == SILENCE
