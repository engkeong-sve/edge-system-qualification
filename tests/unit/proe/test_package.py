# Copyright (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

import os
import stat
import tempfile
import unittest
import warnings
import zipfile
from pathlib import Path

from esq.utils.proe import package as pkg
from esq.utils.proe.constants import PackageError

from . import synthetic as syn


class PackageValidationTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)

    def _inspect(self, zip_path, sha, dest=None):
        pkg.verify_checksum(zip_path, sha)
        return pkg.inspect_archive(zip_path, sha, dest)

    def test_valid_package(self):
        zip_path, sha = syn.build_package(self.dir)
        manifest = self._inspect(zip_path, sha)
        self.assertEqual(manifest.package_name, syn.PACKAGE_NAME)
        self.assertEqual(manifest.os_version, "24.04")
        self.assertEqual(manifest.arch, "x86_64")
        self.assertEqual(manifest.source_commit, "a" * 40)
        self.assertIsNone(manifest.capabilities)

    def test_checksum_mismatch(self):
        zip_path, _ = syn.build_package(self.dir)
        with self.assertRaisesRegex(PackageError, "Checksum mismatch"):
            pkg.verify_checksum(zip_path, "0" * 64)

    def test_checksum_must_be_hex_sha256(self):
        zip_path, _ = syn.build_package(self.dir)
        with self.assertRaises(PackageError):
            pkg.verify_checksum(zip_path, "abc")

    def test_checksum_is_case_insensitive(self):
        zip_path, sha = syn.build_package(self.dir)
        self.assertEqual(pkg.verify_checksum(zip_path, sha.upper()), sha)

    def test_staged_copy_verifies_and_removes_on_mismatch(self):
        zip_path, sha = syn.build_package(self.dir)
        staged, digest = pkg.stage_verified_copy(zip_path, sha, self.dir / "stage")
        self.assertEqual(digest, sha)
        self.assertTrue(staged.exists())
        with self.assertRaises(PackageError):
            pkg.stage_verified_copy(zip_path, "1" * 64, self.dir / "stage2")
        self.assertFalse((self.dir / "stage2" / "package.zip").exists())

    def test_not_a_zip(self):
        bogus = self.dir / "bogus.zip"
        bogus.write_bytes(b"not a zip")
        with self.assertRaisesRegex(PackageError, "not a readable ZIP"):
            pkg.inspect_archive(bogus, syn.sha256_of(b"not a zip"))

    def test_missing_file_rejected(self):
        with self.assertRaisesRegex(PackageError, "cannot be read"):
            pkg.verify_checksum(self.dir / "absent.zip", "0" * 64)

    def test_directory_rejected(self):
        with self.assertRaisesRegex(PackageError, "regular file"):
            pkg.verify_checksum(self.dir, "0" * 64)

    def test_path_traversal_rejected(self):
        extra = [(syn.plain_info(f"{syn.PACKAGE_NAME}/../evil"), b"x")]
        zip_path, sha = syn.build_package(self.dir, extra_entries=extra)
        with self.assertRaisesRegex(PackageError, "Unsafe archive path"):
            self._inspect(zip_path, sha)

    def test_absolute_path_rejected(self):
        zip_path, sha = syn.build_package(self.dir, extra_entries=[(syn.plain_info("/etc/evil"), b"x")])
        with self.assertRaisesRegex(PackageError, "Unsafe archive path"):
            self._inspect(zip_path, sha)

    def test_backslash_and_drive_paths_rejected(self):
        names = ["C:evil"]
        if os.sep == "/":  # Python's zipfile rewrites backslashes itself on Windows
            names.append(f"{syn.PACKAGE_NAME}\\evil")
        for name in names:
            with self.subTest(name=name):
                zip_path, sha = syn.build_package(self.dir, extra_entries=[(syn.plain_info(name), b"x")])
                with self.assertRaisesRegex(PackageError, "Unsafe archive path"):
                    self._inspect(zip_path, sha)

    def test_symlink_rejected(self):
        extra = [(syn.symlink_info(f"{syn.PACKAGE_NAME}/link"), b"/etc/passwd")]
        zip_path, sha = syn.build_package(self.dir, extra_entries=extra)
        with self.assertRaisesRegex(PackageError, "Link or special file"):
            self._inspect(zip_path, sha)

    def test_duplicate_entry_rejected(self):
        extra = [(syn.plain_info(f"{syn.PACKAGE_NAME}/install.sh"), b"other")]
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)  # zipfile warns while writing the duplicate
            zip_path, sha = syn.build_package(self.dir, extra_entries=extra)
        with self.assertRaisesRegex(PackageError, "Duplicate"):
            self._inspect(zip_path, sha)

    def test_second_top_level_directory_rejected(self):
        zip_path, sha = syn.build_package(self.dir, extra_entries=[(syn.plain_info("other/file"), b"x")])
        with self.assertRaisesRegex(PackageError, "exactly one top-level"):
            self._inspect(zip_path, sha)

    def test_root_name_must_match_manifest(self):
        zip_path, sha = syn.build_package(self.dir, provenance_overrides={"package": "something-else"})
        with self.assertRaisesRegex(PackageError, "does not match the package directory"):
            self._inspect(zip_path, sha)

    def test_unexpected_package_name_rejected(self):
        zip_path, sha = syn.build_package(self.dir, root="not-a-proe-package")
        with self.assertRaisesRegex(PackageError, "naming scheme"):
            self._inspect(zip_path, sha)

    def test_undeclared_file_rejected(self):
        extra = [(syn.plain_info(f"{syn.PACKAGE_NAME}/surprise.bin"), b"x")]
        zip_path, sha = syn.build_package(self.dir, extra_entries=extra)
        with self.assertRaisesRegex(PackageError, "not in the integrity list"):
            self._inspect(zip_path, sha)

    def test_declared_but_missing_file_rejected(self):
        zip_path, sha = syn.build_package(
            self.dir,
            provenance_overrides={"files": {"ghost": {"sha256": "0" * 64, "size": 1, "mode": "0o644"}}},
        )
        with self.assertRaises(PackageError):
            self._inspect(zip_path, sha)

    def test_tampered_content_rejected(self):
        zip_path, sha = syn.build_package(self.dir, tamper={"cli.dist/proe": b"synthetic cli placehold3r"})
        with self.assertRaisesRegex(PackageError, "Integrity check failed|larger than declared"):
            self._inspect(zip_path, sha)

    def test_tampered_longer_content_rejected(self):
        zip_path, sha = syn.build_package(self.dir, tamper={"cli.dist/proe": b"x" * 4096})
        with self.assertRaises(PackageError):
            self._inspect(zip_path, sha)

    def test_missing_required_layout_rejected(self):
        files = {k: v for k, v in syn.DEFAULT_FILES.items() if k != "daemon.dist/proe"}
        zip_path, sha = syn.build_package(self.dir, files=files)
        with self.assertRaisesRegex(PackageError, "required file"):
            self._inspect(zip_path, sha)

    def test_os_version_file_must_match_name(self):
        files = dict(syn.DEFAULT_FILES)
        files["assets/ubuntu-version"] = b"26.04\n"
        zip_path, sha = syn.build_package(self.dir, files=files)
        with self.assertRaisesRegex(PackageError, "inconsistent"):
            self._inspect(zip_path, sha)

    def test_setuid_mode_in_manifest_rejected(self):
        zip_path, sha = syn.build_package(
            self.dir,
            provenance_overrides={
                "files": {"install.sh": {"sha256": "0" * 64, "size": 1, "mode": "0o4755"}},
            },
        )
        with self.assertRaisesRegex(PackageError, "Unsafe file mode"):
            self._inspect(zip_path, sha)

    def test_declared_symlinks_rejected(self):
        zip_path, sha = syn.build_package(self.dir, provenance_overrides={"symlinks": {"a": "b"}})
        with self.assertRaisesRegex(PackageError, "symbolic links"):
            self._inspect(zip_path, sha)

    def test_manifest_cannot_list_itself(self):
        files = dict(syn.DEFAULT_FILES)
        zip_path, sha = syn.build_package(
            self.dir,
            provenance_overrides={
                "files": {"PROVENANCE.json": {"sha256": "0" * 64, "size": 1, "mode": "0o644"}},
            },
            files=files,
        )
        with self.assertRaises(PackageError):
            self._inspect(zip_path, sha)

    def test_inspection_writes_nothing(self):
        zip_path, sha = syn.build_package(self.dir)
        before = set(self.dir.iterdir())
        self._inspect(zip_path, sha)
        self.assertEqual(set(self.dir.iterdir()), before)

    def test_extraction_is_verified_and_strips_group_write(self):
        zip_path, sha = syn.build_package(self.dir)
        dest = self.dir / "out"
        manifest = self._inspect(zip_path, sha, dest)
        pkg.reverify_extracted(dest, manifest)
        mode = (dest / "install.sh").stat().st_mode
        if os.name == "posix":
            self.assertFalse(mode & (stat.S_IWGRP | stat.S_IWOTH))
        (dest / "cli.dist" / "proe").write_bytes(b"changed")
        with self.assertRaisesRegex(PackageError, "changed after verification"):
            pkg.reverify_extracted(dest, manifest)

    def test_extraction_contains_only_package_files(self):
        zip_path, sha = syn.build_package(self.dir)
        dest = self.dir / "out"
        self._inspect(zip_path, sha, dest)
        found = {p.relative_to(dest).as_posix() for p in dest.rglob("*") if p.is_file()}
        self.assertEqual(found, set(syn.DEFAULT_FILES))

    def test_oversized_declared_entry_rejected(self):
        original = pkg.MAX_MEMBER_BYTES
        pkg.MAX_MEMBER_BYTES = 10
        self.addCleanup(setattr, pkg, "MAX_MEMBER_BYTES", original)
        zip_path, sha = syn.build_package(self.dir)
        with self.assertRaisesRegex(PackageError, "too large"):
            self._inspect(zip_path, sha)

    def test_too_many_entries_rejected(self):
        original = pkg.MAX_ARCHIVE_ENTRIES
        pkg.MAX_ARCHIVE_ENTRIES = 3
        self.addCleanup(setattr, pkg, "MAX_ARCHIVE_ENTRIES", original)
        zip_path, sha = syn.build_package(self.dir)
        with self.assertRaisesRegex(PackageError, "too many entries"):
            self._inspect(zip_path, sha)

    def test_encrypted_entry_rejected(self):
        zip_path, sha = syn.build_package(self.dir)
        data = bytearray(zip_path.read_bytes())
        # Set the encryption flag in the first central directory header.
        marker = data.index(b"PK\x01\x02")
        data[marker + 8] |= 0x1
        zip_path.write_bytes(bytes(data))
        with self.assertRaisesRegex(PackageError, "Encrypted"):
            pkg.inspect_archive(zip_path, syn.sha256_of(bytes(data)))

    def test_special_file_type_rejected(self):
        info = zipfile.ZipInfo(f"{syn.PACKAGE_NAME}/fifo")
        info.create_system = 3
        info.external_attr = (stat.S_IFIFO | 0o644) << 16
        zip_path, sha = syn.build_package(self.dir, extra_entries=[(info, b"")])
        with self.assertRaisesRegex(PackageError, "Link or special file"):
            self._inspect(zip_path, sha)


class CompatibilityTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        zip_path, sha = syn.build_package(Path(self._tmp.name))
        self.manifest = pkg.inspect_archive(zip_path, sha)

    def test_matching_host(self):
        host = pkg.HostInfo("ubuntu", "24.04", "x86_64")
        self.assertEqual(pkg.check_host_compatibility(self.manifest, host), [])

    def test_other_ubuntu_release(self):
        problems = pkg.check_host_compatibility(self.manifest, pkg.HostInfo("ubuntu", "26.04", "x86_64"))
        self.assertTrue(any("built for Ubuntu 24.04" in p for p in problems))

    def test_unsupported_os(self):
        problems = pkg.check_host_compatibility(self.manifest, pkg.HostInfo("fedora", "40", "x86_64"))
        self.assertTrue(any("Unsupported operating system" in p for p in problems))

    def test_wrong_architecture(self):
        problems = pkg.check_host_compatibility(self.manifest, pkg.HostInfo("ubuntu", "24.04", "aarch64"))
        self.assertTrue(any("architecture" in p for p in problems))


class CapabilityTests(unittest.TestCase):
    def _caps(self, **overrides):
        data = {
            "schema": "esq-proe-capabilities/1",
            "profiles": [
                {
                    "esq_profile": "profile.suite.realtime.proe_1ms_ai",
                    "proe_profile": "1ms_ai",
                    "topology": "single-dut",
                    "report_schema": "1.0",
                }
            ],
        }
        data.update(overrides)
        return data

    def test_valid(self):
        caps = pkg.parse_capabilities(self._caps(requires_reboot=True, system_changes=["x"]))
        self.assertTrue(caps.requires_reboot)
        self.assertEqual([p.proe_profile for p in caps.profiles], ["1ms_ai"])

    def test_forged_profile_name_is_not_exposed(self):
        entry = dict(self._caps()["profiles"][0], esq_profile="profile.suite.evil")
        caps = pkg.parse_capabilities(self._caps(profiles=[entry]))
        self.assertEqual(caps.profiles, ())
        self.assertIn("not defined by this ESQ version", caps.unsupported[0])

    def test_profile_path_cannot_be_declared(self):
        entry = dict(self._caps()["profiles"][0], path="../../etc/x")
        with self.assertRaises(PackageError):
            pkg.parse_capabilities(self._caps(profiles=[entry]))

    def test_multi_target_topology_not_supported(self):
        entry = dict(self._caps()["profiles"][0], topology="two-host")
        caps = pkg.parse_capabilities(self._caps(profiles=[entry]))
        self.assertEqual(caps.profiles, ())
        self.assertIn("topology", caps.unsupported[0])

    def test_unknown_report_schema_not_supported(self):
        entry = dict(self._caps()["profiles"][0], report_schema="9.9")
        caps = pkg.parse_capabilities(self._caps(profiles=[entry]))
        self.assertEqual(caps.profiles, ())

    def test_invalid_proe_identifier(self):
        entry = dict(self._caps()["profiles"][0], proe_profile="1ms; rm -rf /")
        with self.assertRaises(PackageError):
            pkg.parse_capabilities(self._caps(profiles=[entry]))

    def test_unknown_top_level_field(self):
        with self.assertRaises(PackageError):
            pkg.parse_capabilities(self._caps(extra=1))

    def test_wrong_schema(self):
        with self.assertRaises(PackageError):
            pkg.parse_capabilities(self._caps(schema="other"))

    def test_duplicate_profile(self):
        entry = self._caps()["profiles"][0]
        with self.assertRaisesRegex(PackageError, "more than once"):
            pkg.parse_capabilities(self._caps(profiles=[entry, dict(entry)]))

    def test_reboot_must_be_boolean(self):
        with self.assertRaises(PackageError):
            pkg.parse_capabilities(self._caps(requires_reboot="yes"))

    def test_capabilities_inside_package_are_integrity_checked(self):
        with tempfile.TemporaryDirectory() as tmp:
            zip_path, sha = syn.build_package(
                Path(tmp), capabilities=syn.CAPABILITIES, tamper={"esq-capabilities.json": b"{}"}
            )
            with self.assertRaises(PackageError):
                pkg.inspect_archive(zip_path, sha)
            zip_path, sha = syn.build_package(Path(tmp), capabilities=syn.CAPABILITIES)
            manifest = pkg.inspect_archive(zip_path, sha)
            self.assertEqual(len(manifest.capabilities.profiles), 1)


if __name__ == "__main__":
    unittest.main()
