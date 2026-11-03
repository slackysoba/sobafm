"""One immutable FMA record remains owned across synchronous partial writes."""

from __future__ import annotations

import os

from evaluations.linux_resource.probe import ProbeError

from .limits import REPORT_BYTES, encode_report


class RecordWrite:
    """Submission is sticky even when the first syscall has an unknown outcome."""

    def __init__(self, data: bytes, reason: str = "report_write_failed") -> None:
        if type(data) is not bytes or len(data) > REPORT_BYTES:
            raise ProbeError("invalid_report_record")
        self.data, self.reason = data, reason
        self.offset = 0
        self.submitted = self.complete = self.failed = self.busy = False
        self.fd: int | None = None

    def write(self, fd: int) -> None:
        if self.busy or self.submitted or self.complete or self.failed:
            raise ProbeError("report_operation_retained")
        self.fd, self.busy = fd, True
        try:
            with memoryview(self.data) as view:
                while self.offset < len(view):
                    with view[self.offset :] as remaining:
                        self.submitted = True
                        try:
                            amount = os.write(fd, remaining)
                        except InterruptedError:
                            continue
                        if type(amount) is not int or not 0 < amount <= len(remaining):
                            raise ProbeError(self.reason)
                    self.offset += amount
            self.complete = True
        except BaseException:
            self.failed = True
            raise
        finally:
            self.busy = False


def emit(value: object) -> None:
    RecordWrite(encode_report(value, newline=True)).write(1)
