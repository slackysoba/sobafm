"""The PCM format Lyria RealTime produces and Discord encodes: 16-bit, 48 kHz, stereo."""

from collections.abc import Generator
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
    """Assemble owned immutable frames incrementally, carrying less than one frame.

    Consume each iterator fully, or close it before consuming another chunk.
    Consumption starts on first advancement; overlapping consumption raises
    RuntimeError. Closing after a yield discards the unread part of that input.
    An unstarted iterator has no effect on the carry, even when closed.

    The iterator retains its original input until exhaustion/close, but never
    stages a whole-input copy or a collection of output frames. Only the copied
    remainder survives exhaustion; caller-retained frames have separate storage.
    """

    def __init__(self) -> None:
        self._pending = b""
        self._splitting = False

    def split(self, chunk: bytes) -> Generator[bytes]:
        if self._splitting:
            raise RuntimeError("consume or close the previous frame iterator first")
        self._splitting = True
        try:
            with memoryview(chunk) as data:
                offset = 0
                if self._pending:
                    needed = FRAME_BYTES - len(self._pending)
                    if len(data) < needed:
                        self._pending += data.tobytes()
                        return
                    frame = self._pending + data[:needed].tobytes()
                    self._pending = b""
                    offset = needed
                    yield frame
                    del frame
                while len(data) - offset >= FRAME_BYTES:
                    yield data[offset : offset + FRAME_BYTES].tobytes()
                    offset += FRAME_BYTES
                self._pending = data[offset:].tobytes()
        finally:
            self._splitting = False
