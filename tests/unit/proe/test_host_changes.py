# Copyright (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

import json
import tempfile
import unittest
from pathlib import Path

from esq.utils.proe import host_changes
from esq.utils.proe.constants import StateError

DROPIN = "/etc/default/grub.d/99-esq-proe.cfg"
BACKUP = "/var/backups/esq/proe-grub"


class HostChangesTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)

    def _write(self, **overrides):
        data = host_changes.HostChanges().to_dict()
        data.update(overrides)
        host_changes.host_changes_path(self.dir).write_text(json.dumps(data), encoding="utf-8")

    def test_missing_record_is_none(self):
        self.assertIsNone(host_changes.read_host_changes(self.dir))

    def test_updates_merge_packages_and_replace_other_values(self):
        host_changes.update_host_changes(self.dir, kernel_packages=["b", "a"], grub_backup=BACKUP)
        host_changes.update_host_changes(self.dir, kernel_packages=["c"], tool_packages=["tuna"], grub_dropin=DROPIN)
        record = host_changes.read_host_changes(self.dir)
        self.assertEqual(record.kernel_packages, ("a", "b", "c"))
        self.assertEqual(record.tool_packages, ("tuna",))
        self.assertEqual(record.grub_dropin, DROPIN)
        self.assertEqual(record.grub_backup, BACKUP)
        self.assertEqual(record.libyang_manifest, "")

    def test_remove_deletes_the_record_and_the_manifest(self):
        host_changes.update_host_changes(self.dir, tool_packages=["tuna"])
        (self.dir / host_changes.LIBYANG_MANIFEST_FILE).write_text("/usr/local/lib/a.so\n")
        host_changes.remove_host_changes(self.dir)
        self.assertEqual(list(self.dir.iterdir()), [])

    def test_corrupt_or_foreign_records_are_rejected(self):
        host_changes.host_changes_path(self.dir).write_text("{not json")
        with self.assertRaises(StateError):
            host_changes.read_host_changes(self.dir)
        host_changes.host_changes_path(self.dir).write_text("[]")
        with self.assertRaises(StateError):
            host_changes.read_host_changes(self.dir)
        self._write(schema="something-else")
        with self.assertRaises(StateError):
            host_changes.read_host_changes(self.dir)

    def test_unsafe_package_names_are_rejected(self):
        for bad in ("-y", "tuna; reboot", "Tuna", "", "a b", "../x"):
            self._write(tool_packages=[bad])
            with self.assertRaises(StateError, msg=bad):
                host_changes.read_host_changes(self.dir)
        self._write(kernel_packages="tuna")
        with self.assertRaises(StateError):
            host_changes.read_host_changes(self.dir)

    def test_dropin_must_be_the_esq_drop_in(self):
        self._write(grub_dropin="/etc/default/grub")
        with self.assertRaises(StateError):
            host_changes.read_host_changes(self.dir)
        self._write(grub_dropin="/etc/passwd")
        with self.assertRaises(StateError):
            host_changes.read_host_changes(self.dir)
        self._write(grub_dropin=DROPIN)
        self.assertEqual(host_changes.read_host_changes(self.dir).grub_dropin, DROPIN)

    def test_backup_must_be_a_deep_absolute_path(self):
        for bad in ("relative/dir", "/", "/etc", "/var/backups"):
            self._write(grub_backup=bad)
            with self.assertRaises(StateError, msg=bad):
                host_changes.read_host_changes(self.dir)

    def test_manifest_name_is_fixed(self):
        self._write(libyang_manifest="../../etc/passwd")
        with self.assertRaises(StateError):
            host_changes.read_host_changes(self.dir)

    def test_libyang_files_are_validated_and_deduplicated(self):
        host_changes.update_host_changes(self.dir, libyang_manifest=host_changes.LIBYANG_MANIFEST_FILE)
        manifest = self.dir / host_changes.LIBYANG_MANIFEST_FILE
        manifest.write_text("/usr/local/lib/libyang.so\n\n/usr/local/bin/yanglint\n/usr/local/lib/libyang.so\n")
        record = host_changes.read_host_changes(self.dir)
        self.assertEqual(
            host_changes.libyang_files(self.dir, record),
            ["/usr/local/lib/libyang.so", "/usr/local/bin/yanglint"],
        )

    def test_libyang_files_outside_the_root_are_rejected(self):
        host_changes.update_host_changes(self.dir, libyang_manifest=host_changes.LIBYANG_MANIFEST_FILE)
        record = host_changes.read_host_changes(self.dir)
        manifest = self.dir / host_changes.LIBYANG_MANIFEST_FILE
        bad_paths = (
            "/etc/passwd",
            "/usr/local/../etc/passwd",
            "relative/path",
            "/usr/local/lib/",
            "/usr/localx/lib.so",
        )
        for bad in bad_paths:
            manifest.write_text(f"/usr/local/lib/ok.so\n{bad}\n")
            with self.assertRaises(StateError, msg=bad):
                host_changes.libyang_files(self.dir, record)

    def test_no_manifest_means_no_files(self):
        self.assertEqual(host_changes.libyang_files(self.dir, host_changes.HostChanges()), [])


if __name__ == "__main__":
    unittest.main()
