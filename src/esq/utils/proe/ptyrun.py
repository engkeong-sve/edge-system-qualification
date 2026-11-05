# Copyright (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""
Run a command with its output on a pseudo-terminal.

Programs such as ``apt-get`` and ``dpkg`` write in large blocks when their output is a pipe, so a live display would
show nothing for a long time and then a burst. On a pseudo-terminal they write line by line, so the latest line of
output can be shown while the command runs.
"""

import codecs
import os
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path

from esq.utils.proe.progress import clean_line

TERMINAL_ROWS = 50
TERMINAL_COLUMNS = 200
READ_SIZE = 4096


def available() -> bool:
    """Whether pseudo-terminals can be used on this platform."""
    try:
        import fcntl  # noqa: F401
        import pty  # noqa: F401
        import termios  # noqa: F401
    except ImportError:
        return False
    return True


def run_in_pty(argv: Sequence[str], cwd: Path, on_text: Callable[[str], None]) -> int:
    """Run ``argv`` and pass everything it writes to ``on_text``. Returns the exit code."""
    import fcntl
    import pty
    import struct
    import termios

    master, slave = pty.openpty()
    try:
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", TERMINAL_ROWS, TERMINAL_COLUMNS, 0, 0))
        proc = subprocess.Popen(  # noqa: S603 - argument list, no shell
            list(argv),
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=slave,
            stderr=slave,
            env=dict(os.environ, TERM="dumb"),
            close_fds=True,
        )
    except BaseException:
        os.close(master)
        raise
    finally:
        os.close(slave)
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    try:
        while True:
            try:
                data = os.read(master, READ_SIZE)
            except OSError:  # EIO: the last writer closed the terminal
                break
            if not data:
                break
            on_text(decoder.decode(data))
        tail = decoder.decode(b"", final=True)
        if tail:
            on_text(tail)
    finally:
        os.close(master)
    return proc.wait()


class LineSplitter:
    """Turn terminal output into log lines and live lines.

    The log gets one clean line per newline, with progress rewrites (carriage returns) reduced to their final state.
    The live display also gets every rewrite as it happens.
    """

    def __init__(self, log: Callable[[str], None], live: Callable[[str], None]) -> None:
        self._log = log
        self._live = live
        self._pending = ""

    def feed(self, text: str) -> None:
        parts = (self._pending + text.replace("\r\n", "\n")).split("\n")
        self._pending = parts.pop()
        for line in parts:
            segments = line.split("\r")
            for segment in segments[:-1]:
                self._live(segment)
            self._finish(segments[-1])
        if "\r" in self._pending:
            segments = self._pending.split("\r")
            for segment in segments[:-1]:
                self._live(segment)
            self._pending = segments[-1]

    def flush(self) -> None:
        if self._pending:
            self._finish(self._pending)
            self._pending = ""

    def _finish(self, line: str) -> None:
        self._live(line)
        text = clean_line(line)
        if text:
            self._log(text + "\n")
