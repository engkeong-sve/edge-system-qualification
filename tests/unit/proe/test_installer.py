# Copyright (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

import shutil
import tempfile
import unittest
from pathlib import Path

from esq.utils.proe import installer as inst
from esq.utils.proe import state as proe_state
from esq.utils.proe.availability import explain, is_available
from esq.utils.proe.package import HostInfo

from . import synthetic as syn

SUPPORTED_PROFILE = "profile.suite.realtime.proe_1ms_ai"


class FakeRunner:
    """Pretends to be the vendor installer by copying the runtime trees to a fake install root."""

    def __init__(self, installed_root: Path, *, install_code=0, version_code=0, service="active"):
        self.installed_root = installed_root
        self.install_code = install_code
        self.version_code = version_code
        self.service = service
        self.streamed = []
        self.captured = []

    def stream(self, argv, log_path, cwd):
        self.streamed.append((list(argv), Path(cwd)))
        Path(log_path).write_text("vendor log\n")
        if self.install_code == 0:
            for tree in ("cli.dist", "daemon.dist"):
                shutil.copytree(Path(cwd) / tree, self.installed_root / tree)
        return self.install_code

    def capture(self, argv, timeout):
        self.captured.append(list(argv))
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

    def _env(self, runner=None, *, interactive=True, answers=(), existing=None, is_root=True, host=None):
        answers = list(answers)
        self.runner = runner or FakeRunner(self.installed)
        return inst.InstallEnvironment(
            runner=self.runner,
            host=host or HostInfo("ubuntu", "24.04", "x86_64", "Ubuntu 24.04.4 LTS"),
            is_root=is_root,
            interactive=interactive,
            prompt=lambda _q: answers.pop(0) if answers else "n",
            out=self.out.append,
            boot_id=lambda: self.boot,
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

    def test_dry_run_changes_nothing(self):
        zip_path, sha = self._package(capabilities=syn.CAPABILITIES)
        env = self._env()
        code = inst.run_install(self._opts(zip_path, sha, dry_run=True), env)
        self.assertEqual(code, inst.EXIT_OK)
        self.assertEqual(self.runner.streamed, [])
        self.assertFalse(self.state.exists())
        self.assertIn("PROe installation preview", self._text())
        self.assertIn(sha, self._text())
        self.assertIn(SUPPORTED_PROFILE, self._text())

    def test_preview_states_when_no_profiles_are_supported(self):
        zip_path, sha = self._package()
        code = inst.run_install(self._opts(zip_path, sha, dry_run=True), self._env())
        self.assertEqual(code, inst.EXIT_OK)
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
        env = self._env(host=HostInfo("ubuntu", "26.04", "x86_64"))
        code = inst.run_install(self._opts(zip_path, sha, assume_yes=True), env)
        self.assertEqual(code, inst.EXIT_ERROR)
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
        zip2, sha2 = syn.build_package(other_dir, capabilities=syn.CAPABILITIES, provenance_overrides={"createdUtc": "x"})
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

    def test_dry_run_with_existing_install_does_not_modify_state(self):
        self._make_cli()
        zip_path, sha = self._package(capabilities=syn.CAPABILITIES)
        inst.run_install(self._opts(zip_path, sha, assume_yes=True), self._env(existing=lambda: []))
        record_before = (self.state / "capability.json").read_text()
        self.assertEqual(inst.run_install(self._opts(zip_path, sha, dry_run=True), self._env()), inst.EXIT_OK)
        self.assertEqual((self.state / "capability.json").read_text(), record_before)

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


class AvailabilityTests(unittest.TestCase):
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

    def test_available_when_verified(self):
        self._publish()
        self.assertIsNone(explain(SUPPORTED_PROFILE, self.dir))

    def test_only_declared_profiles_are_available(self):
        self._publish()
        self.assertIn("does not support", explain("profile.suite.realtime.proe_control", self.dir))

    def test_incomplete_install_hides_profiles(self):
        self._publish(state=proe_state.STATE_AWAITING_REBOOT)
        self.assertIn("incomplete", explain(SUPPORTED_PROFILE, self.dir))

    def test_missing_executable_hides_profiles(self):
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

    def test_corrupt_record_hides_profiles(self):
        self._publish()
        (self.dir / "capability.json").write_text("{not json")
        self.assertIsNotNone(explain(SUPPORTED_PROFILE, self.dir))

    def test_entry_point_function_uses_state_dir(self):
        import os
        from unittest import mock

        with mock.patch.dict(os.environ, {"ESQ_STATE_DIR": str(self.dir / "home")}):
            self.assertFalse(is_available(SUPPORTED_PROFILE, {}))
            self._publish(directory=self.dir / "home" / "proe")
            self.assertTrue(is_available(SUPPORTED_PROFILE, {}))
            self.assertFalse(is_available("profile.suite.realtime.proe_control", {}))


if __name__ == "__main__":
    unittest.main()
