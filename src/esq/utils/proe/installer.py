# Copyright (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""
Orchestration of ``esq install proe``.

Sequence (nothing is executed before the checksum and the package contents are verified):

1. verify the trusted SHA-256 and validate the archive read-only,
2. check the host operating system and architecture,
3. detect an existing installation (idempotent re-run, resume, or explicit conflict),
4. show a preview of exactly what will happen and obtain consent (``--dry-run`` stops here),
5. stage a verified private copy, re-verify it, then run the vendor ``install.sh`` with privileges,
6. verify the installed files, executable and service, and only then write the capability record.

ESQ never uninstalls, overwrites or modifies an existing installation on its own, and never reboots
without a separate consent. All system effects are performed by the vendor installer that ships
inside the verified package.
"""

import logging
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional, Protocol, Sequence, Tuple

from esq.utils.proe import state as proe_state
from esq.utils.proe.state import is_absolute_path
from esq.utils.proe.constants import (
    DEFAULT_RESULTS_DIR,
    INSTALLED_TREES,
    INSTALLER_FILE,
    VENDOR_APP_CONFIG,
    VENDOR_CLI_PATH,
    VENDOR_EXISTING_PATHS,
    VENDOR_RUNTIME_DIR,
    VENDOR_SERVICE_NAME,
    InstallError,
    ProeError,
)
from esq.utils.proe.package import (
    HostInfo,
    PackageManifest,
    check_host_compatibility,
    detect_host,
    inspect_archive,
    normalize_sha256,
    reverify_extracted,
    stage_verified_copy,
    verify_checksum,
    verify_installed_file,
)

logger = logging.getLogger(__name__)

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_ACTION_REQUIRED = 2

# Effects of the vendor installer that are always listed, in addition to any that the package declares.
GENERIC_SYSTEM_CHANGES: Tuple[str, ...] = (
    f"create {VENDOR_RUNTIME_DIR} (PROe runtime), /etc/proe (configuration) and /var/log/proe (logs and results)",
    f"create the {VENDOR_CLI_PATH} command and register and start the {VENDOR_SERVICE_NAME} system service",
    "install missing host tools and clone pinned third-party sources (requires network access)",
)


class Runner(Protocol):
    """Process execution used by the installer. Replaced in tests."""

    def stream(self, argv: Sequence[str], log_path: Path, cwd: Path) -> int:
        """Run a command, show its output live and copy it to ``log_path``. Returns the exit code."""

    def capture(self, argv: Sequence[str], timeout: int) -> Tuple[int, str]:
        """Run a command and return its exit code and combined output."""


class SubprocessRunner:
    """Runner backed by :mod:`subprocess` with argument lists only (no shell)."""

    def stream(self, argv: Sequence[str], log_path: Path, cwd: Path) -> int:
        fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8", errors="replace") as log:
            with subprocess.Popen(
                list(argv),
                cwd=cwd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                errors="replace",
            ) as proc:
                assert proc.stdout is not None
                for line in proc.stdout:
                    sys.stdout.write(line)
                    log.write(line)
                return proc.wait()

    def capture(self, argv: Sequence[str], timeout: int) -> Tuple[int, str]:
        try:
            completed = subprocess.run(  # noqa: S603 - argument list, fixed executable names
                list(argv),
                capture_output=True,
                text=True,
                errors="replace",
                timeout=timeout,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return 127, f"{argv[0]}: {exc}"
        return completed.returncode, (completed.stdout or "") + (completed.stderr or "")


@dataclass
class InstallOptions:
    package: Path
    sha256: str
    dry_run: bool = False
    assume_yes: bool = False
    allow_reboot: bool = False


def _default_prompt(question: str) -> str:
    return input(question)


def _default_boot_id() -> str:
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
    except OSError:
        return ""


@dataclass
class InstallEnvironment:
    """Everything the installer reads from or writes to the machine. All members are replaceable."""

    runner: Runner = field(default_factory=SubprocessRunner)
    host: HostInfo = field(default_factory=detect_host)
    is_root: bool = field(default_factory=lambda: hasattr(os, "geteuid") and os.geteuid() == 0)
    interactive: bool = field(default_factory=lambda: sys.stdin.isatty() and sys.stdout.isatty())
    prompt: Callable[[str], str] = _default_prompt
    out: Callable[[str], None] = print
    boot_id: Callable[[], str] = _default_boot_id
    state_dir: Optional[Path] = None
    existing_paths: Callable[[], List[str]] = field(
        default_factory=lambda: lambda: [p for p in VENDOR_EXISTING_PATHS if os.path.lexists(p)]
    )
    installed_root: Path = Path(VENDOR_RUNTIME_DIR)
    cli_path: str = VENDOR_CLI_PATH
    app_config: Path = Path(VENDOR_APP_CONFIG)

    def directory(self) -> Path:
        return self.state_dir if self.state_dir is not None else proe_state.state_dir()


def run_install(options: InstallOptions, env: InstallEnvironment) -> int:
    """Run the installation flow and return a process exit code (0 ok, 1 error, 2 action required)."""
    try:
        return _run(options, env)
    except ProeError as exc:
        env.out(f"ERROR: {exc}")
        return EXIT_ERROR


def _run(options: InstallOptions, env: InstallEnvironment) -> int:
    expected = normalize_sha256(options.sha256)
    zip_path = Path(options.package).expanduser()

    env.out("Verifying package checksum and contents (no files are installed or executed)...")
    zip_sha = verify_checksum(zip_path, expected)
    manifest = inspect_archive(zip_path, zip_sha)
    problems = check_host_compatibility(manifest, env.host)
    if problems:
        raise InstallError("This package cannot be installed on this machine:\n  - " + "\n  - ".join(problems))

    directory = env.directory()
    previous = proe_state.read_install_state(directory)
    existing = env.existing_paths()

    if existing:
        return _handle_existing(options, env, manifest, previous, existing, directory)

    _print_preview(env, manifest, options)
    if options.dry_run:
        env.out("Dry run complete. Nothing was changed.")
        return EXIT_OK
    if not _consent(env, options.assume_yes, "Proceed with the installation? [y/N] "):
        env.out("Installation cancelled. Nothing was changed.")
        return EXIT_ACTION_REQUIRED

    return _install(options, env, manifest, directory)


def _handle_existing(
    options: InstallOptions,
    env: InstallEnvironment,
    manifest: PackageManifest,
    previous: Optional[proe_state.InstallState],
    existing: List[str],
    directory: Path,
) -> int:
    same = previous is not None and previous.package_sha256 == manifest.zip_sha256
    if same and previous is not None and previous.state == proe_state.STATE_AWAITING_REBOOT:
        if env.boot_id() and env.boot_id() == previous.detail:
            env.out("The installation is waiting for a reboot of this machine. Reboot, then run this command again.")
            return EXIT_ACTION_REQUIRED
        env.out("Reboot detected. Completing the installation checks...")
        if options.dry_run:
            env.out("Dry run: no state was changed.")
            return EXIT_OK
        return _finalize(env, manifest, directory)
    if same and previous is not None and previous.state == proe_state.STATE_INSTALLED:
        env.out("This exact package is already installed. Re-verifying the installation (idempotent re-run)...")
        if options.dry_run:
            env.out("Dry run: no state was changed.")
            return EXIT_OK
        return _finalize(env, manifest, directory)

    detail = ""
    if same and previous is not None and previous.state in (proe_state.STATE_INSTALLING, proe_state.STATE_FAILED):
        detail = " A previous installation attempt of this package did not complete."
    raise InstallError(
        "PROe files already exist on this machine: "
        + ", ".join(existing)
        + "."
        + detail
        + " ESQ never overwrites or removes an existing installation. Remove it with the uninstall.sh script of "
        "the package that installed it (run as root), then run this command again."
    )


def _print_preview(env: InstallEnvironment, manifest: PackageManifest, options: InstallOptions) -> None:
    caps = manifest.capabilities
    sudo_note = "as root" if env.is_root else "through sudo (you may be asked for your password)"
    lines = [
        "",
        "PROe installation preview",
        "-------------------------",
        f"Package          : {manifest.package_name}",
        f"Package SHA-256  : {manifest.zip_sha256} (matches the checksum you supplied)",
        f"Source revision  : {manifest.source_commit or 'not recorded'}",
        f"Built            : {manifest.created_utc or 'not recorded'}",
        f"Content          : {len(manifest.files)} files, {manifest.total_bytes} bytes, all verified against the package manifest",
        f"Target host      : {env.host.pretty_name or env.host.os_id + ' ' + env.host.os_version} ({env.host.arch})",
        f"Command          : bash {INSTALLER_FILE} (vendor installer from the verified package), run {sudo_note}",
        "System changes   :",
    ]
    changes = list(GENERIC_SYSTEM_CHANGES)
    if caps is not None:
        changes.extend(caps.system_changes)
    lines.extend(f"  - {change}" for change in changes)

    if caps is not None and caps.requires_reboot:
        lines.append("Reboot           : required; you will be asked for a separate consent, then re-run this command")
    elif caps is None:
        lines.append("Reboot           : not declared by the package; the vendor installer is not expected to need one")
    else:
        lines.append("Reboot           : not required")

    if caps is None or not caps.profiles:
        lines.append(
            "ESQ profiles     : NONE. This package does not declare any single-DUT profile that this ESQ version "
            "supports, so no PROe profile will be listed or runnable after installation."
        )
    else:
        lines.append("ESQ profiles     : " + ", ".join(p.esq_profile for p in caps.profiles))
    if caps is not None and caps.unsupported:
        lines.append("Ignored entries  : " + "; ".join(caps.unsupported))
    lines.append("")
    env.out("\n".join(lines))


def _consent(env: InstallEnvironment, assume_yes: bool, question: str) -> bool:
    if assume_yes:
        return True
    if not env.interactive:
        raise InstallError("Consent is required. Re-run interactively, or pass --yes after reviewing the preview.")
    return env.prompt(question).strip().lower() in ("y", "yes")


def _install(options: InstallOptions, env: InstallEnvironment, manifest: PackageManifest, directory: Path) -> int:
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix="stage-", dir=directory))
    os.chmod(staging, 0o700)
    keep_staging = False
    try:
        staged_zip, staged_sha = stage_verified_copy(Path(options.package).expanduser(), options.sha256, staging)
        extracted = staging / "extracted"
        staged_manifest = inspect_archive(staged_zip, staged_sha, extracted)
        package_root = extracted
        reverify_extracted(package_root, staged_manifest)

        log_path = directory / "install.log"
        proe_state.write_install_state(
            proe_state.InstallState(
                proe_state.STATE_INSTALLING, manifest.zip_sha256, manifest.package_name, "vendor installer running"
            ),
            directory,
        )
        argv = _privileged(env, ["bash", str(package_root / INSTALLER_FILE)])
        env.out(f"Running the vendor installer (log: {log_path})...")
        # Re-verify immediately before privileged execution to narrow the window for tampering.
        reverify_extracted(package_root, staged_manifest)
        code = env.runner.stream(argv, log_path, package_root)
        if code != 0:
            keep_staging = True
            proe_state.write_install_state(
                proe_state.InstallState(
                    proe_state.STATE_FAILED, manifest.zip_sha256, manifest.package_name, f"installer exit {code}"
                ),
                directory,
            )
            raise InstallError(
                f"The vendor installer failed with exit code {code}. See {log_path}. The verified package copy was "
                f"kept at {package_root}; if files were partially installed, run its uninstall.sh as root before "
                "retrying. Dependency or network failures are reported in the log."
            )

        caps = manifest.capabilities
        if caps is not None and caps.requires_reboot:
            return _reboot_flow(options, env, manifest, directory)
        return _finalize(env, manifest, directory)
    finally:
        if not keep_staging:
            shutil.rmtree(staging, ignore_errors=True)


def _reboot_flow(options: InstallOptions, env: InstallEnvironment, manifest: PackageManifest, directory: Path) -> int:
    proe_state.write_install_state(
        proe_state.InstallState(
            proe_state.STATE_AWAITING_REBOOT, manifest.zip_sha256, manifest.package_name, env.boot_id()
        ),
        directory,
    )
    env.out("The installation completed its first phase and needs a reboot before it can be verified.")
    if options.allow_reboot or (
        env.interactive and env.prompt("Reboot this machine now? [y/N] ").strip().lower() in ("y", "yes")
    ):
        env.out("Rebooting. After the machine is back, run the same 'esq install proe' command to finish.")
        env.runner.capture(_privileged(env, ["systemctl", "reboot"]), 60)
    else:
        env.out("Reboot later, then run the same 'esq install proe' command to finish the verification.")
    return EXIT_ACTION_REQUIRED


def _privileged(env: InstallEnvironment, argv: List[str]) -> List[str]:
    if env.is_root:
        return argv
    return ["sudo"] + ([] if env.interactive else ["-n"]) + ["--"] + argv


def _finalize(env: InstallEnvironment, manifest: PackageManifest, directory: Path) -> int:
    """Verify the installed system against the package and publish the capability record."""
    problems: List[str] = []
    for rel, entry in manifest.files.items():
        if rel.split("/", 1)[0] in INSTALLED_TREES:
            problem = verify_installed_file(env.installed_root / rel, entry)
            if problem:
                problems.append(problem)
    if problems:
        raise InstallError("Installed files do not match the package:\n  - " + "\n  - ".join(problems[:10]))

    code, output = env.runner.capture([env.cli_path, "--version"], 30)
    if code != 0:
        raise InstallError(f"'{env.cli_path} --version' failed (exit {code}): {output.strip()[:300]}")
    version_text = output.strip().splitlines()[0] if output.strip() else ""

    code, output = env.runner.capture(["systemctl", "is-active", VENDOR_SERVICE_NAME], 30)
    if code != 0 or output.strip() != "active":
        raise InstallError(f"The {VENDOR_SERVICE_NAME} service is not active (systemctl reported: {output.strip()!r}).")

    results_dir = _read_results_dir(env.app_config)
    caps = manifest.capabilities
    profiles = tuple(
        proe_state.RecordedProfile(p.esq_profile, p.proe_profile, p.report_schema)
        for p in (caps.profiles if caps else ())
    )
    record = proe_state.CapabilityRecord(
        package_name=manifest.package_name,
        package_sha256=manifest.zip_sha256,
        source_commit=manifest.source_commit or "unknown",
        executable=env.cli_path,
        profiles=profiles,
        results_dir=results_dir,
    )
    proe_state.write_capability_record(record, directory)
    proe_state.write_install_state(
        proe_state.InstallState(proe_state.STATE_INSTALLED, manifest.zip_sha256, manifest.package_name, ""), directory
    )

    env.out("")
    env.out(f"Installed        : yes ({manifest.package_name}; reported version '{version_text}')")
    env.out("Service          : active")
    if profiles:
        env.out("Profiles         : " + ", ".join(p.esq_profile for p in profiles))
        env.out("Ready           : run 'esq list' to see them and 'esq run' to execute one.")
    else:
        env.out("Profiles         : none (the package declares no supported single-DUT profile)")
        env.out("Ready            : NO. Installed, but ESQ cannot run PROe benchmarks from this package.")
    return EXIT_OK


def _read_results_dir(config_path: Path) -> str:
    """Read ``Results.ResultsDirectory`` from the installed PROe configuration."""
    try:
        text = config_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise InstallError(f"The installed PROe configuration cannot be read ({config_path}): {exc}") from exc
    in_results = False
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip())
        stripped = raw.strip()
        if indent == 0:
            in_results = stripped == "Results:"
        elif in_results and stripped.startswith("ResultsDirectory:"):
            value = stripped.split(":", 1)[1].strip().strip("'\"")
            if not is_absolute_path(value):
                raise InstallError("The configured PROe results directory must be an absolute path.")
            return value
    # The vendor default applies when the key is absent.
    return DEFAULT_RESULTS_DIR
