"""One owned Linux stderr PTY; the bidirectional master is used only for reads."""

from __future__ import annotations

import errno
import os
import pty
import termios
import tty

from evaluations.linux_resource.probe import ProbeError


def open_diagnostics(owned: list[int]) -> tuple[int, int]:
    master, slave = pty.openpty()
    owned.extend((master, slave))
    tty.setraw(slave, when=termios.TCSANOW)
    termios.tcsetwinsize(slave, (24, 80))
    attrs = termios.tcgetattr(slave)
    forbidden_input = (
        termios.IGNBRK
        | termios.BRKINT
        | termios.PARMRK
        | termios.ISTRIP
        | termios.INLCR
        | termios.IGNCR
        | termios.ICRNL
        | termios.IXON
        | termios.IXOFF
        | termios.IXANY
    )
    # tty.setraw does not clear every platform's flow-control flags; set those
    # explicitly, without flushing/discarding any queued data.
    attrs[0] &= ~forbidden_input
    termios.tcsetattr(slave, termios.TCSANOW, attrs)
    attrs = termios.tcgetattr(slave)
    if (
        not os.isatty(slave)
        or termios.tcgetwinsize(slave) != (24, 80)
        or attrs[0] & forbidden_input
        or attrs[1] & termios.OPOST
        or attrs[2] & termios.CSIZE != termios.CS8
        or attrs[2] & termios.PARENB
        or attrs[3]
        & (termios.ECHO | termios.ECHONL | termios.ICANON | termios.ISIG | termios.IEXTEN)
        or attrs[6][termios.VMIN] != 1
        or attrs[6][termios.VTIME] != 0
    ):
        raise ProbeError("diagnostic_terminal_gate")
    os.set_blocking(master, False)
    return master, slave


def read_diagnostics(master: int, count: int) -> bytes | None:
    try:
        return os.read(master, count)
    except BlockingIOError, InterruptedError:
        return None
    except OSError as error:
        if error.errno != errno.EIO:
            raise
        # Linux drained-peer closure. Actual process/descriptor settlement is a
        # separate gate; another inherited slave would keep this channel alive.
        return b""
