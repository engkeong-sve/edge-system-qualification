# Copyright (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

import io
import unittest
from contextlib import redirect_stdout

from esq.utils.proe import progress


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class ProgressTests(unittest.TestCase):
    def setUp(self):
        self.out = []
        self.clock = Clock()
        self.progress = progress.Progress(self.out.append, total=3, clock=self.clock)

    def test_durations(self):
        self.assertEqual(progress.format_duration(0.4), "0s")
        self.assertEqual(progress.format_duration(41.2), "41s")
        self.assertEqual(progress.format_duration(125), "2m05s")

    def test_a_phase_prints_one_numbered_line_with_its_duration(self):
        with self.progress.phase("Installing the host tools"):
            self.clock.now = 12.0
        self.assertEqual(self.out, ["[1/3] Installing the host tools ... ok (12s)"])

    def test_a_quick_phase_shows_no_duration(self):
        with self.progress.phase("Backing up"):
            self.clock.now = 0.2
        self.assertEqual(self.out, ["[1/3] Backing up ... ok"])

    def test_notes_are_printed_under_the_result(self):
        with self.progress.phase("Installing") as phase:
            phase.note("+ tuna")
            phase.note("+ cmake")
        self.assertEqual(self.out, ["[1/3] Installing ... ok", "      + tuna", "      + cmake"])

    def test_a_phase_can_report_another_result(self):
        with self.progress.phase("Removing") as phase:
            phase.result = "skipped"
        self.assertEqual(self.out, ["[1/3] Removing ... skipped"])

    def test_a_failing_phase_is_marked_and_the_error_propagates(self):
        with self.assertRaises(ValueError):
            with self.progress.phase("Building"):
                raise ValueError("boom")
        self.assertEqual(self.out, ["[1/3] Building ... FAILED"])

    def test_the_total_grows_when_more_phases_run_than_planned(self):
        for name in ("a", "b", "c", "d"):
            with self.progress.phase(name):
                pass
        self.assertEqual(self.out[-1], "[4/4] d ... ok")

    def test_a_note_without_progress_goes_straight_to_the_sink(self):
        lines = []
        phase = progress.Phase(sink=lines.append)
        phase.note("hello")
        self.assertEqual(lines, ["hello"])
        self.assertEqual(phase.notes, [])


class LiveBlockTests(unittest.TestCase):
    def setUp(self):
        self.stream = io.StringIO()
        self.clock = Clock()
        self.block = progress._LiveBlock(
            "[2/7] Installing the real-time kernel", self.clock, stream=self.stream, width=lambda: 60, interval=0.01
        )

    def test_the_first_draw_shows_the_label_the_spinner_and_the_elapsed_time(self):
        self.clock.now = 41.7
        self.block._draw()
        text = self.stream.getvalue()
        self.assertIn("[2/7] Installing the real-time kernel (0:41)", text)
        self.assertIn(progress.FRAMES[0], text)

    def test_only_the_latest_output_line_is_shown_under_the_status(self):
        for index in range(5):
            self.block.feed(f"Unpacking package {index}\n")
        self.block._draw()
        text = self.stream.getvalue()
        for index in range(4):
            self.assertNotIn("package %d" % index, text)
        self.assertIn("    Unpacking package 4", text)

    def test_a_redraw_moves_up_over_the_previous_block(self):
        self.block._draw()
        self.block.feed("first\n")
        self.block._draw()
        self.block.feed("second\n")
        self.block._draw()
        text = self.stream.getvalue()
        self.assertIn("\x1b[1A", text)
        self.assertIn("\x1b[2A", text)

    def test_long_lines_are_cut_to_the_terminal_width(self):
        self.block.feed("x" * 200 + "\n")
        self.block._draw()
        drawn = [line for line in self.stream.getvalue().split("\n") if "xxx" in line][0]
        self.assertLessEqual(len(drawn.replace("\r\x1b[2K", "")), 59)
        self.assertTrue(drawn.endswith("…"))

    def test_progress_rewrites_and_colour_codes_are_removed(self):
        self.assertEqual(progress.clean_line("\x1b[32mdone\x1b[0m\r\n"), "done")
        self.assertEqual(progress.clean_line("Reading 10%\rReading 90%\n"), "Reading 90%")
        self.assertEqual(progress.clean_line("\n"), "")

    def test_stop_removes_the_block(self):
        self.block.feed("line\n")
        self.block._draw()
        self.block.stop()
        self.assertTrue(self.stream.getvalue().endswith("\x1b[2A\r\x1b[J"))

    def test_other_messages_are_printed_above_the_block(self):
        self.block._draw()
        self.block.write_above("WARNING: something")
        text = self.stream.getvalue()
        self.assertIn("\x1b[1A\r\x1b[JWARNING: something\n", text)

    def test_the_elapsed_time_format(self):
        self.assertEqual(progress.format_clock(5), "0:05")
        self.assertEqual(progress.format_clock(125), "2:05")
        self.assertEqual(progress.format_clock(3725), "1:02:05")

    def test_a_phase_with_the_live_block_shows_output_and_leaves_only_the_result(self):
        out = []
        stream = io.StringIO()
        runner_progress = progress.Progress(out.append, total=1, spinner=True, clock=self.clock, stream=stream)
        with runner_progress.phase("Installing") as phase:
            runner_progress.feed("Setting up linux-image ...\n")
            phase.note("+ linux-image")
        self.assertIn("[1/1] Installing", stream.getvalue())
        self.assertEqual(out, ["[1/1] Installing ... ok", "      + linux-image"])
        self.assertIsNone(progress._live)

    def test_write_line_prints_normally_without_a_live_block(self):
        self.assertIsNone(progress._live)
        printed = io.StringIO()
        with redirect_stdout(printed):
            progress.write_line("plain")
        self.assertEqual(printed.getvalue(), "plain\n")


if __name__ == "__main__":
    unittest.main()
