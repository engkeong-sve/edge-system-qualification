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
NAME = "proe-binary-ubuntu24.04-x86_64-12538f28"
DROPIN = "/etc/default/grub.d/99-esq-proe.cfg"
BACKUP = "/var/backups/esq/proe-grub"
GENERIC = "7.0.0-38-generic"
RT = "6.8.1-1015-realtime"
KERNEL_PACKAGES = ["linux-image-6.8.1-1015-realtime", "linux-modules-6.8.1-1015-realtime", "ubuntu-realtime"]
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
        self.packages = set(TOOL_PACKAGES) | set(KERNEL_PACKAGES) | {"git", "bash", "linux-image-7.0.0-38-generic"}
        self.extra_removals = []
        self.dpkg_code = 0
        self.apt_sim_code = 0
        self.rebooted = False

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
            self.world.rebooted = True
        return 0, ""


class UninstallerTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.state = Path(self._tmp.name) / "state"
        self.state.mkdir()
        self.world = World()
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
        self.assertEqual(self.world.packages, {"git", "bash", "linux-image-7.0.0-38-generic"})
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
        self.assertLess(index("99-esq-proe.cfg"), index("purge -y linux-image"))
        self.assertLess(index("purge -y linux-image"), index("update-grub"))

    def test_running_rt_kernel_is_removed_after_a_reboot_with_one_question(self):
        self.release = RT
        self.assertEqual(self._run(), uninst.EXIT_ACTION_REQUIRED)
        self.assertEqual(len(self.prompts), 1)
        self.assertIn("REBOOT", self.prompts[0])
        self.assertEqual(self._purges(), [TOOL_PACKAGES, ["ubuntu-realtime"]])
        self.assertTrue(self.world.rebooted)
        self.assertEqual(proe_state.read_install_state(self.state).state, proe_state.STATE_UNINSTALL_REBOOT)
        self.assertIn("linux-image-6.8.1-1015-realtime", self.world.packages)
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

        # Rebooted into the normal kernel: finish without asking again.
        self.release = GENERIC
        env = self._env()
        self.assertEqual(uninst.run_uninstall(uninst.UninstallOptions(), env), uninst.EXIT_OK)
        self.assertEqual(self.prompts, [])
        self.assertEqual(self._purges(), [["linux-image-6.8.1-1015-realtime", "linux-modules-6.8.1-1015-realtime"]])
        self.assertEqual(self.world.packages, {"git", "bash", "linux-image-7.0.0-38-generic"})
        self.assertEqual(self._state_files(), [uninst.LOG_NAME])

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
