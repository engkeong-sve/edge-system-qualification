# Copyright (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""
Numbered progress for ``esq install proe`` and ``esq uninstall proe``.

Each phase prints one line when it ends, for example ``[3/7] Installing the host tools ... ok (12s)``, followed by
indented notes such as the packages that were added. The raw output of the commands goes to the log file. While a
phase runs on a terminal, a live block under it shows a spinner, the elapsed time and the latest line that the running
command wrote. The block is redrawn in place and removed when the phase ends.
"""

import os
import re
import shutil
import sys
import threading
import time
from collections import deque
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import List, Optional, TextIO

MIN_SECONDS_SHOWN = 1
NOTE_INDENT = "      "
OUTPUT_INDENT = "    "
OUTPUT_LINES = 1
FRAMES = ["⣾", "⣷", "⣯", "⣟", "⡿", "⢿", "⣻", "⣽"]
FRAME_INTERVAL = 0.1

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def live_enabled(verbose: bool) -> bool:
    """Whether to draw the live block: on a terminal, and not when the raw output is shown."""
    return not verbose and sys.stdout.isatty() and os.environ.get("TERM", "") != "dumb"


def clean_line(raw: str) -> str:
    """One line of command output without progress rewrites (carriage returns) and terminal control codes."""
    text = raw.rstrip("\r\n").split("\r")[-1]
    return _CONTROL_RE.sub("", _ANSI_RE.sub("", text)).strip()


def truncate(text: str, width: int) -> str:
    if len(text) <= width:
        return text
    return text[: max(width - 1, 1)] + "…"


def format_clock(seconds: float) -> str:
    total = int(seconds)
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def format_duration(seconds: float) -> str:
    total = int(seconds + 0.5)
    if total < 60:
        return f"{total}s"
    minutes, rest = divmod(total, 60)
    return f"{minutes}m{rest:02d}s"


class _LiveBlock:
    """A status line with a spinner and the elapsed time, and the latest lines of output below it.

    The block only grows (up to ``OUTPUT_LINES`` output lines), so a redraw moves the cursor up by the number of lines
    drawn last time and writes over them.
    """

    def __init__(
        self,
        label: str,
        clock: Callable[[], float],
        stream: Optional[TextIO] = None,
        width: Optional[Callable[[], int]] = None,
        interval: float = FRAME_INTERVAL,
    ) -> None:
        self._label = label
        self._clock = clock
        self._stream = stream if stream is not None else sys.stdout
        self._width = width if width is not None else (lambda: shutil.get_terminal_size((80, 24)).columns)
        self._interval = interval
        self._tail: deque = deque(maxlen=OUTPUT_LINES)
        self._lock = threading.RLock()
        self._drawn = 0
        self._frame = 0
        self._started = clock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._animate, daemon=True)

    def start(self) -> None:
        with self._lock:
            self._draw()
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=1)
        with self._lock:
            self._erase()

    def feed(self, raw: str) -> None:
        text = clean_line(raw)
        if text:
            with self._lock:
                self._tail.append(text)

    def write_above(self, text: str) -> None:
        """Print a line above the block, so that other messages do not tear it."""
        with self._lock:
            self._erase()
            self._write(text + "\n")
            self._draw()

    def _animate(self) -> None:
        while not self._stop.wait(self._interval):
            with self._lock:
                self._frame += 1
                self._draw()

    def _lines(self) -> List[str]:
        columns = max(self._width() - 1, 20)
        elapsed = format_clock(self._clock() - self._started)
        head = f"{FRAMES[self._frame % len(FRAMES)]} {self._label} ({elapsed})"
        return [truncate(head, columns)] + [truncate(OUTPUT_INDENT + line, columns) for line in self._tail]

    def _draw(self) -> None:
        lines = self._lines()
        out = f"\x1b[{self._drawn}A" if self._drawn else ""
        out += "".join(f"\r\x1b[2K{line}\n" for line in lines)
        self._drawn = len(lines)
        self._write(out)

    def _erase(self) -> None:
        if self._drawn:
            self._write(f"\x1b[{self._drawn}A\r\x1b[J")
            self._drawn = 0

    def _write(self, text: str) -> None:
        self._stream.write(text)
        self._stream.flush()


# The block that is being drawn, so that ``write_line`` can print above it.
_live: Optional[_LiveBlock] = None


def write_line(text: str) -> None:
    """Print a line without tearing the live block."""
    live = _live
    if live is not None:
        live.write_above(text)
    else:
        print(text)


class Phase:
    """Handle that the body of a phase uses to report notes and a result other than ``ok``."""

    def __init__(self, sink: Optional[Callable[[str], None]] = None) -> None:
        self.result = "ok"
        self.notes: List[str] = []
        self._sink = sink

    def note(self, text: str) -> None:
        if self._sink is not None:
            self._sink(text)
        else:
            self.notes.append(text)


class Progress:
    def __init__(
        self,
        out: Callable[[str], None],
        total: int,
        spinner: bool = False,
        clock: Callable[[], float] = time.monotonic,
        stream: Optional[TextIO] = None,
    ) -> None:
        self._out = out
        self._total = total
        self._spinner = spinner
        self._clock = clock
        self._stream = stream
        self._index = 0
        self._block: Optional[_LiveBlock] = None

    def feed(self, raw: str) -> None:
        """Show a line that the running command wrote, in the live block."""
        block = self._block
        if block is not None:
            block.feed(raw)

    def _label(self, title: str) -> str:
        self._index += 1
        return f"[{self._index}/{max(self._total, self._index)}] {title}"

    @contextmanager
    def phase(self, title: str) -> Iterator[Phase]:
        global _live
        label = self._label(title)
        handle = Phase()
        start = self._clock()
        block = _LiveBlock(label, self._clock, stream=self._stream) if self._spinner else None
        try:
            if block is not None:
                block.start()
                self._block = _live = block
            try:
                yield handle
            finally:
                if block is not None:
                    block.stop()
                    self._block = _live = None
        except Exception:
            self._out(f"{label} ... FAILED")
            raise
        elapsed = self._clock() - start
        shown = f" ({format_duration(elapsed)})" if elapsed >= MIN_SECONDS_SHOWN else ""
        self._out(f"{label} ... {handle.result}{shown}")
        for note in handle.notes:
            self._out(NOTE_INDENT + note)
