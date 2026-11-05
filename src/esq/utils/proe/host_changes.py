# Copyright (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""
Record of the system changes that ``esq install proe`` made.

``esq uninstall proe`` reads this record to undo exactly those changes and nothing else. The uninstaller acts
with administrator privileges, so every value is validated when it is read.
"""

import json
import posixpath
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from esq.utils.proe.constants import StateError
from esq.utils.proe.host_prep import DROPIN_NAME
from esq.utils.proe.state import atomic_write_json, is_absolute_path

HOST_CHANGES_FILE = "host-changes.json"
HOST_CHANGES_SCHEMA = "esq-proe-host-changes/1"
LIBYANG_MANIFEST_FILE = "libyang-install-manifest.txt"

_PACKAGE_RE = re.compile(r"^[a-z0-9][a-z0-9+.\-]*$")
LIBYANG_ROOT = "/usr/local/"


@dataclass(frozen=True)
class HostChanges:
    """What the installer added to the host. Empty values mean that nothing was recorded for that item."""

    kernel_packages: tuple[str, ...] = ()
    tool_packages: tuple[str, ...] = ()
    libyang_manifest: str = ""
    grub_dropin: str = ""
    grub_backup: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": HOST_CHANGES_SCHEMA,
            "kernel_packages": list(self.kernel_packages),
            "tool_packages": list(self.tool_packages),
            "libyang_manifest": self.libyang_manifest,
            "grub_dropin": self.grub_dropin,
            "grub_backup": self.grub_backup,
        }


def host_changes_path(directory: Path) -> Path:
    return Path(directory) / HOST_CHANGES_FILE


def _parts(path: str) -> list[str]:
    return [part for part in path.replace("\\", "/").split("/") if part]


def _packages(value: Any, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) and _PACKAGE_RE.match(item) for item in value):
        raise StateError(f"The PROe host-change record is invalid ({label}); remove it to start over.")
    return tuple(value)


def _validated(data: dict[str, Any]) -> HostChanges:
    if data.get("schema") != HOST_CHANGES_SCHEMA:
        raise StateError("The PROe host-change record is not recognized; remove it to start over.")
    manifest = data.get("libyang_manifest", "")
    dropin = data.get("grub_dropin", "")
    backup = data.get("grub_backup", "")
    if manifest not in ("", LIBYANG_MANIFEST_FILE):
        raise StateError("The PROe host-change record is invalid (libyang manifest); remove it to start over.")
    if not isinstance(dropin, str) or (
        dropin and (Path(dropin).name != DROPIN_NAME or Path(dropin).parent.name != "grub.d")
    ):
        raise StateError("The PROe host-change record is invalid (GRUB drop-in); remove it to start over.")
    if not isinstance(backup, str) or (backup and (not is_absolute_path(backup) or len(_parts(backup)) < 3)):
        raise StateError("The PROe host-change record is invalid (GRUB backup); remove it to start over.")
    return HostChanges(
        kernel_packages=_packages(data.get("kernel_packages", []), "kernel packages"),
        tool_packages=_packages(data.get("tool_packages", []), "tool packages"),
        libyang_manifest=manifest,
        grub_dropin=dropin,
        grub_backup=backup,
    )


def read_host_changes(directory: Path) -> HostChanges | None:
    """Read and validate the record. Returns ``None`` when no record exists."""
    path = host_changes_path(directory)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise StateError(f"{path} cannot be read: {exc}") from exc
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise StateError(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise StateError(f"{path} must contain a JSON object.")
    return _validated(data)


def update_host_changes(
    directory: Path,
    *,
    kernel_packages: Sequence[str] = (),
    tool_packages: Sequence[str] = (),
    libyang_manifest: str | None = None,
    grub_dropin: str | None = None,
    grub_backup: str | None = None,
) -> HostChanges:
    """Add to the record (packages are merged, other values replaced) and write it."""
    current = read_host_changes(directory) or HostChanges()
    merged = HostChanges(
        kernel_packages=tuple(sorted(set(current.kernel_packages) | set(kernel_packages))),
        tool_packages=tuple(sorted(set(current.tool_packages) | set(tool_packages))),
        libyang_manifest=current.libyang_manifest if libyang_manifest is None else libyang_manifest,
        grub_dropin=current.grub_dropin if grub_dropin is None else grub_dropin,
        grub_backup=current.grub_backup if grub_backup is None else grub_backup,
    )
    _validated(merged.to_dict())
    atomic_write_json(host_changes_path(directory), merged.to_dict())
    return merged


def libyang_files(directory: Path, changes: HostChanges, root: str = LIBYANG_ROOT) -> list[str]:
    """Files that the libyang build installed, from the saved manifest, validated to be under ``root``."""
    if not changes.libyang_manifest:
        return []
    path = Path(directory) / changes.libyang_manifest
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise StateError(f"The libyang install manifest cannot be read ({path}): {exc}") from exc
    files: list[str] = []
    for raw in lines:
        entry = raw.strip()
        if not entry:
            continue
        parts = entry.split("/")
        if (
            not posixpath.isabs(entry)
            or not entry.startswith(root)
            or ".." in parts
            or entry.endswith("/")
            or "\x00" in entry
        ):
            raise StateError(f"The libyang install manifest lists a path outside {root}: {entry!r}")
        if entry not in files:
            files.append(entry)
    return files


def remove_host_changes(directory: Path) -> None:
    """Delete the record and the saved libyang manifest."""
    Path(directory, HOST_CHANGES_FILE).unlink(missing_ok=True)
    Path(directory, LIBYANG_MANIFEST_FILE).unlink(missing_ok=True)
