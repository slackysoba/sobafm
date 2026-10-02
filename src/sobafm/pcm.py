"""The PCM format Lyria RealTime produces and Discord encodes: 16-bit, 48 kHz, stereo."""

from email.message import Message

SAMPLE_RATE = 48_000
CHANNELS = 2
SAMPLE_WIDTH = 2  # bytes per sample
BYTES_PER_SECOND = SAMPLE_RATE * CHANNELS * SAMPLE_WIDTH
FRAME_SECONDS = 0.02
FRAME_BYTES = 3_840  # one 20 ms frame, the unit discord.py reads
SILENCE = bytes(FRAME_BYTES)
MIME_TYPE = "audio/l16;rate=48000;channels=2"  # how Lyria RealTime labels this format


def _essentials(mime_type: str) -> tuple[str, object, object]:
    """The type, rate, and channels, parsed by the standard library's MIME parser."""
    header = Message()
    header["Content-Type"] = mime_type
    parameters = dict(header.get_params(failobj=[])[1:])  # the first is the type itself
    return header.get_content_type(), parameters.get("rate"), parameters.get("channels")


_FORMAT = _essentials(MIME_TYPE)


def matches_mime_type(mime_type: str) -> bool:
    """Whether `mime_type` has `MIME_TYPE`'s type, rate, and channels.

    Names can be in any case, and parameters in any order and quoted. Other parameters are
    ignored.
    """
    try:
        return _essentials(mime_type) == _FORMAT
    except ValueError:  # such as a lone surrogate, which the parser cannot encode
        return False


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
