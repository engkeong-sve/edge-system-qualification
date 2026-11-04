# Copyright (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""
Validation of a locally supplied PROe binary package.

Nothing in the package is executed or trusted until:

1. the SHA-256 of the ZIP file matches the checksum supplied by the user through a trusted channel,
2. every archive member passes the safety rules (no absolute or traversing paths, no links or special
   files, bounded size and count, a single package root), and
3. every member matches the integrity manifest (``PROVENANCE.json``) that ships inside the package.

The in-package manifest is only as trustworthy as the ZIP itself, so authenticity comes from the
trusted ZIP checksum. The manifest additionally detects corruption and unexpected content.
"""

import hashlib
import hmac
import json
import os
import platform
import re
import stat
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Dict, List, Optional, Tuple

from esq.utils.proe.constants import (
    CAPABILITIES_FILE,
    CAPABILITIES_SCHEMA,
    HASH_CHUNK_BYTES,
    MAX_ARCHIVE_ENTRIES,
    MAX_ARCHIVE_UNCOMPRESSED_BYTES,
    MAX_COMPRESSION_RATIO,
    MAX_JSON_MEMBER_BYTES,
    MAX_MEMBER_BYTES,
    OS_VERSION_FILE,
    PACKAGE_NAME_RE,
    PROE_PROFILE_ID_RE,
    PROVENANCE_FILE,
    REPORT_CONTRACTS,
    REQUIRED_PACKAGE_FILES,
    SHA256_RE,
    SUPPORTED_ARCH,
    SUPPORTED_OS_ID,
    SUPPORTED_OS_VERSIONS,
    TOPOLOGY_SINGLE_DUT,
    KNOWN_PROFILES,
    PackageError,
)

_DRIVE_RE = re.compile(r"^[A-Za-z]:")
_ZIP_FLAG_ENCRYPTED = 0x1


@dataclass(frozen=True)
class FileEntry:
    """Integrity record of one package file."""

    sha256: str
    size: int
    mode: int


@dataclass(frozen=True)
class ProfileCapability:
    """A profile that the package declares as supported and that ESQ also supports."""

    esq_profile: str
    proe_profile: str
    topology: str
    report_schema: str

    def to_dict(self) -> Dict[str, str]:
        return {
            "esq_profile": self.esq_profile,
            "proe_profile": self.proe_profile,
            "topology": self.topology,
            "report_schema": self.report_schema,
        }


@dataclass(frozen=True)
class Capabilities:
    """Validated content of the optional package capability declaration."""

    requires_reboot: bool = False
    system_changes: Tuple[str, ...] = ()
    profiles: Tuple[ProfileCapability, ...] = ()
    # Declared entries that this ESQ version cannot expose, with the reason.
    unsupported: Tuple[str, ...] = ()


@dataclass(frozen=True)
class PackageManifest:
    """Validated identity and content of a package."""

    zip_sha256: str
    root: str
    package_name: str
    os_version: str
    arch: str
    revision: str
    source_commit: Optional[str]
    created_utc: Optional[str]
    files: Dict[str, FileEntry]
    total_bytes: int
    capabilities: Optional[Capabilities]
    manifest_sha256: str = ""

    @property
    def capability_declared(self) -> bool:
        return self.capabilities is not None


@dataclass(frozen=True)
class HostInfo:
    """Operating system and architecture of the machine that runs the installation."""

    os_id: str
    os_version: str
    arch: str
    pretty_name: str = ""


@dataclass
class _Member:
    info: zipfile.ZipInfo
    relpath: str
    is_dir: bool


@dataclass
class _Scan:
    members: List[_Member] = field(default_factory=list)
    root: str = ""


def normalize_sha256(value: str) -> str:
    """Return a lower-case SHA-256 hex digest or raise when the text is not one."""
    if not isinstance(value, str):
        raise PackageError("The checksum must be text.")
    candidate = value.strip().lower()
    if not SHA256_RE.match(candidate):
        raise PackageError("The checksum must be a 64-character hexadecimal SHA-256 digest.")
    return candidate


def hash_file(path: Path) -> str:
    """Return the SHA-256 digest of a regular file."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(HASH_CHUNK_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_checksum(zip_path: Path, expected_sha256: str) -> str:
    """Hash the ZIP and compare it with the trusted checksum.

    Returns:
        The verified digest.

    Raises:
        PackageError: If the file is not a regular file or the digest differs.
    """
    expected = normalize_sha256(expected_sha256)
    _require_regular_file(zip_path)
    actual = hash_file(zip_path)
    if not hmac.compare_digest(actual, expected):
        raise PackageError(
            "Checksum mismatch: the package does not match the trusted SHA-256. "
            f"Expected {expected}, computed {actual}. Nothing was installed."
        )
    return actual


def stage_verified_copy(zip_path: Path, expected_sha256: str, staging_dir: Path) -> Tuple[Path, str]:
    """Copy the ZIP into a private staging directory while hashing the bytes that are copied.

    Later steps use only the staged copy, so the original cannot be replaced between the checksum
    check and the use of the package.

    Returns:
        The staged ZIP path and its verified digest.
    """
    expected = normalize_sha256(expected_sha256)
    _require_regular_file(zip_path)
    staging_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    staged = staging_dir / "package.zip"
    digest = hashlib.sha256()
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    fd = os.open(staged, flags, 0o600)
    try:
        with open(zip_path, "rb") as source, os.fdopen(fd, "wb") as target:
            for chunk in iter(lambda: source.read(HASH_CHUNK_BYTES), b""):
                digest.update(chunk)
                target.write(chunk)
    except BaseException:
        staged.unlink(missing_ok=True)
        raise
    actual = digest.hexdigest()
    if not hmac.compare_digest(actual, expected):
        staged.unlink(missing_ok=True)
        raise PackageError(
            "Checksum mismatch: the package does not match the trusted SHA-256. "
            f"Expected {expected}, computed {actual}. Nothing was installed."
        )
    return staged, actual


def inspect_archive(zip_path: Path, zip_sha256: str, dest: Optional[Path] = None) -> PackageManifest:
    """Validate an archive and optionally extract it.

    The caller must already have verified ``zip_sha256`` against the trusted checksum.

    Args:
        zip_path: ZIP file to inspect (the staged copy when extracting).
        zip_sha256: Verified digest of ``zip_path``.
        dest: Extraction directory. When ``None`` the archive is validated read-only.

    Raises:
        PackageError: On any structural, safety or integrity violation.
    """
    try:
        archive = zipfile.ZipFile(zip_path)
    except (zipfile.BadZipFile, OSError) as exc:
        raise PackageError(f"The package is not a readable ZIP archive: {exc}") from exc

    with archive:
        scan = _scan_members(archive)
        provenance_bytes = _read_small_member(archive, scan, PROVENANCE_FILE)
        provenance = _parse_json_object(provenance_bytes, PROVENANCE_FILE)
        package_name, files, source_commit, created = _parse_provenance(provenance, scan.root)
        _check_member_coverage(scan, files)
        _check_required_files(files)

        extracted_root = None
        if dest is not None:
            dest.mkdir(mode=0o700, parents=True, exist_ok=True)
            extracted_root = dest

        contents = _verify_and_extract(archive, scan, files, extracted_root)

        os_version_text = contents.get(OS_VERSION_FILE, b"").decode("ascii", errors="replace").strip()
        match = PACKAGE_NAME_RE.match(package_name)
        if match is None:
            raise PackageError(f"Package name '{package_name}' does not follow the expected naming scheme.")
        if os_version_text != match.group("os"):
            raise PackageError(
                f"Package metadata is inconsistent: {OS_VERSION_FILE} says '{os_version_text}' "
                f"but the package name says Ubuntu {match.group('os')}."
            )

        capabilities = None
        if CAPABILITIES_FILE in files:
            capabilities = parse_capabilities(
                _parse_json_object(contents[CAPABILITIES_FILE], CAPABILITIES_FILE),
            )

        total = sum(entry.size for entry in files.values())
        return PackageManifest(
            zip_sha256=zip_sha256,
            root=scan.root,
            package_name=package_name,
            os_version=match.group("os"),
            arch=match.group("arch"),
            revision=match.group("rev"),
            source_commit=source_commit,
            created_utc=created,
            files=files,
            total_bytes=total,
            capabilities=capabilities,
            manifest_sha256=hashlib.sha256(provenance_bytes).hexdigest(),
        )


def parse_capabilities(data: Dict[str, Any]) -> Capabilities:
    """Validate a package capability declaration.

    Package content can only *select* among the profiles that ESQ defines; it cannot name a profile
    path or introduce a profile. Entries that ESQ cannot expose are returned as ``unsupported``.
    """
    allowed_keys = {"schema", "requires_reboot", "system_changes", "profiles"}
    unknown = set(data) - allowed_keys
    if unknown:
        raise PackageError(f"{CAPABILITIES_FILE} has unknown field(s): {', '.join(sorted(unknown))}.")
    if data.get("schema") != CAPABILITIES_SCHEMA:
        raise PackageError(f"{CAPABILITIES_FILE} schema must be '{CAPABILITIES_SCHEMA}'.")

    requires_reboot = data.get("requires_reboot", False)
    if not isinstance(requires_reboot, bool):
        raise PackageError(f"{CAPABILITIES_FILE}: requires_reboot must be true or false.")

    changes = data.get("system_changes", [])
    if not isinstance(changes, list) or not all(isinstance(c, str) and 0 < len(c) <= 200 for c in changes):
        raise PackageError(f"{CAPABILITIES_FILE}: system_changes must be a list of short text entries.")
    if any(not c.isprintable() for c in changes):
        raise PackageError(f"{CAPABILITIES_FILE}: system_changes must contain printable text only.")

    raw_profiles = data.get("profiles")
    if not isinstance(raw_profiles, list):
        raise PackageError(f"{CAPABILITIES_FILE}: profiles must be a list.")

    supported: List[ProfileCapability] = []
    unsupported: List[str] = []
    seen = set()
    for entry in raw_profiles:
        if not isinstance(entry, dict) or set(entry) != {"esq_profile", "proe_profile", "topology", "report_schema"}:
            raise PackageError(
                f"{CAPABILITIES_FILE}: each profile needs exactly esq_profile, proe_profile, topology, report_schema."
            )
        values = [entry[key] for key in ("esq_profile", "proe_profile", "topology", "report_schema")]
        if not all(isinstance(value, str) for value in values):
            raise PackageError(f"{CAPABILITIES_FILE}: profile fields must be text.")
        esq_profile, proe_profile, topology, report_schema = values
        if not PROE_PROFILE_ID_RE.match(proe_profile):
            raise PackageError(f"{CAPABILITIES_FILE}: invalid PROe profile identifier '{proe_profile}'.")
        if esq_profile in seen:
            raise PackageError(f"{CAPABILITIES_FILE}: profile '{esq_profile}' is declared more than once.")
        seen.add(esq_profile)

        if esq_profile not in KNOWN_PROFILES:
            unsupported.append(f"{esq_profile}: not defined by this ESQ version")
        elif topology != TOPOLOGY_SINGLE_DUT:
            unsupported.append(f"{esq_profile}: unsupported topology '{topology}'")
        elif report_schema not in REPORT_CONTRACTS:
            unsupported.append(f"{esq_profile}: unsupported report schema '{report_schema}'")
        else:
            supported.append(ProfileCapability(esq_profile, proe_profile, topology, report_schema))

    return Capabilities(
        requires_reboot=requires_reboot,
        system_changes=tuple(changes),
        profiles=tuple(supported),
        unsupported=tuple(unsupported),
    )


def detect_host() -> HostInfo:
    """Read the operating system identity of this machine."""
    values: Dict[str, str] = {}
    try:
        with open("/etc/os-release", encoding="utf-8") as handle:
            for line in handle:
                key, sep, value = line.strip().partition("=")
                if sep:
                    values[key] = value.strip().strip('"')
    except OSError:
        pass
    return HostInfo(
        os_id=values.get("ID", platform.system().lower()),
        os_version=values.get("VERSION_ID", ""),
        arch=platform.machine(),
        pretty_name=values.get("PRETTY_NAME", ""),
    )


def check_host_compatibility(manifest: PackageManifest, host: HostInfo) -> List[str]:
    """Return the reasons why the package cannot be installed on ``host``. Empty means compatible."""
    problems: List[str] = []
    if host.os_id != SUPPORTED_OS_ID or host.os_version not in SUPPORTED_OS_VERSIONS:
        supported = ", ".join(SUPPORTED_OS_VERSIONS)
        problems.append(
            f"Unsupported operating system '{host.pretty_name or host.os_id + ' ' + host.os_version}'; "
            f"supported: Ubuntu {supported}."
        )
    if host.arch != SUPPORTED_ARCH:
        problems.append(f"Unsupported architecture '{host.arch}'; supported: {SUPPORTED_ARCH}.")
    if manifest.arch != host.arch:
        problems.append(f"The package targets '{manifest.arch}' but this machine is '{host.arch}'.")
    if manifest.os_version != host.os_version:
        problems.append(
            f"The package was built for Ubuntu {manifest.os_version} but this machine runs Ubuntu "
            f"'{host.os_version}'. Use the package built for this release."
        )
    return problems


def verify_installed_file(path: Path, entry: FileEntry) -> Optional[str]:
    """Compare an installed file with its manifest entry. Returns a problem text or ``None``."""
    try:
        if not path.is_file():
            return f"{path} is missing"
        if path.stat().st_size != entry.size:
            return f"{path} has an unexpected size"
        if not hmac.compare_digest(hash_file(path), entry.sha256):
            return f"{path} does not match the package checksum"
    except OSError as exc:
        return f"{path} could not be read: {exc}"
    return None


def reverify_extracted(root: Path, manifest: PackageManifest) -> None:
    """Re-check every extracted file against the manifest (done right before privileged execution)."""
    for rel, entry in manifest.files.items():
        problem = verify_installed_file(root / rel, entry)
        if problem:
            raise PackageError(f"Staged package changed after verification: {problem}.")


def _require_regular_file(path: Path) -> None:
    try:
        mode = os.stat(path).st_mode
    except OSError as exc:
        raise PackageError(f"The package file cannot be read: {exc}") from exc
    if not stat.S_ISREG(mode):
        raise PackageError("The package path must be a regular file.")


def _validate_relative_name(name: str, *, allow_trailing_slash: bool) -> str:
    if not name or "\x00" in name or "\\" in name or name.startswith("/") or _DRIVE_RE.match(name):
        raise PackageError(f"Unsafe archive path rejected: {name!r}.")
    trimmed = name[:-1] if allow_trailing_slash and name.endswith("/") else name
    parts = trimmed.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise PackageError(f"Unsafe archive path rejected: {name!r}.")
    return trimmed


def _scan_members(archive: zipfile.ZipFile) -> _Scan:
    infos = archive.infolist()
    if not infos:
        raise PackageError("The package archive is empty.")
    if len(infos) > MAX_ARCHIVE_ENTRIES:
        raise PackageError(f"The package archive has too many entries (limit {MAX_ARCHIVE_ENTRIES}).")

    scan = _Scan()
    seen = set()
    declared_total = 0
    for info in infos:
        name = info.filename
        is_dir_name = name.endswith("/")
        rel_full = _validate_relative_name(name, allow_trailing_slash=True)
        if rel_full in seen:
            raise PackageError(f"Duplicate archive entry rejected: {name!r}.")
        seen.add(rel_full)
        if info.flag_bits & _ZIP_FLAG_ENCRYPTED:
            raise PackageError(f"Encrypted archive entry rejected: {name!r}.")

        unix_mode = (info.external_attr >> 16) & 0xFFFF if info.create_system == 3 else 0
        if unix_mode:
            file_type = stat.S_IFMT(unix_mode)
            if file_type == stat.S_IFDIR:
                is_dir = True
            elif file_type == stat.S_IFREG:
                is_dir = False
            else:
                raise PackageError(f"Link or special file rejected: {name!r}.")
            if is_dir != is_dir_name:
                raise PackageError(f"Archive entry type does not match its name: {name!r}.")
        else:
            is_dir = is_dir_name

        if not is_dir:
            if info.file_size > MAX_MEMBER_BYTES:
                raise PackageError(f"Archive entry too large: {name!r}.")
            if info.file_size > 1024 * 1024 and info.file_size > MAX_COMPRESSION_RATIO * max(info.compress_size, 1):
                raise PackageError(f"Archive entry has an implausible compression ratio: {name!r}.")
            declared_total += info.file_size
            if declared_total > MAX_ARCHIVE_UNCOMPRESSED_BYTES:
                raise PackageError("The package archive expands beyond the allowed size.")

        scan.members.append(_Member(info=info, relpath=rel_full, is_dir=is_dir))

    roots = {member.relpath.split("/", 1)[0] for member in scan.members}
    if len(roots) != 1:
        raise PackageError("The package must contain exactly one top-level directory.")
    root = next(iter(roots))
    if not any(m.relpath == root and m.is_dir for m in scan.members) and any(
        m.relpath == root and not m.is_dir for m in scan.members
    ):
        raise PackageError("The package must contain a top-level directory, not a top-level file.")

    scan.root = root
    stripped: List[_Member] = []
    for member in scan.members:
        if member.relpath == root:
            continue
        rel = member.relpath[len(root) + 1 :]
        stripped.append(_Member(info=member.info, relpath=rel, is_dir=member.is_dir))
    scan.members = stripped
    return scan


def _read_small_member(archive: zipfile.ZipFile, scan: _Scan, relpath: str) -> bytes:
    for member in scan.members:
        if member.relpath == relpath and not member.is_dir:
            if member.info.file_size > MAX_JSON_MEMBER_BYTES:
                raise PackageError(f"{relpath} is unexpectedly large.")
            with archive.open(member.info) as handle:
                data = handle.read(MAX_JSON_MEMBER_BYTES + 1)
            if len(data) > MAX_JSON_MEMBER_BYTES:
                raise PackageError(f"{relpath} is unexpectedly large.")
            return data
    raise PackageError(f"The package does not contain the required file {relpath}.")


def _parse_json_object(data: bytes, name: str) -> Dict[str, Any]:
    try:
        parsed = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise PackageError(f"{name} is not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise PackageError(f"{name} must contain a JSON object.")
    return parsed


def _parse_mode(value: Any, name: str) -> int:
    try:
        mode = int(value, 8) if isinstance(value, str) else int(value)
    except (TypeError, ValueError) as exc:
        raise PackageError(f"Invalid file mode for {name}: {value!r}.") from exc
    if isinstance(value, bool) or mode < 0 or mode & 0o7000:
        raise PackageError(f"Unsafe file mode for {name}: {value!r}.")
    return mode


def _parse_provenance(data: Dict[str, Any], root: str) -> Tuple[str, Dict[str, FileEntry], Optional[str], Optional[str]]:
    package = data.get("package")
    if not isinstance(package, str) or package != root:
        raise PackageError(f"{PROVENANCE_FILE} package name does not match the package directory '{root}'.")

    symlinks = data.get("symlinks", {})
    if symlinks:
        raise PackageError("The package declares symbolic links, which are not allowed.")

    raw_files = data.get("files")
    if not isinstance(raw_files, dict) or not raw_files:
        raise PackageError(f"{PROVENANCE_FILE} has no file integrity list.")

    files: Dict[str, FileEntry] = {}
    for name, entry in raw_files.items():
        rel = _validate_relative_name(name, allow_trailing_slash=False)
        if not isinstance(entry, dict):
            raise PackageError(f"Invalid integrity entry for {name}.")
        sha = entry.get("sha256")
        size = entry.get("size")
        if not isinstance(sha, str) or not SHA256_RE.match(sha):
            raise PackageError(f"Invalid SHA-256 for {name}.")
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise PackageError(f"Invalid size for {name}.")
        files[rel] = FileEntry(sha256=sha, size=size, mode=_parse_mode(entry.get("mode", "0o644"), name))

    source = data.get("source")
    commit = source.get("commit") if isinstance(source, dict) else None
    created = data.get("createdUtc")
    return (
        package,
        files,
        commit if isinstance(commit, str) else None,
        created if isinstance(created, str) else None,
    )


def _check_member_coverage(scan: _Scan, files: Dict[str, FileEntry]) -> None:
    # The vendor checksum list is informational and is never extracted or used; the manifest is authoritative.
    ignored = {PROVENANCE_FILE, "SHA256SUMS.txt"} - set(files)
    archive_files = {m.relpath for m in scan.members if not m.is_dir and m.relpath not in ignored}
    declared = set(files)
    if PROVENANCE_FILE in declared:
        raise PackageError(f"{PROVENANCE_FILE} must not list itself.")
    undeclared = sorted(archive_files - declared)
    if undeclared:
        raise PackageError(f"The package contains files that are not in the integrity list: {undeclared[:5]}.")
    missing = sorted(declared - archive_files)
    if missing:
        raise PackageError(f"The integrity list names files that are not in the package: {missing[:5]}.")


def _check_required_files(files: Dict[str, FileEntry]) -> None:
    missing = [name for name in REQUIRED_PACKAGE_FILES if name != PROVENANCE_FILE and name not in files]
    if missing:
        raise PackageError(f"Incompatible package layout; required file(s) missing: {', '.join(missing)}.")


def _verify_and_extract(
    archive: zipfile.ZipFile, scan: _Scan, files: Dict[str, FileEntry], dest: Optional[Path]
) -> Dict[str, bytes]:
    """Stream each file, check it against the manifest and optionally write it out.

    Returns the content of the small metadata files that the caller needs to read.
    """
    keep_small = {OS_VERSION_FILE, CAPABILITIES_FILE}
    kept: Dict[str, bytes] = {}
    dest_resolved = dest.resolve() if dest is not None else None

    for member in scan.members:
        if member.is_dir:
            if dest is not None:
                _safe_target(dest_resolved, member.relpath).mkdir(mode=0o755, parents=True, exist_ok=True)
            continue
        if member.relpath == PROVENANCE_FILE or member.relpath not in files:
            continue
        entry = files[member.relpath]
        digest = hashlib.sha256()
        written = 0
        buffer: Optional[bytearray] = bytearray() if member.relpath in keep_small else None
        target_handle = None
        target_path = None
        try:
            if dest is not None:
                target_path = _safe_target(dest_resolved, member.relpath)
                target_path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
                flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
                target_handle = os.fdopen(os.open(target_path, flags, 0o600), "wb")
            with archive.open(member.info) as source:
                while True:
                    chunk = source.read(HASH_CHUNK_BYTES)
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > entry.size or written > MAX_MEMBER_BYTES:
                        raise PackageError(f"Archive entry {member.relpath} is larger than declared.")
                    digest.update(chunk)
                    if buffer is not None:
                        buffer.extend(chunk)
                    if target_handle is not None:
                        target_handle.write(chunk)
        except zipfile.BadZipFile as exc:
            raise PackageError(f"Archive entry {member.relpath} is corrupt: {exc}") from exc
        finally:
            if target_handle is not None:
                target_handle.close()

        if written != entry.size or not hmac.compare_digest(digest.hexdigest(), entry.sha256):
            raise PackageError(f"Integrity check failed for {member.relpath}.")
        if target_path is not None:
            # Group and other write bits are never granted to staged files.
            os.chmod(target_path, entry.mode & ~0o022)
        if buffer is not None:
            kept[member.relpath] = bytes(buffer)
    return kept


def _safe_target(dest_resolved: Path, relpath: str) -> Path:
    target = dest_resolved.joinpath(*PurePosixPath(relpath).parts)
    if not target.resolve().is_relative_to(dest_resolved):
        raise PackageError(f"Archive path escapes the staging directory: {relpath!r}.")
    return target
