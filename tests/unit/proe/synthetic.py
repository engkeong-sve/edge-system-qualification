# Copyright (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Builders for synthetic PROe-style packages. They contain only placeholder text, never a real payload."""

import hashlib
import json
import stat
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

PACKAGE_NAME = "proe-binary-ubuntu26.04-x86_64-aaaaaaaa"

DEFAULT_FILES: Dict[str, bytes] = {
    "install.sh": b"#!/bin/bash\necho synthetic installer\n",
    "uninstall.sh": b"#!/bin/bash\necho synthetic uninstaller\n",
    "assets/ubuntu-version": b"26.04\n",
    "assets/conf/proe_app.conf": b"Results:\n    ResultsDirectory: /var/log/proe/benchmark_results\n",
    "cli.dist/proe": b"synthetic cli placeholder",
    "daemon.dist/proe": b"synthetic daemon placeholder",
}

CAPABILITIES = {
    "schema": "esq-proe-capabilities/1",
    "requires_reboot": False,
    "system_changes": ["synthetic change"],
    "profiles": [
        {
            "esq_profile": "profile.suite.realtime.proe_1ms_ai_single_dut",
            "proe_profile": "1ms_ai",
            "topology": "single-dut-dual-nic",
            "report_schema": "1.0",
        }
    ],
}


def sha256_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _info(name: str, mode: int, kind: int = stat.S_IFREG) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name)
    info.create_system = 3
    info.external_attr = ((kind | mode) & 0xFFFF) << 16
    return info


def build_package(
    directory: Path,
    *,
    root: str = PACKAGE_NAME,
    files: Optional[Dict[str, bytes]] = None,
    capabilities: Optional[Dict[str, Any]] = None,
    provenance_overrides: Optional[Dict[str, Any]] = None,
    extra_entries: Optional[List[Tuple[zipfile.ZipInfo, bytes]]] = None,
    drop_from_manifest: Tuple[str, ...] = (),
    tamper: Optional[Dict[str, bytes]] = None,
) -> Tuple[Path, str]:
    """Write a ZIP and return its path and SHA-256.

    ``tamper`` replaces archive content after the manifest was computed (integrity violation).
    """
    content = dict(DEFAULT_FILES if files is None else files)
    if capabilities is not None:
        content["esq-capabilities.json"] = json.dumps(capabilities).encode()

    manifest_files = {
        name: {"sha256": sha256_of(data), "size": len(data), "mode": "0o755" if name.endswith(".sh") else "0o644"}
        for name, data in content.items()
        if name not in drop_from_manifest
    }
    provenance: Dict[str, Any] = {
        "package": root,
        "source": {"commit": "a" * 40},
        "createdUtc": "2026-01-01T00:00:00Z",
        "files": manifest_files,
        "symlinks": {},
    }
    provenance.update(provenance_overrides or {})

    zip_path = directory / "pkg.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(_info(f"{root}/", 0o755, stat.S_IFDIR), b"")
        archive.writestr(_info(f"{root}/PROVENANCE.json", 0o644), json.dumps(provenance).encode())
        for name, data in content.items():
            payload = (tamper or {}).get(name, data)
            archive.writestr(_info(f"{root}/{name}", 0o755 if name.endswith(".sh") else 0o644), payload)
        for info, data in extra_entries or []:
            archive.writestr(info, data)
    return zip_path, sha256_of(zip_path.read_bytes())


def symlink_info(name: str) -> zipfile.ZipInfo:
    return _info(name, 0o777, stat.S_IFLNK)


def plain_info(name: str, mode: int = 0o644) -> zipfile.ZipInfo:
    return _info(name, mode)
