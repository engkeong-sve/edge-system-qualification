# Copyright (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""
Persistent PROe installation state and the verified capability record.

Two small JSON files live in the user's ESQ state directory:

- ``install-state.json``  progress of an installation (supports recovery and reboot resume)
- ``capability.json``     written only after a verified installation; it is the single source of
                          truth for which ESQ profiles are available

Both are written atomically with owner-only permissions and are validated on every read.
"""

import json
import os
import posixpath
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from esq.utils.proe.constants import (
    CAPABILITY_RECORD_SCHEMA,
    INSTALL_STATE_SCHEMA,
    KNOWN_PROFILES,
    PROE_PROFILE_ID_RE,
    REPORT_CONTRACTS,
    SHA256_RE,
    TOPOLOGY_SINGLE_DUT,
    StateError,
)

STATE_AWAITING_REBOOT = "awaiting-reboot"
STATE_UNINSTALL_REBOOT = "uninstall-awaiting-reboot"
STATE_INSTALLING = "installing"
STATE_INSTALLED = "installed"
STATE_FAILED = "failed"
_VALID_STATES = {
    STATE_AWAITING_REBOOT,
    STATE_UNINSTALL_REBOOT,
    STATE_INSTALLING,
    STATE_INSTALLED,
    STATE_FAILED,
}


def is_absolute_path(path: str) -> bool:
    """Whether ``path`` is absolute. Recorded paths are Linux paths, so POSIX rules always apply."""
    return posixpath.isabs(path) or os.path.isabs(path)


def state_dir() -> Path:
    """Return the per-user state directory for the PROe integration (not created)."""
    base = os.environ.get("ESQ_STATE_DIR")
    if base:
        return Path(base).expanduser() / "proe"
    xdg = os.environ.get("XDG_STATE_HOME")
    root = Path(xdg).expanduser() if xdg else Path.home() / ".local" / "state"
    return root / "esq" / "proe"


def ensure_state_dir() -> Path:
    """Create the state directory with owner-only permissions."""
    path = state_dir()
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    return path


def atomic_write_json(path: Path, data: Dict[str, Any]) -> None:
    """Write JSON atomically with mode 0600."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise


def _read_json(path: Path) -> Optional[Dict[str, Any]]:
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
    return data


@dataclass(frozen=True)
class InstallState:
    """Installation progress."""

    state: str
    package_sha256: str
    package_name: str
    detail: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema": INSTALL_STATE_SCHEMA,
            "state": self.state,
            "package_sha256": self.package_sha256,
            "package_name": self.package_name,
            "detail": self.detail,
        }


def write_install_state(state: InstallState, directory: Optional[Path] = None) -> None:
    atomic_write_json((directory or state_dir()) / "install-state.json", state.to_dict())


def read_install_state(directory: Optional[Path] = None) -> Optional[InstallState]:
    data = _read_json((directory or state_dir()) / "install-state.json")
    if data is None:
        return None
    if data.get("schema") != INSTALL_STATE_SCHEMA or data.get("state") not in _VALID_STATES:
        raise StateError("The PROe install state file is not recognized; remove it to start over.")
    sha = data.get("package_sha256")
    name = data.get("package_name")
    if not isinstance(sha, str) or not SHA256_RE.match(sha) or not isinstance(name, str):
        raise StateError("The PROe install state file is invalid; remove it to start over.")
    return InstallState(state=data["state"], package_sha256=sha, package_name=name, detail=str(data.get("detail", "")))


@dataclass(frozen=True)
class RecordedProfile:
    esq_profile: str
    proe_profile: str
    report_schema: str


@dataclass(frozen=True)
class CapabilityRecord:
    """Verified record of an installed package and the profiles it supports."""

    package_name: str
    package_sha256: str
    source_commit: str
    executable: str
    profiles: Tuple[RecordedProfile, ...]
    results_dir: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema": CAPABILITY_RECORD_SCHEMA,
            "package_name": self.package_name,
            "package_sha256": self.package_sha256,
            "source_commit": self.source_commit,
            "executable": self.executable,
            "results_dir": self.results_dir,
            "profiles": [
                {
                    "esq_profile": p.esq_profile,
                    "proe_profile": p.proe_profile,
                    "topology": TOPOLOGY_SINGLE_DUT,
                    "report_schema": p.report_schema,
                }
                for p in self.profiles
            ],
        }

    def find(self, esq_profile: str) -> Optional[RecordedProfile]:
        for profile in self.profiles:
            if profile.esq_profile == esq_profile:
                return profile
        return None


def write_capability_record(record: CapabilityRecord, directory: Optional[Path] = None) -> None:
    atomic_write_json((directory or state_dir()) / "capability.json", record.to_dict())


def remove_capability_record(directory: Optional[Path] = None) -> None:
    (directory or state_dir()).joinpath("capability.json").unlink(missing_ok=True)


def read_capability_record(directory: Optional[Path] = None) -> Optional[CapabilityRecord]:
    """Read and strictly validate the record. Returns ``None`` when no record exists.

    Raises:
        StateError: If the record exists but is invalid. A tampered record never yields profiles.
    """
    data = _read_json((directory or state_dir()) / "capability.json")
    if data is None:
        return None
    if data.get("schema") != CAPABILITY_RECORD_SCHEMA:
        raise StateError("The PROe capability record has an unknown schema.")
    for key in ("package_name", "package_sha256", "source_commit", "executable", "results_dir"):
        if not isinstance(data.get(key), str):
            raise StateError(f"The PROe capability record is missing '{key}'.")
    if not SHA256_RE.match(data["package_sha256"]):
        raise StateError("The PROe capability record has an invalid package checksum.")
    if not is_absolute_path(data["executable"]) or not is_absolute_path(data["results_dir"]):
        raise StateError("The PROe capability record must use absolute paths.")

    raw_profiles = data.get("profiles")
    if not isinstance(raw_profiles, list):
        raise StateError("The PROe capability record has no profile list.")
    profiles = []
    for entry in raw_profiles:
        if not isinstance(entry, dict):
            raise StateError("The PROe capability record has an invalid profile entry.")
        esq_profile = entry.get("esq_profile")
        proe_profile = entry.get("proe_profile")
        schema = entry.get("report_schema")
        if (
            esq_profile not in KNOWN_PROFILES
            or not isinstance(proe_profile, str)
            or not PROE_PROFILE_ID_RE.match(proe_profile)
            or entry.get("topology") != TOPOLOGY_SINGLE_DUT
            or schema not in REPORT_CONTRACTS
        ):
            raise StateError("The PROe capability record lists a profile that this ESQ version does not support.")
        profiles.append(RecordedProfile(esq_profile, proe_profile, schema))
    return CapabilityRecord(
        package_name=data["package_name"],
        package_sha256=data["package_sha256"],
        source_commit=data["source_commit"],
        executable=data["executable"],
        profiles=tuple(profiles),
        results_dir=data["results_dir"],
    )
