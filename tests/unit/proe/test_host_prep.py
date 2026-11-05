# Copyright (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from esq.utils.proe import host_prep
from esq.utils.proe.constants import InstallError


class GrubConfigTests(unittest.TestCase):
    PARAMS = ("isolcpus=1,2", "nowatchdog")

    @staticmethod
    def _cfg(*entries):
        return "".join(
            f"menuentry 'x' {{\n\tlinux\t/boot/vmlinuz-{image} root=/dev/sda1 ro {args}\n}}\n"
            for image, args in entries
        )

    def test_a_real_time_entry_with_every_parameter_is_accepted(self):
        text = self._cfg(("7.0.0-42-generic", "quiet"), ("6.8.1-1015-realtime", "quiet isolcpus=1,2 nowatchdog"))
        self.assertEqual(host_prep.grub_cfg_problems(text, self.PARAMS, True), [])

    def test_a_missing_real_time_entry_is_reported(self):
        text = self._cfg(("7.0.0-42-generic", "quiet isolcpus=1,2 nowatchdog"))
        self.assertEqual(host_prep.grub_cfg_problems(text, self.PARAMS, True)[0], "it has no real-time kernel entry")

    def test_parameters_must_be_on_the_real_time_entry(self):
        text = self._cfg(("7.0.0-42-generic", "isolcpus=1,2 nowatchdog"), ("6.8.1-1015-realtime", "quiet"))
        problems = host_prep.grub_cfg_problems(text, self.PARAMS, True)
        self.assertEqual(len(problems), 2)
        self.assertIn("isolcpus=1,2", problems[0])

    def test_without_a_kernel_change_any_entry_counts(self):
        text = self._cfg(("6.8.1-1015-realtime", "quiet isolcpus=1,2 nowatchdog"))
        self.assertEqual(host_prep.grub_cfg_problems(text, self.PARAMS, False), [])

    def test_a_file_without_kernel_entries_is_reported(self):
        problems = host_prep.grub_cfg_problems("set timeout=0\n", self.PARAMS, True)
        self.assertEqual(problems, ["it contains no kernel entry"])


class BootParamTests(unittest.TestCase):
    def test_stock_ubuntu_needs_every_parameter(self):
        plan = host_prep.plan_boot_params(["quiet", "splash"])
        self.assertEqual(plan.add, host_prep.BOOT_PARAMS)
        self.assertEqual(plan.present, ())
        self.assertEqual(plan.conflicts, ())

    def test_present_parameters_are_not_added_again(self):
        plan = host_prep.plan_boot_params(["quiet", "iommu=pt", "nowatchdog"])
        self.assertNotIn("iommu=pt", plan.add)
        self.assertNotIn("nowatchdog", plan.add)
        self.assertEqual(plan.present, ("iommu=pt", "nowatchdog"))

    def test_different_value_for_the_same_key_is_replaced(self):
        plan = host_prep.plan_boot_params(["isolcpus=3"])
        self.assertEqual(len(plan.conflicts), 1)
        self.assertIn("isolcpus=1,2", plan.conflicts[0])
        self.assertIn("isolcpus=3", plan.conflicts[0])
        self.assertIn("isolcpus=1,2", plan.add)
        self.assertEqual(plan.replace_keys, ("isolcpus",))

    def test_repeatable_keys_never_conflict(self):
        plan = host_prep.plan_boot_params(["console=tty0", "modprobe.blacklist=nouveau"])
        self.assertEqual(plan.conflicts, ())
        self.assertEqual(plan.replace_keys, ())
        self.assertIn("console=ttyS0,115200n8", plan.add)
        self.assertIn("modprobe.blacklist=i915", plan.add)

    def test_missing_from_cmdline(self):
        cmdline = "BOOT_IMAGE=/vmlinuz ro quiet splash " + " ".join(host_prep.BOOT_PARAMS)
        self.assertEqual(host_prep.missing_from_cmdline(cmdline), [])
        self.assertEqual(host_prep.missing_from_cmdline("quiet splash"), list(host_prep.BOOT_PARAMS))
        self.assertEqual(host_prep.missing_from_cmdline(cmdline.replace("hpet=disable", "")), ["hpet=disable"])

    def test_default_parameters_match_the_agreed_set(self):
        self.assertIn("isolcpus=1,2", host_prep.BOOT_PARAMS)
        self.assertIn("rcu_nocbs=1,2", host_prep.BOOT_PARAMS)
        self.assertIn("irqaffinity=0", host_prep.BOOT_PARAMS)
        self.assertEqual(len(host_prep.BOOT_PARAMS), len(set(host_prep.BOOT_PARAMS)))


class GrubFileTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        (self.root / "grub.d").mkdir()
        self.default = self.root / "default"

    def test_reads_defaults_and_dropins_but_not_its_own_dropin(self):
        self.default.write_text(
            '# comment\nGRUB_DEFAULT=0\nGRUB_CMDLINE_LINUX_DEFAULT="quiet splash"\nGRUB_CMDLINE_LINUX="iommu=pt"\n'
        )
        (self.root / "grub.d" / "50-other.cfg").write_text("GRUB_CMDLINE_LINUX='$GRUB_CMDLINE_LINUX nosmt'\n")
        (self.root / "grub.d" / host_prep.DROPIN_NAME).write_text('GRUB_CMDLINE_LINUX="isolcpus=1,2"\n')
        tokens = host_prep.read_configured_tokens(self.default, self.root / "grub.d")
        self.assertEqual(sorted(tokens), sorted(["quiet", "splash", "iommu=pt", "nosmt"]))

    def test_missing_files_are_not_an_error(self):
        self.assertEqual(host_prep.read_configured_tokens(self.root / "none", self.root / "no-dir"), [])


class DropinTests(unittest.TestCase):
    def test_render_appends_to_the_existing_command_line(self):
        text = host_prep.render_dropin(["isolcpus=1,2", "xe.force_probe=*"])
        self.assertIn('GRUB_CMDLINE_LINUX="$GRUB_CMDLINE_LINUX isolcpus=1,2 xe.force_probe=*"', text)
        self.assertNotIn("GRUB_DEFAULT", text)
        self.assertNotIn("_esq_drop", text)

    def test_render_replacing_existing_values_filters_both_variables(self):
        text = host_prep.render_dropin(["isolcpus=1,2"], ["isolcpus", "rcu_nocbs"])
        self.assertIn('"isolcpus="*|"rcu_nocbs="*) ;;', text)
        self.assertIn('GRUB_CMDLINE_LINUX="$(_esq_drop "$GRUB_CMDLINE_LINUX") isolcpus=1,2"', text)
        self.assertIn('GRUB_CMDLINE_LINUX_DEFAULT="$(_esq_drop "$GRUB_CMDLINE_LINUX_DEFAULT")"', text)

    def test_render_refuses_shell_metacharacters(self):
        for bad in ('a"b', "a$b", "a`b", "a\\b", "a\nb"):
            with self.assertRaises(InstallError):
                host_prep.render_dropin([bad])
        for bad_key in ("a b", 'a"b', "a|b", "a*", ""):
            with self.assertRaises(InstallError):
                host_prep.render_dropin(["x"], [bad_key])

    @unittest.skipUnless(shutil.which("sh"), "needs a POSIX shell")
    def test_replace_form_filters_the_existing_command_line_in_a_real_shell(self):
        script = host_prep.render_dropin(["isolcpus=1,2", "xe.force_probe=*"], ["isolcpus"])
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "dropin.cfg"
            path.write_bytes(script.encode("utf-8"))
            env = dict(
                os.environ,
                GRUB_CMDLINE_LINUX="isolcpus=3 nosmt",
                GRUB_CMDLINE_LINUX_DEFAULT="quiet isolcpus=4 splash",
            )
            command = '. "$1"; printf "%s|%s" "$GRUB_CMDLINE_LINUX" "$GRUB_CMDLINE_LINUX_DEFAULT"'
            argv = ["sh", "-c", command, "sh", path.as_posix()]
            result = subprocess.run(argv, env=env, capture_output=True, text=True)
        self.assertEqual(result.stdout, "nosmt isolcpus=1,2 xe.force_probe=*|quiet splash", result.stderr)


class KernelVersionTests(unittest.TestCase):
    def test_parse_kernel_version(self):
        self.assertEqual(host_prep.parse_kernel_version("6.8.1-1015-realtime"), (6, 8, 1))
        self.assertEqual(host_prep.parse_kernel_version("7.0.0-38-generic"), (7, 0, 0))
        self.assertEqual(host_prep.parse_kernel_version("6.8-rc1"), (6, 8, 0))
        self.assertEqual(host_prep.parse_kernel_version("6.8.1-1015.16"), (6, 8, 1))
        self.assertIsNone(host_prep.parse_kernel_version("garbage"))
        self.assertIsNone(host_prep.parse_kernel_version(""))

    def test_kernel_at_least_the_minimum(self):
        self.assertEqual(host_prep.MIN_RT_KERNEL, (6, 8, 1))
        self.assertTrue(host_prep.kernel_at_least("6.8.1-1015-realtime"))
        self.assertTrue(host_prep.kernel_at_least("6.8.2-1-realtime"))
        self.assertTrue(host_prep.kernel_at_least("6.10.0-1-realtime"))
        self.assertTrue(host_prep.kernel_at_least("7.0.0-29-realtime"))
        self.assertFalse(host_prep.kernel_at_least("6.8.0-1-realtime"))
        self.assertFalse(host_prep.kernel_at_least("6.6.1-1-realtime"))
        self.assertFalse(host_prep.kernel_at_least("5.15.0-rt"))
        self.assertFalse(host_prep.kernel_at_least("unknown"))

    def test_format_version(self):
        self.assertEqual(host_prep.format_version((6, 8, 1)), "6.8.1")

    def test_parse_apt_candidate(self):
        output = "linux-image-realtime:\n  Installed: (none)\n  Candidate: 6.8.1-1015.16\n  Version table:\n"
        self.assertEqual(host_prep.parse_apt_candidate(output), "6.8.1-1015.16")
        self.assertIsNone(host_prep.parse_apt_candidate("x:\n  Installed: (none)\n  Candidate: (none)\n"))
        self.assertIsNone(host_prep.parse_apt_candidate(""))


class DependencyTests(unittest.TestCase):
    def test_nothing_missing(self):
        plan = host_prep.plan_dependencies(lambda _tool: True)
        self.assertEqual(plan.tools, ())
        self.assertEqual(plan.packages, ())
        self.assertFalse(plan.build_libyang)

    def test_missing_tools_map_to_packages(self):
        plan = host_prep.plan_dependencies(lambda tool: tool not in ("ptp4l", "phc2sys", "rdmsr"))
        self.assertEqual(plan.tools, ("ptp4l", "phc2sys", "rdmsr"))
        self.assertEqual(plan.packages, ("linuxptp", "msr-tools"))
        self.assertFalse(plan.build_libyang)

    def test_libyang_is_built_from_source(self):
        plan = host_prep.plan_dependencies(lambda tool: tool != "yanglint")
        self.assertTrue(plan.build_libyang)
        self.assertIn("yanglint", plan.tools)
        self.assertIn("cmake", plan.packages)
        self.assertEqual(host_prep.LIBYANG_TAG, "v4.2.2")


if __name__ == "__main__":
    unittest.main()
