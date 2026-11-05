# Copyright (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

import sys
import time
import unittest

from esq.utils.proe import ptyrun


class LineSplitterTests(unittest.TestCase):
    def setUp(self):
        self.log = []
        self.live = []
        self.splitter = ptyrun.LineSplitter(self.log.append, self.live.append)

    def test_complete_lines_go_to_the_log_and_the_live_display(self):
        self.splitter.feed("Unpacking a ...\r\nSetting up a ...\r\n")
        self.assertEqual(self.log, ["Unpacking a ...\n", "Setting up a ...\n"])
        self.assertEqual(self.live, ["Unpacking a ...", "Setting up a ..."])

    def test_a_line_split_across_reads_is_joined(self):
        self.splitter.feed("Unpack")
        self.splitter.feed("ing a ...\n")
        self.assertEqual(self.log, ["Unpacking a ...\n"])

    def test_progress_rewrites_reach_the_live_display_but_the_log_keeps_the_final_state(self):
        self.splitter.feed("Reading database ... 5%\rReading database ... 50%\rReading database ... 100%\n")
        self.assertEqual(self.log, ["Reading database ... 100%\n"])
        self.assertEqual(
            self.live, ["Reading database ... 5%", "Reading database ... 50%", "Reading database ... 100%"]
        )

    def test_an_unfinished_progress_line_is_shown_live_before_its_newline(self):
        self.splitter.feed("Reading database ... 5%\rReading database ... 20%")
        self.assertEqual(self.live, ["Reading database ... 5%"])
        self.splitter.feed("\rReading database ... 40%\n")
        self.assertIn("Reading database ... 20%", self.live)
        self.assertEqual(self.log, ["Reading database ... 40%\n"])

    def test_colour_codes_and_empty_lines_are_not_logged(self):
        self.splitter.feed("\x1b[32mdone\x1b[0m\r\n\r\n")
        self.assertEqual(self.log, ["done\n"])

    def test_the_last_line_without_a_newline_is_flushed(self):
        self.splitter.feed("final")
        self.splitter.flush()
        self.assertEqual(self.log, ["final\n"])


@unittest.skipUnless(ptyrun.available(), "pseudo-terminals are not available on this platform")
class PseudoTerminalTests(unittest.TestCase):
    def test_output_arrives_line_by_line_while_the_command_runs(self):
        script = "import time\nprint('first')\ntime.sleep(1.0)\nprint('second')\n"
        seen = []
        start = time.monotonic()
        code = ptyrun.run_in_pty(
            [sys.executable, "-c", script], None, lambda text: seen.append((time.monotonic() - start, text))
        )
        self.assertEqual(code, 0)
        text = "".join(chunk for _when, chunk in seen)
        self.assertIn("first", text)
        self.assertIn("second", text)
        first = next(when for when, chunk in seen if "first" in chunk)
        self.assertLess(first, 0.8, "the first line must arrive while the command is still running")

    def test_the_exit_code_is_returned(self):
        code = ptyrun.run_in_pty([sys.executable, "-c", "raise SystemExit(7)"], None, lambda _text: None)
        self.assertEqual(code, 7)


if __name__ == "__main__":
    unittest.main()
