# Copyright (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

import tempfile
import unittest
from pathlib import Path, PurePosixPath

from esq.utils.proe import host_changes
from esq.utils.proe import installer as inst
from esq.utils.proe import state as proe_state
from esq.utils.proe import uninstaller as uninst

SHA = "a" * 64
NAME = "proe-binary-ubuntu26.04-x86_64-12538f28"
DROPIN = "/etc/default/grub.d/99-esq-proe.cfg"
BACKUP = "/var/backups/esq/proe-grub"
GENERIC = "7.0.0-30-generic"
RT = "7.0.0-38-realtime"
# The installer records the packages that 'apt-get install ubuntu-realtime' added, in sorted order.
KERNEL_PACKAGES = [
    f"linux-headers-{RT}",
    f"linux-image-{RT}",
    f"linux-modules-{RT}",
    "linux-realtime",
    f"linux-tools-{RT}",
    "ubuntu-realtime",
]
OTHER_PACKAGES = {"git", "bash", f"linux-image-{GENERIC}"}
ADVANCED = "gnulinux-advanced-U"


def grub_cfg(*releases):
    """A generated grub.cfg: the first kernel on top, then every kernel in the Advanced options submenu."""
    option = "--class ubuntu $menuentry_id_option"
    text = (
        f"menuentry 'Ubuntu' {option} 'gnulinux-simple-U' {{\n"
        f"\tlinux /boot/vmlinuz-{releases[0]} root=/dev/sda1 ro\n}}\n"
        f"submenu 'Advanced options for Ubuntu' $menuentry_id_option '{ADVANCED}' {{\n"
    )
    for release in releases:
        for suffix, kind in (("", "advanced"), (" (recovery mode)", "recovery")):
            text += (
                f"\tmenuentry 'Ubuntu, with Linux {release}{suffix}' {option} 'gnulinux-{release}-{kind}-U' {{\n"
                f"\t\tlinux /boot/vmlinuz-{release} root=/dev/sda1 ro\n\t}}\n"
            )
    return text + "}\n"
TOOL_PACKAGES = ["cmake", "cmake-data", "tuna"]
LIBYANG = ["/usr/local/lib/libyang.so", "/usr/local/bin/yanglint", "/usr/local/include/libyang/libyang.h"]


class World:
    """A pretend machine: files, links and installed packages."""

    def __init__(self):
        self.files = {
            "/opt/proe",
            "/opt/proe/cli.dist/proe",
            "/usr/local/bin/proe",
            "/etc/systemd/system/proe.service",
            "/etc/proe",
            "/etc/proe/app_config/proe_app.conf",
            "/var/log/proe",
            "/var/log/proe/benchmark_results",
            DROPIN,
            *LIBYANG,
        }
        self.links = {"/usr/local/bin/proe": "/opt/proe/cli.dist/proe"}
        self.packages = set(TOOL_PACKAGES) | set(KERNEL_PACKAGES) | OTHER_PACKAGES
        self.extra_removals = []
        self.dpkg_code = 0
        self.apt_sim_code = 0
        self.rebooted = False
        self.reboot_code = 0
        self.grub_cfg = grub_cfg(RT, GENERIC)
        self.dropin_text = None

    def remove_path(self, path):
        self.files = {f for f in self.files if f != path and not f.startswith(path.rstrip("/") + "/")}


class FakeRunner:
    def __init__(self, world):
        self.world = world
        self.streamed = []
        self.captured = []

    def stream(self, argv, log_path, cwd):
        self.streamed.append(list(argv))
        Path(log_path).write_text("log\n")
        if argv[0] == "rm":
            recursive = "-rf" in argv
            for path in [a for a in argv[1:] if a.startswith("/")]:
                if recursive:
                    self.world.remove_path(path)
                else:
                    self.world.files.discard(path)
        if argv[:4] == ["install", "-D", "-m", "0644"]:
            self.world.dropin_text = Path(argv[-2]).read_text()
            self.world.files.add(argv[-1])
        if argv[:5] == ["env", "DEBIAN_FRONTEND=noninteractive", "apt-get", "purge", "-y"]:
            self.world.packages -= set(argv[5:])
        return 0

    def capture(self, argv, timeout):
        self.captured.append(list(argv))
        if argv[0] == "dpkg-query":
            if self.world.dpkg_code:
                return self.world.dpkg_code, ""
            return 0, "".join(f"ii \t{name}\n" for name in sorted(self.world.packages))
        if argv[:3] == ["apt-get", "-s", "purge"]:
            if self.world.apt_sim_code:
                return self.world.apt_sim_code, ""
            names = [n for n in argv[3:] if n in self.world.packages] + list(self.world.extra_removals)
            return 0, "".join(f"Purg {name} [1.0]\n" for name in names)
        if argv[:2] == ["systemctl", "reboot"]:
            self.world.rebooted = self.world.reboot_code == 0
            return self.world.reboot_code, "Failed to reboot" if self.world.reboot_code else ""
        return 0, ""


class UninstallerTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.state = Path(self._tmp.name) / "state"
        self.state.mkdir()
        self.world = World()
        self.grub_cfg = Path(self._tmp.name) / "grub.cfg"
        self.out = []
        self.boot = "boot-1"
        self.release = GENERIC
        self._record()

    def _record(self, **overrides):
        proe_state.write_install_state(proe_state.InstallState(proe_state.STATE_INSTALLED, SHA, NAME), self.state)
        (self.state / "capability.json").write_text("{}")
        (self.state / "install.log").write_text("install log\n")
        (self.state / host_changes.LIBYANG_MANIFEST_FILE).write_text("\n".join(LIBYANG) + "\n")
        values = dict(
            kernel_packages=KERNEL_PACKAGES,
            tool_packages=TOOL_PACKAGES,
            libyang_manifest=host_changes.LIBYANG_MANIFEST_FILE,
            grub_dropin=DROPIN,
            grub_backup=BACKUP,
        )
        values.update(overrides)
        host_changes.update_host_changes(self.state, **values)

    def _env(self, *, interactive=True, answers=(), is_root=True):
        answers = list(answers)
        self.runner = FakeRunner(self.world)
        self.grub_cfg.write_text(self.world.grub_cfg)
        self.prompts = []

        def prompt(question):
            self.prompts.append(question)
            return answers.pop(0) if answers else "n"

        return inst.InstallEnvironment(
            runner=self.runner,
            is_root=is_root,
            interactive=interactive,
            prompt=prompt,
            out=self.out.append,
            boot_id=lambda: self.boot,
            kernel_release=lambda: self.release,
            grub_cfg=self.grub_cfg,
            state_dir=self.state,
            installed_root=PurePosixPath("/opt/proe"),
            cli_path="/usr/local/bin/proe",
            config_dir=PurePosixPath("/etc/proe"),
            log_dir=PurePosixPath("/var/log/proe"),
            service_path=PurePosixPath("/etc/systemd/system/proe.service"),
            libyang_root="/usr/local/",
            exists=lambda path: path in self.world.files,
            link_target=lambda path: self.world.links.get(path),
        )

    def _run(self, env=None, **options):
        return uninst.run_uninstall(uninst.UninstallOptions(**options), env or self._env(answers=["y"]))

    def _text(self):
        return "\n".join(self.out)

    def _commands(self):
        return self.runner.streamed

    def _purges(self):
        prefix = ["env", "DEBIAN_FRONTEND=noninteractive", "apt-get", "purge", "-y"]
        return [c[5:] for c in self._commands() if c[:5] == prefix]

    def _state_files(self):
        return sorted(p.name for p in self.state.iterdir())

    def test_nothing_installed_by_esq(self):
        for path in list(self.state.iterdir()):
            path.unlink()
        self.world.files = set()
        env = self._env()
        self.assertEqual(uninst.run_uninstall(uninst.UninstallOptions(), env), uninst.EXIT_OK)
        self.assertIn("Nothing to uninstall", self._text())
        self.assertEqual(self._commands(), [])

    def test_a_foreign_proe_installation_is_not_removed(self):
        for path in list(self.state.iterdir()):
            path.unlink()
        env = self._env()
        self.assertEqual(uninst.run_uninstall(uninst.UninstallOptions(assume_yes=True), env), uninst.EXIT_ERROR)
        self.assertIn("not installed by 'esq install proe'", self._text())
        self.assertEqual(self._commands(), [])
        self.assertIn("/opt/proe", self.world.files)

    def test_the_preview_shows_what_will_be_removed(self):
        self.assertEqual(self._run(self._env(answers=["n"])), uninst.EXIT_ACTION_REQUIRED)
        text = self._text()
        self.assertIn("PROe uninstall preview", text)
        self.assertIn("purge cmake cmake-data tuna", text)
        self.assertIn("remove 3 files", text)
        self.assertIn(DROPIN, text)
        self.assertIn(BACKUP, text)
        self.assertEqual(self._commands(), [])
        self.assertIn("/opt/proe", self.world.files)
        self.assertIn("capability.json", self._state_files())

    def test_declined_consent_changes_nothing(self):
        env = self._env(answers=["n"])
        self.assertEqual(uninst.run_uninstall(uninst.UninstallOptions(), env), uninst.EXIT_ACTION_REQUIRED)
        self.assertEqual(self._commands(), [])
        self.assertIn("cancelled", self._text())
        self.assertIn("/opt/proe", self.world.files)

    def test_non_interactive_needs_yes(self):
        env = self._env(interactive=False)
        self.assertEqual(uninst.run_uninstall(uninst.UninstallOptions(), env), uninst.EXIT_ERROR)
        self.assertIn("Consent is required", self._text())
        self.assertEqual(self._commands(), [])

    def test_full_uninstall_when_a_normal_kernel_runs(self):
        env = self._env(answers=["y"])
        self.assertEqual(uninst.run_uninstall(uninst.UninstallOptions(), env), uninst.EXIT_OK)
        self.assertEqual(len(self.prompts), 1)
        self.assertNotIn("REBOOT", self.prompts[0])
        commands = self._commands()
        self.assertIn(["rm", "-f", "--", "/etc/systemd/system/proe.service"], commands)
        self.assertIn(["rm", "-f", "--", "/usr/local/bin/proe"], commands)
        for path in ("/opt/proe", "/etc/proe", "/var/log/proe"):
            self.assertIn(["rm", "-rf", "--", path], commands)
        self.assertIn(["systemctl", "disable", "--now", "proe.service"], self.runner.captured)
        self.assertEqual(self._purges(), [TOOL_PACKAGES, KERNEL_PACKAGES])
        self.assertIn(["rm", "-f", "--", *LIBYANG], commands)
        self.assertIn(["ldconfig"], commands)
        self.assertIn(["rm", "-f", "--", DROPIN], commands)
        self.assertEqual(commands[-1], ["update-grub"])
        self.assertFalse(self.world.rebooted)
        self.assertEqual(self.world.files, set())
        self.assertEqual(self.world.packages, OTHER_PACKAGES)
        self.assertEqual(self._state_files(), [uninst.LOG_NAME])
        self.assertIn(BACKUP, self._text())

    def test_progress_lines_name_each_phase(self):
        self._run()
        text = self._text()
        for line in (
            "[1/6] Removing PROe ... ok",
            "[2/6] Removing the host tools ... ok",
            "[3/6] Removing libyang ... ok",
            "[4/6] Removing the boot parameters ... ok",
            "[5/6] Removing the real-time kernel ... ok",
            "[6/6] Updating the boot menu ... ok",
        ):
            self.assertIn(line, text)
        self.assertIn("      - cmake", text)

    def test_the_order_undoes_the_installation_in_reverse(self):
        self._run()
        commands = [" ".join(c) for c in self._commands()]

        def index(fragment):
            return next(i for i, c in enumerate(commands) if fragment in c)

        self.assertLess(index("rm -rf -- /opt/proe"), index("purge -y cmake"))
        self.assertLess(index("purge -y cmake"), index("ldconfig"))
        self.assertLess(index("ldconfig"), index("99-esq-proe.cfg"))
        self.assertLess(index("99-esq-proe.cfg"), index("purge -y linux-headers"))
        self.assertLess(index("purge -y linux-headers"), index("update-grub"))

    def test_running_rt_kernel_is_removed_after_a_reboot_with_one_question(self):
        self.release = RT
        self.assertEqual(self._run(), uninst.EXIT_ACTION_REQUIRED)
        self.assertEqual(len(self.prompts), 1)
        self.assertIn("REBOOT", self.prompts[0])
        self.assertEqual(self._purges(), [TOOL_PACKAGES], "the running kernel is not purged before the reboot")
        self.assertEqual(self._commands()[-1], ["update-grub"], "the boot menu is updated before the reboot")
        self.assertIn("[5/5] Updating the boot menu ... ok", self._text())
        self.assertTrue(self.world.rebooted)
        self.assertEqual(proe_state.read_install_state(self.state).state, proe_state.STATE_UNINSTALL_REBOOT)
        self.assertIn(f"linux-image-{RT}", self.world.packages)
        self.assertIn("host-changes.json", self._state_files(), "the record is kept until the kernel is removed")

        # Same boot: still waiting.
        env = self._env()
        self.assertEqual(uninst.run_uninstall(uninst.UninstallOptions(), env), uninst.EXIT_ACTION_REQUIRED)
        self.assertIn("waiting for a reboot", self._text())
        self.assertEqual(self._commands(), [])

        # Rebooted into the same RT kernel: explain.
        self.boot = "boot-2"
        env = self._env()
        self.assertEqual(uninst.run_uninstall(uninst.UninstallOptions(), env), uninst.EXIT_ACTION_REQUIRED)
        self.assertIn("still running the real-time kernel", self._text())
        self.assertEqual(self._commands(), [])

        # Rebooted into the normal kernel: finish without asking again, and delete the rewritten drop-in.
        self.release = GENERIC
        env = self._env()
        self.assertEqual(uninst.run_uninstall(uninst.UninstallOptions(), env), uninst.EXIT_OK)
        self.assertEqual(self.prompts, [])
        self.assertEqual(self._purges(), [KERNEL_PACKAGES])
        self.assertIn(["rm", "-f", "--", DROPIN], self._commands())
        self.assertNotIn(DROPIN, self.world.files)
        self.assertEqual(self.world.packages, OTHER_PACKAGES)
        self.assertEqual(self._state_files(), [uninst.LOG_NAME])

    def test_the_next_boot_is_the_normal_kernel_even_when_the_rt_kernel_is_newer(self):
        # GRUB sorts by version, so the newer real-time kernel would be the default again after the reboot.
        self.release = RT
        self.assertEqual(self._run(), uninst.EXIT_ACTION_REQUIRED)
        self.assertIn(DROPIN, self.world.files, "the drop-in is rewritten, not deleted, before the reboot")
        self.assertNotIn(["rm", "-f", "--", DROPIN], self._commands())
        self.assertIn(f'GRUB_DEFAULT="{ADVANCED}>gnulinux-{GENERIC}-advanced-U"', self.world.dropin_text)
        self.assertNotIn("isolcpus", self.world.dropin_text, "the PROe boot parameters are gone")
        self.assertIn(DROPIN, self._commands()[-3] + self._commands()[-2], "rewritten before update-grub")
        self.assertEqual(self._commands()[-1], ["update-grub"])
        self.assertEqual(self.runner.captured[-1][:2], ["systemctl", "reboot"], "the reboot is the last step")
        self.assertIn("set the next boot to the normal kernel", self._text())

    def test_without_a_normal_kernel_the_running_rt_kernel_is_left_installed(self):
        self.release = RT
        self.world.grub_cfg = grub_cfg(RT, "7.0.0-36-realtime")
        self.assertEqual(self._run(), uninst.EXIT_ACTION_REQUIRED)
        self.assertFalse(self.world.rebooted)
        self.assertIn("no normal kernel is installed", self._text())
        self.assertIn("linux-generic", self._text())
        self.assertEqual(self._purges(), [TOOL_PACKAGES])
        self.assertIn(f"linux-image-{RT}", self.world.packages)
        self.assertNotIn(DROPIN, self.world.files, "the PROe boot parameters are removed anyway")
        self.assertIn("host-changes.json", self._state_files(), "the record is kept, so the command can be repeated")
        self.assertNotEqual(
            proe_state.read_install_state(self.state).state, proe_state.STATE_UNINSTALL_REBOOT, "no reboot is pending"
        )

    def test_a_failed_reboot_command_tells_the_user_to_reboot_manually(self):
        self.release = RT
        self.world.reboot_code = 1
        self.assertEqual(self._run(), uninst.EXIT_ACTION_REQUIRED)
        self.assertFalse(self.world.rebooted)
        self.assertIn("The reboot command failed", self._text())
        self.assertIn("Reboot this machine manually", self._text())

    def test_the_preview_of_a_running_rt_kernel_names_the_boot_switch(self):
        self.release = RT
        self.assertEqual(self._run(self._env(answers=["n"])), uninst.EXIT_ACTION_REQUIRED)
        text = self._text()
        self.assertIn("set the next boot to the normal kernel", text)
        self.assertIn("then REBOOT", self.prompts[0])
        self.assertEqual(self._commands(), [])

    def test_yes_covers_the_reboot(self):
        self.release = RT
        env = self._env(interactive=False)
        code = uninst.run_uninstall(uninst.UninstallOptions(assume_yes=True), env)
        self.assertEqual(code, uninst.EXIT_ACTION_REQUIRED)
        self.assertTrue(self.world.rebooted)

    def test_packages_are_kept_when_apt_would_remove_others(self):
        self.world.extra_removals = ["libreoffice-core"]
        self.assertEqual(self._run(), uninst.EXIT_ACTION_REQUIRED)
        self.assertIn("would also remove packages that ESQ did not add (libreoffice-core)", self._text())
        self.assertEqual(self._purges(), [])
        self.assertTrue(set(TOOL_PACKAGES) <= self.world.packages)
        self.assertNotIn("/opt/proe", self.world.files, "PROe itself is still removed")
        self.assertIn("host-changes.json", self._state_files())
        self.assertIn("run this command again", self._text())

    def test_unknown_package_state_skips_package_removal_but_continues(self):
        self.world.dpkg_code = 1
        self.assertEqual(self._run(), uninst.EXIT_ACTION_REQUIRED)
        self.assertEqual(self._purges(), [])
        self.assertIn("could not be listed", self._text())
        self.assertNotIn("/opt/proe", self.world.files)
        self.assertIn("host-changes.json", self._state_files())

    def test_apt_simulation_failure_keeps_the_packages(self):
        self.world.apt_sim_code = 100
        self.assertEqual(self._run(), uninst.EXIT_ACTION_REQUIRED)
        self.assertEqual(self._purges(), [])
        self.assertIn("could not simulate", self._text())
        self.assertIn("host-changes.json", self._state_files())

    def test_keep_data_keeps_the_configuration_and_results(self):
        self.assertEqual(self._run(keep_data=True), uninst.EXIT_OK)
        commands = self._commands()
        self.assertIn(["rm", "-rf", "--", "/opt/proe"], commands)
        self.assertNotIn(["rm", "-rf", "--", "/etc/proe"], commands)
        self.assertNotIn(["rm", "-rf", "--", "/var/log/proe"], commands)
        self.assertIn("/etc/proe", self.world.files)
        self.assertIn("/var/log/proe/benchmark_results", self.world.files)

    def test_a_cli_link_that_is_not_from_proe_is_left_alone(self):
        self.world.links["/usr/local/bin/proe"] = "/somewhere/else"
        self.assertEqual(self._run(), uninst.EXIT_OK)
        self.assertNotIn(["rm", "-f", "--", "/usr/local/bin/proe"], self._commands())
        self.assertIn("/usr/local/bin/proe", self.world.files)
        self.assertIn("was not created by PROe", self._text())

    def test_only_packages_that_esq_recorded_are_purged(self):
        self.world.packages.add("gcc")
        self._run()
        for purged in self._purges():
            self.assertNotIn("gcc", purged)
            self.assertNotIn("git", purged)
        self.assertIn("gcc", self.world.packages)

    def test_a_manifest_path_outside_usr_local_stops_everything(self):
        (self.state / host_changes.LIBYANG_MANIFEST_FILE).write_text("/usr/local/lib/a.so\n/etc/passwd\n")
        self.assertEqual(self._run(), uninst.EXIT_ERROR)
        self.assertIn("outside /usr/local/", self._text())
        self.assertEqual(self._commands(), [])
        self.assertIn("/opt/proe", self.world.files)

    def test_running_the_command_again_after_completion_has_nothing_to_do(self):
        self.assertEqual(self._run(), uninst.EXIT_OK)
        self.out.clear()
        env = self._env()
        self.assertEqual(uninst.run_uninstall(uninst.UninstallOptions(), env), uninst.EXIT_OK)
        self.assertEqual(self._commands(), [])

    def test_an_interrupted_uninstall_can_be_repeated(self):
        self.world.files -= {"/opt/proe", "/opt/proe/cli.dist/proe"}
        self.world.packages -= set(TOOL_PACKAGES)
        self.assertEqual(self._run(), uninst.EXIT_OK)
        self.assertNotIn(["rm", "-rf", "--", "/opt/proe"], self._commands())
        self.assertEqual(self._purges(), [KERNEL_PACKAGES])

    def test_sudo_is_used_when_not_root(self):
        env = self._env(answers=["y"], is_root=False)
        self.assertEqual(uninst.run_uninstall(uninst.UninstallOptions(), env), uninst.EXIT_OK)
        self.assertTrue(all(c[:2] == ["sudo", "--"] for c in self._commands()))


if __name__ == "__main__":
    unittest.main()
