# Copyright (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

import shutil
import tempfile
import unittest
from pathlib import Path

from esq.utils.proe import host_changes, host_prep
from esq.utils.proe import installer as inst
from esq.utils.proe import state as proe_state
from esq.utils.proe.availability import resolve_profile
from esq.utils.proe.constants import RT_KERNEL_PACKAGE, ProeError
from esq.utils.proe.package import HostInfo

from . import synthetic as syn

SUPPORTED_PROFILE = "profile.suite.realtime.proe_1ms_ai_single_dut"
FULL_CMDLINE = "BOOT_IMAGE=/vmlinuz ro quiet splash " + " ".join(host_prep.BOOT_PARAMS)
STOCK_CMDLINE = "BOOT_IMAGE=/vmlinuz ro quiet splash vt.handoff=7"
RT = "7.0.0-38-realtime"
# What 'apt-get install -y ubuntu-realtime' adds in the fake machine, in the order the installer records it.
RT_PACKAGES = (f"linux-image-{RT}", RT_KERNEL_PACKAGE)


def grub_cfg(params=" ".join(host_prep.BOOT_PARAMS)):
    """A generated grub.cfg with a generic kernel and a real-time kernel."""
    return (
        "menuentry 'Ubuntu' {\n\tlinux\t/boot/vmlinuz-7.0.0-42-generic root=/dev/sda1 ro quiet splash\n}\n"
        f"menuentry 'Ubuntu, with Linux {RT}' {{\n\tlinux\t/boot/vmlinuz-{RT} root=/dev/sda1 ro quiet {params}\n}}\n"
    )


class FakeRunner:
    """Pretends to be the vendor installer by copying the runtime trees to a fake install root."""

    def __init__(
        self,
        installed_root: Path,
        *,
        install_code=0,
        version_code=0,
        service="active",
        cpupower_code=0,
        installer_code=0,
    ):
        self.installed_root = installed_root
        self.install_code = install_code
        self.version_code = version_code
        self.service = service
        self.cpupower_code = cpupower_code
        self.installer_code = installer_code
        self.packages = {"bash"}
        self.hook = None
        self.streamed = []
        self.captured = []

    def stream(self, argv, log_path, cwd):
        self.streamed.append((list(argv), Path(cwd)))
        with open(log_path, "a") as log:
            log.write("vendor log\n")
        if self.hook is not None:
            self.hook(list(argv))
        if self.install_code == 0 and "apt-get" in argv and "install" in argv:
            self.packages.update(a for a in argv[argv.index("install") + 1 :] if not a.startswith("-"))
            if RT_KERNEL_PACKAGE in argv:
                self.packages.add(f"linux-image-{RT}")
        if self.install_code == 0 and argv[:3] == ["cmake", "--install", "libyang/build"]:
            manifest = Path(cwd) / "libyang" / "build" / "install_manifest.txt"
            manifest.parent.mkdir(parents=True, exist_ok=True)
            manifest.write_text("/usr/local/lib/libyang.so\n/usr/local/bin/yanglint\n")
        if self.install_code == 0 and argv[:2] == ["install", "-d"]:
            for target in argv[4:]:
                Path(target).mkdir(parents=True, exist_ok=True)
        if self.install_code == 0 and argv[:2] == ["install", "-D"]:
            Path(argv[-1]).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(argv[-2], argv[-1])
        if argv[-1].endswith("install.sh") and self.installer_code != 0:
            return self.installer_code
        if self.install_code == 0 and argv[-1].endswith("install.sh"):
            for tree in ("cli.dist", "daemon.dist"):
                shutil.copytree(Path(cwd) / tree, self.installed_root / tree)
        return self.install_code

    def capture(self, argv, timeout):
        self.captured.append(list(argv))
        if argv[0] == "dpkg-query":
            return 0, "".join(f"ii \t{name}\n" for name in sorted(self.packages))
        if argv[0] == "cpupower":
            return self.cpupower_code, "cpupower 7.0\n"
        if argv[-1] == "--version":
            return self.version_code, "proe 1.0.0\n"
        if argv[:2] == ["systemctl", "is-active"]:
            return (0 if self.service == "active" else 3), self.service + "\n"
        return 0, ""


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.state = self.root / "state"
        self.installed = self.root / "opt-proe"
        conf = self.root / "proe_app.conf"
        conf.write_text(syn.DEFAULT_FILES["assets/conf/proe_app.conf"].decode())
        self.conf = conf
        self.out = []
        self.boot = "boot-1"
        self.rt = True
        self.release = RT
        self.cmdline = FULL_CMDLINE
        self.missing_tools = set()
        self.grub = self.root / "grub"
        (self.grub / "grub.d").mkdir(parents=True)
        self.grub_default = self.grub / "default"
        self.grub_default.write_text(
            'GRUB_DEFAULT=0\nGRUB_CMDLINE_LINUX_DEFAULT="quiet splash"\nGRUB_CMDLINE_LINUX=""\n'
        )
        self.dropin = self.grub / "grub.d" / host_prep.DROPIN_NAME
        self.backup = self.grub / "backup"
        (self.grub / "grub.cfg").write_text(grub_cfg())

    def _env(self, runner=None, *, interactive=True, answers=(), existing=None, is_root=True, host=None):
        answers = list(answers)
        self.runner = runner or FakeRunner(self.installed)
        return inst.InstallEnvironment(
            runner=self.runner,
            host=host or HostInfo("ubuntu", "26.04", "x86_64", "Ubuntu 26.04 LTS"),
            is_root=is_root,
            interactive=interactive,
            prompt=lambda _q: answers.pop(0) if answers else "n",
            out=self.out.append,
            boot_id=lambda: self.boot,
            rt_kernel_active=lambda: self.rt,
            kernel_release=lambda: self.release,
            read_cmdline=lambda: self.cmdline,
            tool_available=lambda tool: tool not in self.missing_tools,
            grub_default=self.grub_default,
            grub_dropin_dir=self.grub / "grub.d",
            grub_backup_dir=self.backup,
            grub_cfg=self.grub / "grub.cfg",
            state_dir=self.state,
            existing_paths=existing or (lambda: [str(p) for p in (self.installed,) if p.exists()]),
            installed_root=self.installed,
            cli_path=str(self.root / "proe"),
            app_config=self.conf,
        )

    def _package(self, **kwargs):
        zip_path, sha = syn.build_package(self.root, **kwargs)
        return zip_path, sha

    def _opts(self, zip_path, sha, **kwargs):
        return inst.InstallOptions(package=zip_path, sha256=sha, **kwargs)

    def _text(self):
        return "\n".join(self.out)

    def _make_cli(self):
        cli = self.root / "proe"
        cli.write_text("#!/bin/sh\n")
        cli.chmod(0o755)

    def test_the_preview_is_shown_before_consent_and_declining_changes_nothing(self):
        zip_path, sha = self._package(capabilities=syn.CAPABILITIES)
        env = self._env(answers=["n"])
        code = inst.run_install(self._opts(zip_path, sha), env)
        self.assertEqual(code, inst.EXIT_ACTION_REQUIRED)
        self.assertEqual(self.runner.streamed, [])
        self.assertFalse(self.state.exists())
        self.assertIn("PROe installation preview", self._text())
        self.assertIn(sha, self._text())
        self.assertIn(SUPPORTED_PROFILE, self._text())

    def test_preview_states_when_no_profiles_are_supported(self):
        zip_path, sha = self._package()
        code = inst.run_install(self._opts(zip_path, sha), self._env(answers=["n"]))
        self.assertEqual(code, inst.EXIT_ACTION_REQUIRED)
        self.assertIn("NONE", self._text())

    def test_bad_checksum_runs_nothing(self):
        zip_path, _ = self._package()
        env = self._env()
        code = inst.run_install(self._opts(zip_path, "0" * 64, assume_yes=True), env)
        self.assertEqual(code, inst.EXIT_ERROR)
        self.assertEqual(self.runner.streamed, [])
        self.assertIn("Checksum mismatch", self._text())

    def test_incompatible_host_runs_nothing(self):
        zip_path, sha = self._package()
        env = self._env(host=HostInfo("ubuntu", "24.04", "x86_64", "Ubuntu 24.04.4 LTS"))
        code = inst.run_install(self._opts(zip_path, sha, assume_yes=True), env)
        self.assertEqual(code, inst.EXIT_ERROR)
        self.assertIn("26.04 or newer", self._text())
        self.assertEqual(self.runner.streamed, [])

    def test_declined_consent(self):
        zip_path, sha = self._package()
        env = self._env(answers=["n"])
        code = inst.run_install(self._opts(zip_path, sha), env)
        self.assertEqual(code, inst.EXIT_ACTION_REQUIRED)
        self.assertEqual(self.runner.streamed, [])
        self.assertIn("cancelled", self._text())

    def test_non_interactive_requires_yes(self):
        zip_path, sha = self._package()
        env = self._env(interactive=False)
        code = inst.run_install(self._opts(zip_path, sha), env)
        self.assertEqual(code, inst.EXIT_ERROR)
        self.assertEqual(self.runner.streamed, [])
        self.assertIn("Consent is required", self._text())

    def test_successful_install_with_capabilities(self):
        self._make_cli()
        zip_path, sha = self._package(capabilities=syn.CAPABILITIES)
        env = self._env(answers=["y"], existing=lambda: [])
        self.assertEqual(inst.run_install(self._opts(zip_path, sha), env), inst.EXIT_OK)

        argv, cwd = self.runner.streamed[0]
        self.assertEqual(argv[0], "bash")
        self.assertTrue(argv[1].endswith("install.sh"))
        self.assertTrue((self.installed / "cli.dist" / "proe").exists())

        record = proe_state.read_capability_record(self.state)
        self.assertEqual(record.package_sha256, sha)
        self.assertEqual([p.esq_profile for p in record.profiles], [SUPPORTED_PROFILE])
        self.assertEqual(record.results_dir, "/var/log/proe/benchmark_results")
        self.assertEqual(proe_state.read_install_state(self.state).state, proe_state.STATE_INSTALLED)
        self.assertFalse(list(self.state.glob("stage-*")), "staging must be removed after success")

    def test_install_without_capabilities_is_not_ready(self):
        self._make_cli()
        zip_path, sha = self._package()
        env = self._env()
        self.assertEqual(inst.run_install(self._opts(zip_path, sha, assume_yes=True), env), inst.EXIT_OK)
        record = proe_state.read_capability_record(self.state)
        self.assertEqual(record.profiles, ())
        self.assertIn("Ready            : NO", self._text())

    def test_sudo_is_used_when_not_root(self):
        zip_path, sha = self._package()
        env = self._env(is_root=False, interactive=True, existing=lambda: [])
        self._make_cli()
        inst.run_install(self._opts(zip_path, sha, assume_yes=True), env)
        self.assertEqual(self.runner.streamed[0][0][:2], ["sudo", "--"])

    def test_sudo_never_prompts_when_non_interactive(self):
        zip_path, sha = self._package()
        env = self._env(is_root=False, interactive=False, existing=lambda: [])
        self._make_cli()
        inst.run_install(self._opts(zip_path, sha, assume_yes=True), env)
        self.assertEqual(self.runner.streamed[0][0][:3], ["sudo", "-n", "--"])

    def test_vendor_failure_is_reported_and_recorded(self):
        zip_path, sha = self._package()
        env = self._env(runner=FakeRunner(self.installed, install_code=7), existing=lambda: [])
        code = inst.run_install(self._opts(zip_path, sha, assume_yes=True), env)
        self.assertEqual(code, inst.EXIT_ERROR)
        self.assertIn("exit code 7", self._text())
        self.assertEqual(proe_state.read_install_state(self.state).state, proe_state.STATE_FAILED)
        self.assertIsNone(proe_state.read_capability_record(self.state))

    def test_inactive_service_blocks_capability_record(self):
        self._make_cli()
        zip_path, sha = self._package(capabilities=syn.CAPABILITIES)
        env = self._env(runner=FakeRunner(self.installed, service="inactive"), existing=lambda: [])
        code = inst.run_install(self._opts(zip_path, sha, assume_yes=True), env)
        self.assertEqual(code, inst.EXIT_ERROR)
        self.assertIsNone(proe_state.read_capability_record(self.state))

    def test_cli_version_failure_blocks_capability_record(self):
        zip_path, sha = self._package(capabilities=syn.CAPABILITIES)
        env = self._env(runner=FakeRunner(self.installed, version_code=1), existing=lambda: [])
        code = inst.run_install(self._opts(zip_path, sha, assume_yes=True), env)
        self.assertEqual(code, inst.EXIT_ERROR)
        self.assertIsNone(proe_state.read_capability_record(self.state))

    def test_rerun_is_idempotent(self):
        self._make_cli()
        zip_path, sha = self._package(capabilities=syn.CAPABILITIES)
        env = self._env(existing=lambda: [])
        self.assertEqual(inst.run_install(self._opts(zip_path, sha, assume_yes=True), env), inst.EXIT_OK)
        first = self.runner
        env = self._env()  # the installed directory now exists
        self.assertEqual(inst.run_install(self._opts(zip_path, sha, assume_yes=True), env), inst.EXIT_OK)
        self.assertEqual(self.runner.streamed, [], "the vendor installer must not run again")
        self.assertEqual(first.streamed.__len__(), 1)
        self.assertIn("already installed", self._text())

    def test_rerun_detects_modified_installation(self):
        self._make_cli()
        zip_path, sha = self._package(capabilities=syn.CAPABILITIES)
        inst.run_install(self._opts(zip_path, sha, assume_yes=True), self._env(existing=lambda: []))
        (self.installed / "cli.dist" / "proe").write_bytes(b"modified")
        code = inst.run_install(self._opts(zip_path, sha, assume_yes=True), self._env())
        self.assertEqual(code, inst.EXIT_ERROR)
        self.assertIn("do not match the package", self._text())

    def test_existing_unknown_installation_is_a_conflict(self):
        zip_path, sha = self._package()
        env = self._env(existing=lambda: ["/opt/proe"])
        code = inst.run_install(self._opts(zip_path, sha, assume_yes=True), env)
        self.assertEqual(code, inst.EXIT_ERROR)
        self.assertEqual(self.runner.streamed, [])
        self.assertIn("never overwrites or removes", self._text())

    def test_different_package_with_existing_install_is_a_conflict(self):
        self._make_cli()
        zip_path, sha = self._package(capabilities=syn.CAPABILITIES)
        inst.run_install(self._opts(zip_path, sha, assume_yes=True), self._env(existing=lambda: []))
        other_dir = self.root / "other"
        other_dir.mkdir()
        zip2, sha2 = syn.build_package(
            other_dir, capabilities=syn.CAPABILITIES, provenance_overrides={"createdUtc": "x"}
        )
        code = inst.run_install(self._opts(zip2, sha2, assume_yes=True), self._env())
        self.assertEqual(code, inst.EXIT_ERROR)
        self.assertEqual(self.runner.streamed, [])

    def test_interrupted_install_is_reported_as_such(self):
        zip_path, sha = self._package()
        env = self._env(runner=FakeRunner(self.installed, install_code=1), existing=lambda: [])
        inst.run_install(self._opts(zip_path, sha, assume_yes=True), env)
        self.installed.mkdir(exist_ok=True)
        code = inst.run_install(self._opts(zip_path, sha, assume_yes=True), self._env())
        self.assertEqual(code, inst.EXIT_ERROR)
        self.assertIn("did not complete", self._text())

    def _reboot_caps(self):
        caps = dict(syn.CAPABILITIES, requires_reboot=True)
        return caps

    def test_reboot_requires_separate_consent_and_resumes(self):
        self._make_cli()
        zip_path, sha = self._package(capabilities=self._reboot_caps())
        env = self._env(answers=["y", "n"], existing=lambda: [])
        code = inst.run_install(self._opts(zip_path, sha), env)
        self.assertEqual(code, inst.EXIT_ACTION_REQUIRED)
        self.assertFalse(any(c[:2] == ["systemctl", "reboot"] for c in self.runner.captured))
        self.assertEqual(proe_state.read_install_state(self.state).state, proe_state.STATE_AWAITING_REBOOT)
        self.assertIsNone(proe_state.read_capability_record(self.state))

        # Same boot: still waiting.
        code = inst.run_install(self._opts(zip_path, sha, assume_yes=True), self._env())
        self.assertEqual(code, inst.EXIT_ACTION_REQUIRED)
        self.assertIn("waiting for a reboot", self._text())

        # After a reboot the same command finishes without running the vendor installer again.
        self.boot = "boot-2"
        env = self._env()
        self.assertEqual(inst.run_install(self._opts(zip_path, sha, assume_yes=True), env), inst.EXIT_OK)
        self.assertEqual(self.runner.streamed, [])
        self.assertEqual(len(proe_state.read_capability_record(self.state).profiles), 1)

    def test_yes_does_not_imply_reboot_consent(self):
        self._make_cli()
        zip_path, sha = self._package(capabilities=self._reboot_caps())
        env = self._env(interactive=False, existing=lambda: [])
        code = inst.run_install(self._opts(zip_path, sha, assume_yes=True), env)
        self.assertEqual(code, inst.EXIT_ACTION_REQUIRED)
        self.assertFalse(any(c[:2] == ["systemctl", "reboot"] for c in self.runner.captured))

    def test_allow_reboot_reboots(self):
        self._make_cli()
        zip_path, sha = self._package(capabilities=self._reboot_caps())
        env = self._env(interactive=False, existing=lambda: [])
        inst.run_install(self._opts(zip_path, sha, assume_yes=True, allow_reboot=True), env)
        self.assertTrue(any(c[:2] == ["systemctl", "reboot"] for c in self.runner.captured))

    def _commands(self):
        return [a for a, _ in self.runner.streamed]

    def _rebooted(self):
        return any(c[:2] == ["systemctl", "reboot"] for c in self.runner.captured)

    def test_preview_states_that_host_is_not_prepared(self):
        self.rt = False
        self.cmdline = STOCK_CMDLINE
        zip_path, sha = self._package()
        code = inst.run_install(self._opts(zip_path, sha), self._env(answers=["n"]))
        self.assertEqual(code, inst.EXIT_ACTION_REQUIRED)
        text = self._text()
        self.assertIn("NOT running", text)
        self.assertIn(f"apt-get install -y {RT_KERNEL_PACKAGE}", text)
        self.assertIn("default boot entry", text)
        self.assertIn("isolcpus=1,2", text)
        self.assertIn(host_prep.DROPIN_NAME, text)
        self.assertIn("REQUIRED", text)
        self.assertEqual(self.runner.streamed, [])
        self.assertFalse(self.dropin.exists())

    def test_preview_names_the_running_kernel_but_no_kernel_version_to_install(self):
        self.rt = False
        self.release = "7.0.0-42-generic"
        self.cmdline = STOCK_CMDLINE
        zip_path, sha = self._package()
        inst.run_install(self._opts(zip_path, sha), self._env(answers=["n"]))
        self.assertIn("this machine runs 7.0.0-42-generic", self._text())
        self.assertIn("newest real-time kernel of this Ubuntu release", self._text())

    def test_any_running_real_time_kernel_needs_no_kernel_change(self):
        for release in (RT, "7.0.0-99-realtime", "6.8.1-1015-realtime"):
            with self.subTest(release=release):
                self.release = release
                self.out.clear()
                zip_path, sha = self._package()
                self._make_cli()
                env = self._env(existing=lambda: [])
                self.assertEqual(inst.run_install(self._opts(zip_path, sha, assume_yes=True), env), inst.EXIT_OK)
                self.assertIn("Real-time kernel : running", self._text())
                self.assertFalse(any(RT_KERNEL_PACKAGE in c for c in self._commands()))
                shutil.rmtree(self.installed, ignore_errors=True)
                shutil.rmtree(self.state, ignore_errors=True)

    def test_apt_index_is_refreshed_so_that_the_latest_real_time_kernel_is_installed(self):
        self.rt = False
        self.cmdline = STOCK_CMDLINE
        zip_path, sha = self._package()
        inst.run_install(self._opts(zip_path, sha, assume_yes=True), self._env(existing=lambda: []))
        commands = self._commands()
        update = ["env", "DEBIAN_FRONTEND=noninteractive", "apt-get", "update"]
        install = ["env", "DEBIAN_FRONTEND=noninteractive", "apt-get", "install", "-y", RT_KERNEL_PACKAGE]
        self.assertEqual(commands.index(install), commands.index(update) + 1)

    def test_host_preparation_is_recorded_for_uninstall(self):
        self.rt = False
        self.cmdline = STOCK_CMDLINE
        zip_path, sha = self._package()
        env = self._env(interactive=False, existing=lambda: [])
        inst.run_install(self._opts(zip_path, sha, assume_yes=True), env)
        record = host_changes.read_host_changes(self.state)
        self.assertEqual(record.kernel_packages, RT_PACKAGES)
        self.assertEqual(record.tool_packages, ())
        self.assertEqual(record.grub_dropin, str(self.dropin))
        self.assertEqual(record.grub_backup, str(self.backup))

    def test_host_tools_and_libyang_are_recorded_for_uninstall(self):
        self._make_cli()
        self.missing_tools = {"tuna", "yanglint"}
        zip_path, sha = self._package()
        env = self._env(existing=lambda: [])
        self.runner.hook = lambda argv: self.missing_tools.clear() if argv[0] == "ldconfig" else None
        inst.run_install(self._opts(zip_path, sha, assume_yes=True), env)
        record = host_changes.read_host_changes(self.state)
        self.assertIn("tuna", record.tool_packages)
        self.assertIn("cmake", record.tool_packages)
        self.assertNotIn("bash", record.tool_packages)
        self.assertEqual(record.libyang_manifest, host_changes.LIBYANG_MANIFEST_FILE)
        self.assertEqual(
            host_changes.libyang_files(self.state, record),
            ["/usr/local/lib/libyang.so", "/usr/local/bin/yanglint"],
        )

    def test_packages_that_were_already_installed_are_not_recorded(self):
        self._make_cli()
        self.missing_tools = {"tuna"}
        zip_path, sha = self._package()
        runner = FakeRunner(self.installed)
        runner.packages.add("tuna")
        env = self._env(runner=runner, existing=lambda: [])
        runner.hook = lambda argv: self.missing_tools.clear() if "install" in argv else None
        inst.run_install(self._opts(zip_path, sha, assume_yes=True), env)
        self.assertEqual(host_changes.read_host_changes(self.state).tool_packages, ())

    def _pending_uninstall(self, zip_path, sha, boot):
        name = "proe-binary-ubuntu26.04-x86_64-12538f28"
        state = proe_state.InstallState(proe_state.STATE_UNINSTALL_REBOOT, sha, name, boot)
        proe_state.write_install_state(state, self.state)

    def test_install_is_refused_while_an_uninstall_waits_for_a_reboot(self):
        zip_path, sha = self._package()
        self._pending_uninstall(zip_path, sha, boot=self.boot)
        env = self._env(interactive=False, existing=lambda: [])
        code = inst.run_install(self._opts(zip_path, sha, assume_yes=True), env)
        self.assertEqual(code, inst.EXIT_ERROR)
        self.assertIn("is waiting for a reboot of this machine", self._text())
        self.assertIn("esq uninstall proe", self._text())
        self.assertEqual(self.runner.streamed, [])

    def test_after_the_reboot_the_message_asks_for_the_uninstall_not_another_reboot(self):
        zip_path, sha = self._package()
        self._pending_uninstall(zip_path, sha, boot="boot-before-the-reboot")
        env = self._env(interactive=False, existing=lambda: [])
        code = inst.run_install(self._opts(zip_path, sha, assume_yes=True), env)
        self.assertEqual(code, inst.EXIT_ERROR)
        self.assertIn("already rebooted", self._text())
        self.assertIn("run 'esq uninstall proe' to finish it", self._text())
        self.assertNotIn("waiting for a reboot", self._text())
        self.assertEqual(self.runner.streamed, [])

    def test_host_preparation_declined_changes_nothing(self):
        self.rt = False
        self.cmdline = STOCK_CMDLINE
        zip_path, sha = self._package()
        env = self._env(answers=["n"], existing=lambda: [])
        code = inst.run_install(self._opts(zip_path, sha), env)
        self.assertEqual(code, inst.EXIT_ACTION_REQUIRED)
        self.assertEqual(self.runner.streamed, [])
        self.assertFalse(self.state.exists())
        self.assertFalse(self.dropin.exists())
        self.assertIn("cancelled", self._text())

    def test_one_confirmation_prepares_host_and_reboots(self):
        self.rt = False
        self.cmdline = STOCK_CMDLINE
        zip_path, sha = self._package()
        prompts = []
        env = self._env(answers=["y"], existing=lambda: [])
        original = env.prompt
        env.prompt = lambda q: (prompts.append(q), original(q))[1]
        code = inst.run_install(self._opts(zip_path, sha), env)
        self.assertEqual(code, inst.EXIT_OK)
        self.assertEqual(len(prompts), 1, "one confirmation must cover the kernel, the boot parameters and the reboot")
        self.assertIn("REBOOT", prompts[0])
        commands = self._commands()
        kernel = ["env", "DEBIAN_FRONTEND=noninteractive", "apt-get", "install", "-y", RT_KERNEL_PACKAGE]
        apt = commands.index(kernel)
        self.assertEqual(commands[apt - 1], ["env", "DEBIAN_FRONTEND=noninteractive", "apt-get", "update"])
        self.assertEqual(commands[0][:4], ["install", "-d", "-m", "0700"], "the GRUB backup comes first")
        self.assertTrue(
            all(c[0] in ("install", "cp") for c in commands[: apt - 1]), "only backup steps before the kernel"
        )
        self.assertIn(["cp", "-a", str(self.grub_default), str(self.backup / "default-grub")], commands[:apt])
        self.assertIn(["cp", "/proc/cmdline", str(self.backup / "cmdline-before")], commands[:apt])
        self.assertEqual(commands[apt + 1][:4], ["install", "-D", "-m", "0644"])
        self.assertEqual(commands[apt + 1][-1], str(self.dropin))
        self.assertEqual(commands[apt + 2], ["update-grub"])
        self.assertEqual(len(commands), apt + 4)
        self.assertTrue(commands[-1][-1].endswith("install.sh"), "PROe is installed after the host preparation")
        content = self.dropin.read_text()
        self.assertIn('GRUB_CMDLINE_LINUX="$GRUB_CMDLINE_LINUX intel_iommu=on iommu=pt isolcpus=1,2', content)
        self.assertIn("xe.force_probe=*", content)
        self.assertNotIn("GRUB_DEFAULT", content, "the ubuntu-realtime package already makes the RT kernel default")
        self.assertEqual(self.runner.captured[-1][:2], ["systemctl", "reboot"], "the reboot is the last step")
        self.assertEqual(proe_state.read_install_state(self.state).state, proe_state.STATE_INSTALLED)
        self.assertTrue((self.installed / "cli.dist" / "proe").exists())
        self.assertFalse((self.state / "grub-dropin.tmp").exists())
        self.assertIn("To revert", self._text())

    def test_yes_covers_host_preparation_and_reboot(self):
        self.rt = False
        self.cmdline = STOCK_CMDLINE
        zip_path, sha = self._package()
        env = self._env(interactive=False, existing=lambda: [])
        code = inst.run_install(self._opts(zip_path, sha, assume_yes=True), env)
        self.assertEqual(code, inst.EXIT_OK)
        self.assertTrue(self._rebooted())

    def test_only_boot_parameters_when_rt_kernel_already_runs(self):
        self.cmdline = STOCK_CMDLINE
        zip_path, sha = self._package()
        env = self._env(interactive=False, existing=lambda: [])
        code = inst.run_install(self._opts(zip_path, sha, assume_yes=True), env)
        self.assertEqual(code, inst.EXIT_OK)
        commands = self._commands()
        self.assertFalse(any("apt-get" in c for c in commands))
        self.assertEqual([c[0] for c in commands][-3:], ["install", "update-grub", "bash"])
        self.assertTrue(self.backup.exists())
        self.assertTrue(self._rebooted())

    def test_conflicting_boot_parameter_is_backed_up_then_replaced(self):
        self.rt = False
        self.cmdline = STOCK_CMDLINE
        original = 'GRUB_CMDLINE_LINUX="isolcpus=3 nosmt"\n'
        self.grub_default.write_text(original)
        zip_path, sha = self._package()
        code = inst.run_install(self._opts(zip_path, sha, assume_yes=True), self._env(existing=lambda: []))
        self.assertEqual(code, inst.EXIT_OK)
        self.assertIn("replaces: isolcpus=1,2 (already configured as isolcpus=3)", self._text())
        self.assertEqual(self.grub_default.read_text(), original, "the user's own GRUB file is not edited")
        self.assertIn(["cp", "-a", str(self.grub_default), str(self.backup / "default-grub")], self._commands())
        content = self.dropin.read_text()
        self.assertIn('"isolcpus="*', content)
        self.assertIn("isolcpus=1,2", content)
        self.assertTrue(self._rebooted())

    def test_existing_backup_is_kept_as_the_original(self):
        self.cmdline = STOCK_CMDLINE
        self.backup.mkdir()
        zip_path, sha = self._package()
        inst.run_install(self._opts(zip_path, sha, assume_yes=True), self._env(existing=lambda: []))
        self.assertFalse(any(c[0] == "cp" for c in self._commands()))
        self.assertIn("already backed up", self._text())

    def test_other_grub_dropins_are_backed_up_but_not_our_own(self):
        self.cmdline = STOCK_CMDLINE
        other = self.grub / "grub.d" / "50-other.cfg"
        other.write_text("GRUB_TIMEOUT=5\n")
        self.dropin.write_text("# left by an earlier run\n")
        zip_path, sha = self._package()
        inst.run_install(self._opts(zip_path, sha, assume_yes=True), self._env(existing=lambda: []))
        commands = self._commands()
        self.assertIn(["cp", "-a", str(other), str(self.backup / "grub.d" / "50-other.cfg")], commands)
        self.assertFalse(any(c[0] == "cp" and host_prep.DROPIN_NAME in " ".join(c) for c in commands))

    def test_already_configured_parameter_is_not_duplicated(self):
        self.cmdline = STOCK_CMDLINE
        self.grub_default.write_text('GRUB_CMDLINE_LINUX="iommu=pt"\n')
        zip_path, sha = self._package()
        inst.run_install(self._opts(zip_path, sha, assume_yes=True), self._env(existing=lambda: []))
        content = self.dropin.read_text()
        self.assertNotIn(" iommu=pt", content)
        self.assertIn("intel_iommu=on", content)

    def test_host_preparation_failure_is_reported(self):
        self.rt = False
        self.cmdline = STOCK_CMDLINE
        zip_path, sha = self._package()
        env = self._env(runner=FakeRunner(self.installed, install_code=100), existing=lambda: [])
        code = inst.run_install(self._opts(zip_path, sha, assume_yes=True), env)
        self.assertEqual(code, inst.EXIT_ERROR)
        self.assertIn("exit code 100", self._text())
        self.assertIsNone(proe_state.read_install_state(self.state))
        self.assertFalse(self._rebooted())

    def test_one_run_installs_everything_and_a_rerun_checks_the_host(self):
        self._make_cli()
        self.rt = False
        self.cmdline = STOCK_CMDLINE
        zip_path, sha = self._package(capabilities=syn.CAPABILITIES)
        code = inst.run_install(self._opts(zip_path, sha, assume_yes=True), self._env(existing=lambda: []))
        self.assertEqual(code, inst.EXIT_OK)
        self.assertTrue(self._rebooted())
        self.assertIn("after the reboot", self._text())
        self.assertEqual(len(proe_state.read_capability_record(self.state).profiles), 1)

        # Not rebooted yet: nothing runs, the host is reported as not ready.
        env = self._env()
        self.assertEqual(inst.run_install(self._opts(zip_path, sha, assume_yes=True), env), inst.EXIT_ACTION_REQUIRED)
        self.assertEqual(self.runner.streamed, [])
        self.assertIn("not running a real-time kernel", self._text())

        # Rebooted into a generic kernel: explain, still nothing runs.
        self.boot = "boot-2"
        env = self._env()
        self.assertEqual(inst.run_install(self._opts(zip_path, sha, assume_yes=True), env), inst.EXIT_ACTION_REQUIRED)
        self.assertEqual(self.runner.streamed, [])

        # Real-time kernel runs but the boot parameters did not apply: explain.
        self.rt = True
        env = self._env()
        self.assertEqual(inst.run_install(self._opts(zip_path, sha, assume_yes=True), env), inst.EXIT_ACTION_REQUIRED)
        self.assertIn("command line is missing", self._text())

        # Everything active: ready, and nothing was asked or run again.
        self.cmdline = FULL_CMDLINE
        env = self._env()
        self.assertEqual(inst.run_install(self._opts(zip_path, sha), env), inst.EXIT_OK)
        self.assertEqual(self.runner.streamed, [])
        self.assertIn("Host readiness  : ready", self._text())

    def test_preview_lists_the_whole_sequence_and_asks_once(self):
        self.rt = False
        self.cmdline = STOCK_CMDLINE
        zip_path, sha = self._package()
        inst.run_install(self._opts(zip_path, sha), self._env(answers=["n"]))
        text = self._text()
        self.assertIn("a real-time kernel and a reboot are REQUIRED", text)
        self.assertIn("performance targets", text)
        self.assertIn("check the generated GRUB configuration", text)
        self.assertIn("reboot this machine, only if every check passed", text)
        self.assertIn("This one command sets up PROe", text)

    def test_sanity_check_of_the_boot_configuration_blocks_the_reboot(self):
        self.rt = False
        self.cmdline = STOCK_CMDLINE
        (self.grub / "grub.cfg").write_text(grub_cfg(params="quiet"))
        zip_path, sha = self._package()
        code = inst.run_install(self._opts(zip_path, sha, assume_yes=True), self._env(existing=lambda: []))
        self.assertEqual(code, inst.EXIT_ERROR)
        self.assertIn("sanity check after the host preparation failed", self._text())
        self.assertIn("boot parameter isolcpus=1,2 is not on any real-time kernel entry", self._text())
        self.assertFalse(self._rebooted())
        self.assertFalse(self.installed.exists(), "PROe is installed only after the host preparation checks out")
        self.assertEqual(host_changes.read_host_changes(self.state).grub_dropin, str(self.dropin))

    def test_a_missing_real_time_kernel_entry_stops_before_the_reboot(self):
        self.rt = False
        self.cmdline = STOCK_CMDLINE
        (self.grub / "grub.cfg").write_text(
            "menuentry 'Ubuntu' {\n\tlinux\t/boot/vmlinuz-7.0.0-42-generic root=/dev/sda1 ro quiet splash\n}\n"
        )
        zip_path, sha = self._package()
        code = inst.run_install(self._opts(zip_path, sha, assume_yes=True), self._env(existing=lambda: []))
        self.assertEqual(code, inst.EXIT_ERROR)
        self.assertIn("it has no real-time kernel entry", self._text())
        self.assertIn("esq uninstall proe", self._text())
        self.assertFalse(self._rebooted())
        self.assertFalse(self.installed.exists())
        self.assertEqual(host_changes.read_host_changes(self.state).kernel_packages, RT_PACKAGES, "revertible")

    def test_sanity_check_passes_with_a_matching_boot_configuration(self):
        self.rt = False
        self.cmdline = STOCK_CMDLINE
        zip_path, sha = self._package()
        code = inst.run_install(self._opts(zip_path, sha, assume_yes=True), self._env(existing=lambda: []))
        self.assertEqual(code, inst.EXIT_OK)
        self.assertIn("[4/7] Checking the GRUB configuration ... ok", self._text())
        self.assertIn("boot parameters are in place", self._text())
        self.assertTrue(self._rebooted())

    def test_failed_vendor_installer_means_no_reboot(self):
        self.rt = False
        self.cmdline = STOCK_CMDLINE
        zip_path, sha = self._package()
        env = self._env(runner=FakeRunner(self.installed, installer_code=7), existing=lambda: [])
        code = inst.run_install(self._opts(zip_path, sha, assume_yes=True), env)
        self.assertEqual(code, inst.EXIT_ERROR)
        self.assertIn("exit code 7", self._text())
        self.assertFalse(self._rebooted())
        self.assertEqual(proe_state.read_install_state(self.state).state, proe_state.STATE_FAILED)

    def test_package_reboot_and_host_preparation_share_one_reboot(self):
        self._make_cli()
        self.rt = False
        self.cmdline = STOCK_CMDLINE
        zip_path, sha = self._package(capabilities=self._reboot_caps())
        env = self._env(interactive=False, existing=lambda: [])
        code = inst.run_install(self._opts(zip_path, sha, assume_yes=True), env)
        self.assertEqual(code, inst.EXIT_ACTION_REQUIRED)
        self.assertEqual(sum(c[:2] == ["systemctl", "reboot"] for c in self.runner.captured), 1)
        self.assertEqual(proe_state.read_install_state(self.state).state, proe_state.STATE_AWAITING_REBOOT)

    def test_cpupower_is_checked_after_the_reboot_when_the_kernel_changes(self):
        self.rt = False
        self.cmdline = STOCK_CMDLINE
        zip_path, sha = self._package()
        env = self._env(runner=FakeRunner(self.installed, cpupower_code=1), existing=lambda: [])
        code = inst.run_install(self._opts(zip_path, sha, assume_yes=True), env)
        self.assertEqual(code, inst.EXIT_OK)
        self.assertFalse(any(c[0] == "cpupower" for c in self.runner.captured))

    def test_missing_host_tools_are_installed_before_the_vendor_installer(self):
        self._make_cli()
        self.missing_tools = {"tuna", "yanglint"}
        zip_path, sha = self._package()
        env = self._env(existing=lambda: [])
        self.runner.hook = lambda argv: self.missing_tools.clear() if argv[0] == "ldconfig" else None
        code = inst.run_install(self._opts(zip_path, sha, assume_yes=True), env)
        self.assertEqual(code, inst.EXIT_OK)
        commands = self._commands()
        self.assertEqual(commands[0], ["env", "DEBIAN_FRONTEND=noninteractive", "apt-get", "update"])
        self.assertIn("tuna", commands[1])
        self.assertIn("cmake", commands[1])
        names = [c[0] for c in commands]
        self.assertEqual(names[2:7], ["git", "cmake", "cmake", "cmake", "ldconfig"])
        self.assertIn(host_prep.LIBYANG_TAG, commands[2])
        self.assertTrue(commands[-1][-1].endswith("install.sh"))
        build_dirs = [p.name for p in self.state.glob("libyang-*") if p.is_dir()]
        self.assertEqual(build_dirs, [], "build directory must be removed")

    def test_dependency_failure_stops_before_the_vendor_installer(self):
        self.missing_tools = {"tuna"}
        zip_path, sha = self._package()
        env = self._env(runner=FakeRunner(self.installed, install_code=100), existing=lambda: [])
        code = inst.run_install(self._opts(zip_path, sha, assume_yes=True), env)
        self.assertEqual(code, inst.EXIT_ERROR)
        self.assertIn("exit code 100", self._text())
        self.assertFalse(self.installed.exists())

    def test_cpupower_must_work_with_the_running_kernel(self):
        zip_path, sha = self._package()
        env = self._env(runner=FakeRunner(self.installed, cpupower_code=1), existing=lambda: [])
        code = inst.run_install(self._opts(zip_path, sha, assume_yes=True), env)
        self.assertEqual(code, inst.EXIT_ERROR)
        self.assertIn("cpupower", self._text())
        self.assertEqual(self.runner.streamed, [])

    def test_progress_lines_name_each_phase_in_order(self):
        self.rt = False
        self.cmdline = STOCK_CMDLINE
        zip_path, sha = self._package()
        inst.run_install(self._opts(zip_path, sha, assume_yes=True), self._env(existing=lambda: []))
        text = self._text()
        expected = [
            "[1/7] Backing up the GRUB configuration ... ok",
            f"[2/7] Installing the real-time kernel ({RT_KERNEL_PACKAGE}) ... ok",
            "[3/7] Setting the boot parameters ... ok",
            "[4/7] Checking the GRUB configuration ... ok",
            "[5/7] Verifying a private copy of the package ... ok",
            "[6/7] Installing PROe ... ok",
            "[7/7] Verifying the installation ... ok",
        ]
        positions = [text.index(line) for line in expected]
        self.assertEqual(positions, sorted(positions))
        self.assertIn(f"      + linux-image-{RT}", text)

    def test_the_commands_and_their_output_go_to_the_log(self):
        self.rt = False
        self.cmdline = STOCK_CMDLINE
        zip_path, sha = self._package()
        inst.run_install(self._opts(zip_path, sha, assume_yes=True), self._env(existing=lambda: []))
        log = (self.state / "install.log").read_text()
        install = f"$ env DEBIAN_FRONTEND=noninteractive apt-get install -y {RT_KERNEL_PACKAGE}"
        self.assertIn(install, log)
        self.assertIn("$ update-grub", log)
        self.assertIn("vendor log", log)
        self.assertIn("Log              : " + str(self.state / "install.log"), self._text())

    def test_a_failure_shows_the_last_lines_of_the_output(self):
        zip_path, sha = self._package()
        env = self._env(runner=FakeRunner(self.installed, installer_code=7), existing=lambda: [])
        code = inst.run_install(self._opts(zip_path, sha, assume_yes=True), env)
        self.assertEqual(code, inst.EXIT_ERROR)
        self.assertIn("Installing PROe ... FAILED", self._text())
        self.assertIn("Last lines of the output:", self._text())
        self.assertIn("    vendor log", self._text())

    def test_a_failing_apt_step_shows_its_output_too(self):
        self.missing_tools = {"tuna"}
        zip_path, sha = self._package()
        env = self._env(runner=FakeRunner(self.installed, install_code=100), existing=lambda: [])
        code = inst.run_install(self._opts(zip_path, sha, assume_yes=True), env)
        self.assertEqual(code, inst.EXIT_ERROR)
        self.assertIn("Installing the host tools: tuna ... FAILED", self._text())
        self.assertIn("    vendor log", self._text())

    def test_sudo_is_asked_for_once_before_the_first_phase(self):
        zip_path, sha = self._package()
        self._make_cli()
        env = self._env(is_root=False, interactive=True, existing=lambda: [])
        inst.run_install(self._opts(zip_path, sha, assume_yes=True), env)
        self.assertEqual(self.runner.captured[0][:2], ["sudo", "-v"])
        self.assertLess(self._text().index("sudo may ask"), self._text().index("[1/"))

    def test_the_environment_prints_notes_directly_without_progress(self):
        env = self._env()
        with env.phase("Anything") as phase:
            phase.note("a note")
        self.assertEqual(self.out, ["a note"])


def explain(esq_profile, directory):
    """Why resolve_profile refuses a profile, or None when it resolves."""
    try:
        resolve_profile(esq_profile, directory)
    except ProeError as exc:
        return str(exc)
    return None


class ProfileResolutionTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        cli = self.dir / "proe"
        cli.write_text("x")
        cli.chmod(0o755)
        self.cli = cli

    def _publish(self, profiles=(SUPPORTED_PROFILE,), state=proe_state.STATE_INSTALLED, directory=None):
        directory = directory or self.dir
        sha = "a" * 64
        record = proe_state.CapabilityRecord(
            "pkg",
            sha,
            "c" * 40,
            str(self.cli),
            tuple(proe_state.RecordedProfile(p, "1ms_ai", "1.0") for p in profiles),
            "/var/log/proe/benchmark_results",
        )
        proe_state.write_capability_record(record, directory)
        proe_state.write_install_state(proe_state.InstallState(state, sha, "pkg"), directory)

    def test_not_installed(self):
        self.assertIn("not installed", explain(SUPPORTED_PROFILE, self.dir))

    def test_resolves_when_verified(self):
        self._publish()
        self.assertIsNone(explain(SUPPORTED_PROFILE, self.dir))

    def test_only_declared_profiles_are_available(self):
        self._publish()
        self.assertIn("does not support", explain("profile.suite.realtime.proe_control", self.dir))

    def test_incomplete_install_is_refused(self):
        self._publish(state=proe_state.STATE_AWAITING_REBOOT)
        self.assertIn("incomplete", explain(SUPPORTED_PROFILE, self.dir))

    def test_missing_executable_is_refused(self):
        self._publish()
        self.cli.unlink()
        self.assertIn("missing", explain(SUPPORTED_PROFILE, self.dir))

    def test_tampered_record_with_forged_profile_is_rejected(self):
        self._publish()
        path = self.dir / "capability.json"
        path.write_text(path.read_text().replace(SUPPORTED_PROFILE, "profile.suite.evil"))
        with self.assertRaises(proe_state.StateError):
            proe_state.read_capability_record(self.dir)
        self.assertIsNotNone(explain(SUPPORTED_PROFILE, self.dir))

    def test_corrupt_record_is_refused(self):
        self._publish()
        (self.dir / "capability.json").write_text("{not json")
        self.assertIsNotNone(explain(SUPPORTED_PROFILE, self.dir))



if __name__ == "__main__":
    unittest.main()
