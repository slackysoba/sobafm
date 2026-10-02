"""The PCM format Lyria RealTime produces and Discord encodes: 16-bit, 48 kHz, stereo."""

SAMPLE_RATE = 48_000
CHANNELS = 2
SAMPLE_WIDTH = 2  # bytes per sample
BYTES_PER_SECOND = SAMPLE_RATE * CHANNELS * SAMPLE_WIDTH
FRAME_SECONDS = 0.02
FRAME_BYTES = 3_840  # one 20 ms frame, the unit discord.py reads
SILENCE = bytes(FRAME_BYTES)
MIME_TYPE = "audio/l16;rate=48000;channels=2"  # how Lyria RealTime labels this format


def is_this_format(mime_type: str) -> bool:
    """Whether `mime_type` labels this format, in any case and with its parameters in any order."""
    kind, *parameters = (part.strip().lower() for part in mime_type.split(";"))
    values: dict[str, str] = {}
    for parameter in parameters:
        name, _, value = parameter.partition("=")
        values[name.strip()] = value.strip()
    rate, channels = values.get("rate"), values.get("channels")
    return kind == "audio/l16" and rate == str(SAMPLE_RATE) and channels == str(CHANNELS)


class FrameSplitter:
    """Splits audio chunks of any length into whole frames, carrying any remainder over."""

    def __init__(self) -> None:
        self._pending = bytearray()

    def split(self, chunk: bytes) -> list[bytes]:
        self._pending += chunk
        whole = len(self._pending) - len(self._pending) % FRAME_BYTES
        frames = [bytes(self._pending[i : i + FRAME_BYTES]) for i in range(0, whole, FRAME_BYTES)]
        del self._pending[:whole]
        return frames
