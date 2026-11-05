# Copyright (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""
Orchestration of ``esq install proe``.

Sequence (nothing is executed before the checksum and the package contents are verified):

1. verify the trusted SHA-256 and validate the archive read-only,
2. check the host operating system and architecture,
3. detect an existing installation (idempotent re-run, resume, or explicit conflict),
4. show a preview of exactly what will happen and obtain consent,
5. when the host is not ready, prepare it without rebooting: back up the GRUB configuration, install the
   ``ubuntu-realtime`` kernel (the package makes it the default boot entry), add the boot parameters PROe
   needs, replacing conflicting values, and check the generated GRUB configuration,
6. install the host tools and ``libyang`` that the PROe README lists and the package does not include,
7. stage a verified private copy, re-verify it, then run the vendor ``install.sh`` with privileges,
8. verify the installed files, executable and service, and only then write the capability record,
9. reboot, only when every check passed, so that the real-time kernel and boot parameters take effect.

One command sets up PROe and one confirmation covers all of it, including the reboot; running the
command again only reports whether the host is ready. ESQ never uninstalls, overwrites or
modifies an existing installation on its own, and never reboots without consent. A reboot that a package
declares needs a separate consent unless the host preparation already asked for one. All other system effects
are performed by the vendor installer that ships inside the verified package.
"""

import logging
import os
import platform
import shutil
import subprocess
import sys
import tempfile
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional, Protocol, Sequence, Set, Tuple

from esq.utils.proe import host_changes, host_prep, ptyrun
from esq.utils.proe import state as proe_state
from esq.utils.proe.state import is_absolute_path
from esq.utils.proe.constants import (
    DEFAULT_RESULTS_DIR,
    INSTALLED_TREES,
    INSTALLER_FILE,
    RT_KERNEL_PACKAGE,
    RT_KERNEL_SYSFS,
    VENDOR_APP_CONFIG,
    VENDOR_CLI_PATH,
    VENDOR_CONFIG_DIR,
    VENDOR_EXISTING_PATHS,
    VENDOR_LOG_DIR,
    VENDOR_RUNTIME_DIR,
    VENDOR_SERVICE_NAME,
    VENDOR_SERVICE_PATH,
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
from esq.utils.proe.progress import Phase, Progress, write_line

logger = logging.getLogger(__name__)

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_ACTION_REQUIRED = 2
LOG_TAIL_LINES = 15
MAX_PACKAGE_NOTES = 12

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
    """Runner backed by :mod:`subprocess` with argument lists only (no shell).

    ``echo=False`` keeps the command output off the screen; it is still written to the log file.
    """

    def __init__(self, echo: bool = True) -> None:
        self.echo = echo
        self.on_line: Optional[Callable[[str], None]] = None

    def stream(self, argv: Sequence[str], log_path: Path, cwd: Path) -> int:
        fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8", errors="replace") as log:
            if self.on_line is not None and not self.echo and ptyrun.available():
                # A pseudo-terminal makes the command write line by line, so the live display stays current.
                splitter = ptyrun.LineSplitter(log.write, self.on_line)
                code = ptyrun.run_in_pty(argv, cwd, splitter.feed)
                splitter.flush()
                return code
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
                    if self.echo:
                        sys.stdout.write(line)
                    elif self.on_line is not None:
                        self.on_line(line)
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
    assume_yes: bool = False
    allow_reboot: bool = False


def _default_prompt(question: str) -> str:
    return input(question)


def _default_boot_id() -> str:
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
    except OSError:
        return ""


def _default_rt_kernel_active() -> bool:
    """Whether the running kernel is a PREEMPT_RT kernel."""
    try:
        if Path(RT_KERNEL_SYSFS).read_text(encoding="ascii").strip() == "1":
            return True
    except OSError:
        pass
    try:
        return "PREEMPT_RT" in Path("/proc/version").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False


def _default_read_cmdline() -> str:
    try:
        return Path("/proc/cmdline").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _default_kernel_release() -> str:
    return platform.release()


def _default_link_target(path: str) -> Optional[str]:
    return os.readlink(path) if os.path.islink(path) else None


def _default_tool_available(tool: str) -> bool:
    return shutil.which(tool) is not None


@dataclass
class InstallEnvironment:
    """Everything the installer reads from or writes to the machine. All members are replaceable."""

    runner: Runner = field(default_factory=SubprocessRunner)
    host: HostInfo = field(default_factory=detect_host)
    is_root: bool = field(default_factory=lambda: hasattr(os, "geteuid") and os.geteuid() == 0)
    interactive: bool = field(default_factory=lambda: sys.stdin.isatty() and sys.stdout.isatty())
    prompt: Callable[[str], str] = _default_prompt
    out: Callable[[str], None] = write_line
    boot_id: Callable[[], str] = _default_boot_id
    rt_kernel_active: Callable[[], bool] = _default_rt_kernel_active
    kernel_release: Callable[[], str] = _default_kernel_release
    libyang_root: str = host_changes.LIBYANG_ROOT
    exists: Callable[[str], bool] = os.path.lexists
    link_target: Callable[[str], Optional[str]] = _default_link_target
    read_cmdline: Callable[[], str] = _default_read_cmdline
    tool_available: Callable[[str], bool] = _default_tool_available
    grub_default: Path = Path(host_prep.GRUB_DEFAULT_FILE)
    grub_dropin_dir: Path = Path(host_prep.GRUB_DROPIN_DIR)
    grub_backup_dir: Path = Path(host_prep.GRUB_BACKUP_DIR)
    grub_cfg: Path = Path(host_prep.GRUB_CFG_FILE)
    state_dir: Optional[Path] = None
    spinner: bool = False
    existing_paths: Callable[[], List[str]] = field(
        default_factory=lambda: lambda: [p for p in VENDOR_EXISTING_PATHS if os.path.lexists(p)]
    )
    installed_root: Path = Path(VENDOR_RUNTIME_DIR)
    cli_path: str = VENDOR_CLI_PATH
    app_config: Path = Path(VENDOR_APP_CONFIG)
    config_dir: Path = Path(VENDOR_CONFIG_DIR)
    log_dir: Path = Path(VENDOR_LOG_DIR)
    service_path: Path = Path(VENDOR_SERVICE_PATH)
    progress: Optional[Progress] = None

    def directory(self) -> Path:
        return self.state_dir if self.state_dir is not None else proe_state.state_dir()

    def phase(self, title: str) -> "AbstractContextManager[Phase]":
        """A numbered progress phase, or a plain pass-through that prints its notes when there is no progress."""
        if self.progress is not None:
            return self.progress.phase(title)
        return nullcontext(Phase(sink=self.out))


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

    if previous is not None and previous.state == proe_state.STATE_UNINSTALL_REBOOT:
        raise InstallError(
            "A previous 'esq uninstall proe' is waiting for a reboot. Reboot, then run 'esq uninstall proe' "
            "again to finish it, before you install PROe again."
        )
    if existing:
        return _handle_existing(env, manifest, previous, existing, directory)

    prep = _plan_prep(env)
    deps = host_prep.plan_dependencies(env.tool_available)
    _print_preview(env, manifest, options, prep, deps)
    if prep.needed:
        question = (
            "PROe needs a real-time kernel and boot parameters to meet its performance targets. ESQ will prepare "
            "the host (real-time kernel, GRUB boot parameters), install PROe, run sanity checks and then REBOOT "
            "this machine. Continue? [y/N] "
        )
    else:
        question = "Proceed with the installation? [y/N] "
    if not _consent(env, options.assume_yes, question):
        env.out("Installation cancelled. Nothing was changed.")
        return EXIT_ACTION_REQUIRED
    _prime_sudo(env)
    env.progress = Progress(env.out, _count_phases(prep, deps), spinner=env.spinner)
    _watch_output(env)
    if prep.needed:
        _prepare_host(env, directory, prep)
    return _install(options, env, manifest, directory, host_reboot=prep.needed)


def _count_phases(prep: "_Prep", deps: host_prep.DependencyPlan) -> int:
    """The number of numbered phases that the installation shows."""
    host = 0
    if prep.needed:
        host = 3 + int(prep.rt_missing)
    tools = int(bool(deps.tools)) + int(deps.build_libyang)
    return host + 1 + tools + 2


def _watch_output(env: InstallEnvironment) -> None:
    """Show the last lines that a running command writes, in the live block of the current phase."""
    if env.progress is not None and isinstance(env.runner, SubprocessRunner):
        env.runner.on_line = env.progress.feed


def _prime_sudo(env: InstallEnvironment) -> None:
    """Ask for the sudo password before the first phase, so that the prompt is not drawn over the progress."""
    if env.is_root or not env.interactive:
        return
    env.out("Administrator access is needed. sudo may ask for your password.")
    code, output = env.runner.capture(["sudo", "-v"], 120)
    if code != 0:
        raise InstallError("sudo could not authenticate: " + output.strip()[:200])


@dataclass(frozen=True)
class _Prep:
    """What must change on the host before PROe can be installed."""

    rt_missing: bool
    boot: host_prep.BootParamPlan
    cmdline_missing: Tuple[str, ...]
    rt_too_old: bool = False
    rt_release: str = ""
    rt_candidate: Optional[str] = None

    @property
    def needed(self) -> bool:
        return self.rt_missing or bool(self.cmdline_missing)


def _plan_prep(env: InstallEnvironment) -> _Prep:
    boot = host_prep.plan_boot_params(host_prep.read_configured_tokens(env.grub_default, env.grub_dropin_dir))
    release = env.kernel_release()
    is_rt = env.rt_kernel_active()
    too_old = is_rt and not host_prep.kernel_at_least(release)
    rt_missing = not is_rt or too_old
    candidate = _rt_kernel_candidate(env) if rt_missing else None
    if candidate is not None and not host_prep.kernel_at_least(candidate):
        raise InstallError(
            f"PROe needs a real-time kernel {host_prep.format_version(host_prep.MIN_RT_KERNEL)} or newer, but apt "
            f"only offers {candidate}. Nothing was changed. Enable a repository that provides a newer real-time "
            "kernel, then run this command again."
        )
    return _Prep(
        rt_missing=rt_missing,
        boot=boot,
        cmdline_missing=tuple(host_prep.missing_from_cmdline(env.read_cmdline())),
        rt_too_old=too_old,
        rt_release=release,
        rt_candidate=candidate,
    )


def _rt_kernel_candidate(env: InstallEnvironment) -> Optional[str]:
    """Newest real-time kernel version that apt can install, or None when it cannot be determined."""
    code, output = env.runner.capture(["apt-cache", "policy", host_prep.RT_KERNEL_PROBE_PACKAGE], 30)
    return host_prep.parse_apt_candidate(output) if code == 0 else None


def _host_problems(env: InstallEnvironment) -> List[str]:
    problems: List[str] = []
    if not env.rt_kernel_active():
        problems.append(
            "This machine is not running a real-time kernel. If it has not been rebooted since the installation, "
            "reboot it. If it was, select the '-realtime' kernel in the boot menu (GRUB: Advanced options for "
            "Ubuntu), then run 'esq install proe' again to confirm."
        )
    elif not host_prep.kernel_at_least(env.kernel_release()):
        problems.append(
            f"This machine is running real-time kernel {env.kernel_release()}, older than the required "
            f"{host_prep.format_version(host_prep.MIN_RT_KERNEL)}. Select a newer '-realtime' kernel in the boot "
            "menu (GRUB: Advanced options for Ubuntu), then run 'esq install proe' again to confirm."
        )
    missing = host_prep.missing_from_cmdline(env.read_cmdline())
    if missing:
        problems.append(
            "The running kernel command line is missing: "
            + " ".join(missing)
            + f". Check {env.grub_dropin_dir / host_prep.DROPIN_NAME}, run update-grub and reboot, then run "
            "'esq install proe' again to confirm."
        )
    return problems


def _handle_existing(
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
        return _finalize(env, manifest, directory)
    if same and previous is not None and previous.state == proe_state.STATE_INSTALLED:
        env.out("This exact package is already installed. Re-verifying the installation (idempotent re-run)...")
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


def _print_preview(
    env: InstallEnvironment,
    manifest: PackageManifest,
    options: InstallOptions,
    prep: Optional[_Prep],
    deps: host_prep.DependencyPlan,
) -> None:
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

    minimum = host_prep.format_version(host_prep.MIN_RT_KERNEL)
    if prep is None or not prep.rt_missing:
        lines.append(f"Real-time kernel : running (minimum {minimum})")
    else:
        status = f"NOT running (minimum {minimum})"
        if prep.rt_too_old:
            status = f"TOO OLD ({prep.rt_release}, minimum {minimum})"
        offer = f" apt offers {prep.rt_candidate}." if prep.rt_candidate else ""
        lines.append(
            f"Real-time kernel : {status}.{offer} ESQ will run 'apt-get install -y {RT_KERNEL_PACKAGE}' (about 10 "
            "packages); the package makes it the default boot entry."
        )
    if prep is None or not prep.cmdline_missing:
        lines.append("Boot parameters  : active")
    else:
        lines.append("Boot parameters  : missing from the running kernel: " + " ".join(prep.cmdline_missing))
        if prep.boot.add:
            dropin = env.grub_dropin_dir / host_prep.DROPIN_NAME
            lines.append(
                f"                   ESQ will write {dropin} (adds: {' '.join(prep.boot.add)}) and run update-grub. "
                "To undo: delete that file and run update-grub."
            )
            for conflict in prep.boot.conflicts:
                lines.append(f"                   replaces: {conflict}")
            lines.append(
                "                   The original GRUB files and kernel command line are backed up first, in "
                f"{env.grub_backup_dir}."
            )
    if deps.tools:
        text = "apt packages " + " ".join(deps.packages)
        if deps.build_libyang:
            text += f"; libyang {host_prep.LIBYANG_TAG} is built from source and installed to /usr/local"
        lines.append("Host tools       : missing: " + ", ".join(deps.tools) + ". ESQ will install them (" + text + ").")
    else:
        lines.append("Host tools       : all present")
    host_reboot = prep is not None and prep.needed
    if host_reboot:
        lines.extend(
            [
                "",
                "IMPORTANT        : a real-time kernel and a reboot are REQUIRED",
                "                   PROe benchmarks need the real-time kernel and the boot parameters above to meet "
                "their performance targets.",
                "                   One confirmation covers everything. ESQ will, in this order:",
                "                     1. back up your GRUB configuration,",
                "                     2. install the real-time kernel (if missing), add the missing boot parameters,",
                "                     3. check the generated GRUB configuration,",
                "                     4. install the host tools and PROe,",
                "                     5. check the PROe installation,",
                "                     6. reboot this machine, only if every check passed.",
                "                   This one command sets up PROe; it is ready to use after the reboot.",
                "                   'esq uninstall proe' undoes all of it.",
            ]
        )
    if caps is not None and caps.requires_reboot:
        if host_reboot:
            lines.append("Reboot           : also required by the package; it is the same reboot as the one above")
        else:
            lines.append(
                "Reboot           : required; you will be asked for a separate consent, then re-run this command"
            )
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


def _install(
    options: InstallOptions, env: InstallEnvironment, manifest: PackageManifest, directory: Path, host_reboot: bool
) -> int:
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix="stage-", dir=directory))
    os.chmod(staging, 0o700)
    keep_staging = False
    try:
        with env.phase("Verifying a private copy of the package"):
            staged_zip, staged_sha = stage_verified_copy(Path(options.package).expanduser(), options.sha256, staging)
            extracted = staging / "extracted"
            staged_manifest = inspect_archive(staged_zip, staged_sha, extracted)
            package_root = extracted
            reverify_extracted(package_root, staged_manifest)

        log_path = directory / "install.log"
        _install_dependencies(env, directory, log_path, check_cpupower=not host_reboot)
        proe_state.write_install_state(
            proe_state.InstallState(
                proe_state.STATE_INSTALLING, manifest.zip_sha256, manifest.package_name, "vendor installer running"
            ),
            directory,
        )
        argv = _privileged(env, ["bash", str(package_root / INSTALLER_FILE)])
        # Re-verify immediately before privileged execution to narrow the window for tampering.
        reverify_extracted(package_root, staged_manifest)
        offset = _log_command(log_path, argv)
        with env.phase("Installing PROe") as phase:
            code = env.runner.stream(argv, log_path, package_root)
            if code != 0:
                phase.result = "FAILED"
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
                "retrying. Dependency or network failures are reported in the log." + _failure_detail(log_path, offset)
            )

        caps = manifest.capabilities
        if caps is not None and caps.requires_reboot:
            return _reboot_flow(options, env, manifest, directory, consented=host_reboot)
        code = _finalize(env, manifest, directory, reboot_pending=host_reboot)
        if host_reboot and code == EXIT_OK:
            return _reboot_machine(env)
        return code
    finally:
        if not keep_staging:
            shutil.rmtree(staging, ignore_errors=True)


def _apt(args: List[str]) -> List[str]:
    return ["env", "DEBIAN_FRONTEND=noninteractive", "apt-get", *args]


def installed_packages(env: InstallEnvironment) -> Optional[Set[str]]:
    """Names of the installed dpkg packages, or None when dpkg cannot be queried."""
    code, output = env.runner.capture(["dpkg-query", "-W", "-f=${db:Status-Abbrev}\\t${Package}\\n"], 60)
    if code != 0:
        return None
    names: Set[str] = set()
    for line in output.splitlines():
        status, _, name = line.partition("\t")
        if name.strip() and status.strip() == "ii":
            names.add(name.strip())
    return names


def _record_added(
    env: InstallEnvironment, directory: Path, before: Optional[Set[str]], *, kernel: bool
) -> List[str]:
    """Remember which packages an apt step added, so that ``esq uninstall proe`` removes exactly those."""
    after = installed_packages(env)
    if before is None or after is None:
        env.out("WARNING: the installed packages could not be listed, so they are not recorded for uninstall.")
        return []
    added = sorted(after - before)
    if kernel:
        host_changes.update_host_changes(directory, kernel_packages=added)
    else:
        host_changes.update_host_changes(directory, tool_packages=added)
    return added


def _note_added(phase: Phase, added: Sequence[str]) -> None:
    """Report the packages that a step added, in the style of ``uv``."""
    for name in added[:MAX_PACKAGE_NOTES]:
        phase.note(f"+ {name}")
    if len(added) > MAX_PACKAGE_NOTES:
        phase.note(f"... and {len(added) - MAX_PACKAGE_NOTES} more (see the log)")


def _log_command(log_path: Path, argv: Sequence[str]) -> int:
    """Write the command line to the log, and return the log size so that its own output can be found later."""
    fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8", errors="replace") as log:
        log.write(f"\n$ {' '.join(argv)}\n")
        log.flush()
        return log.tell()


def _log_tail(log_path: Path, offset: int, lines: int = LOG_TAIL_LINES) -> str:
    """The last lines that a command wrote to the log after ``offset``."""
    try:
        with open(log_path, "rb") as handle:
            handle.seek(offset)
            text = handle.read().decode("utf-8", errors="replace")
    except OSError:
        return ""
    return "\n".join("    " + line for line in text.splitlines()[-lines:] if line.strip())


def _failure_detail(log_path: Path, offset: int) -> str:
    tail = _log_tail(log_path, offset)
    return f"\nLast lines of the output:\n{tail}" if tail else ""


def _step(env: InstallEnvironment, argv: List[str], log_path: Path, cwd: Path, *, privileged: bool) -> None:
    offset = _log_command(log_path, argv)
    code = env.runner.stream(_privileged(env, argv) if privileged else argv, log_path, cwd)
    if code != 0:
        raise InstallError(
            f"'{' '.join(argv)}' failed with exit code {code}. See {log_path}. Fix the problem (network, package "
            "index) and run this command again." + _failure_detail(log_path, offset)
        )


def _cpupower_problem(env: InstallEnvironment) -> Optional[str]:
    code, output = env.runner.capture(["cpupower", "--version"], 30)
    if code == 0:
        return None
    return (
        "'cpupower --version' does not work with the running kernel: "
        + output.strip()[:200]
        + ". Install the linux-tools package that matches the running kernel."
    )


def _install_dependencies(env: InstallEnvironment, directory: Path, log_path: Path, check_cpupower: bool) -> None:
    """Install the host tools and libyang that the PROe README lists and the package does not include.

    ``cpupower`` is checked later, after the reboot, when the host is rebooted into another kernel.
    """
    deps = host_prep.plan_dependencies(env.tool_available)
    if deps.tools:
        with env.phase("Installing the host tools: " + ", ".join(deps.tools)) as phase:
            _step(env, _apt(["update"]), log_path, directory, privileged=True)
            before = installed_packages(env)
            _step(env, _apt(["install", "-y", *deps.packages]), log_path, directory, privileged=True)
            _note_added(phase, _record_added(env, directory, before, kernel=False))
        if deps.build_libyang:
            with env.phase(f"Building libyang {host_prep.LIBYANG_TAG} from source"):
                _build_libyang(env, directory, log_path)
        still_missing = [tool for tool in deps.tools if not env.tool_available(tool)]
        if still_missing:
            raise InstallError("These host tools are still missing after installation: " + ", ".join(still_missing))
    problem = _cpupower_problem(env) if check_cpupower else None
    if problem:
        raise InstallError(problem + " Then run this command again.")


def _save_libyang_manifest(env: InstallEnvironment, directory: Path, build: Path) -> None:
    """Keep the list of files that the libyang build installed, so that ``esq uninstall proe`` can remove them."""
    source = build / "libyang" / "build" / "install_manifest.txt"
    if not source.is_file():
        env.out("WARNING: the libyang install manifest was not found, so libyang cannot be removed by uninstall.")
        return
    shutil.copyfile(source, directory / host_changes.LIBYANG_MANIFEST_FILE)
    host_changes.update_host_changes(directory, libyang_manifest=host_changes.LIBYANG_MANIFEST_FILE)


def _build_libyang(env: InstallEnvironment, directory: Path, log_path: Path) -> None:
    build = Path(tempfile.mkdtemp(prefix="libyang-", dir=directory))
    try:
        clone = ["git", "clone", "--depth", "1", "--branch", host_prep.LIBYANG_TAG, host_prep.LIBYANG_REPO, "libyang"]
        _step(env, clone, log_path, build, privileged=False)
        _step(env, ["cmake", "-S", "libyang", "-B", "libyang/build"], log_path, build, privileged=False)
        jobs = str(os.cpu_count() or 1)
        _step(env, ["cmake", "--build", "libyang/build", "-j", jobs], log_path, build, privileged=False)
        _step(env, ["cmake", "--install", "libyang/build"], log_path, build, privileged=True)
        _save_libyang_manifest(env, directory, build)
        _step(env, ["ldconfig", "/usr/local/lib"], log_path, build, privileged=True)
    finally:
        shutil.rmtree(build, ignore_errors=True)


def _backup_grub(env: InstallEnvironment, directory: Path, log_path: Path, phase: Phase) -> None:
    """Save the user's GRUB files and the running kernel command line once, before anything is changed."""
    backup = env.grub_backup_dir
    if backup.exists():
        phase.note(f"The original GRUB configuration is already backed up in {backup}.")
        return
    dropins: List[Path] = []
    if env.grub_dropin_dir.is_dir():
        dropins = sorted(p for p in env.grub_dropin_dir.glob("*.cfg") if p.name != host_prep.DROPIN_NAME)
    copies = []
    if env.grub_default.exists():
        copies.append((env.grub_default, backup / "default-grub"))
    copies.extend((path, backup / "grub.d" / path.name) for path in dropins)
    _step(
        env, ["install", "-d", "-m", "0700", str(backup), str(backup / "grub.d")], log_path, directory, privileged=True
    )
    for source, target in copies:
        _step(env, ["cp", "-a", str(source), str(target)], log_path, directory, privileged=True)
    _step(env, ["cp", "/proc/cmdline", str(backup / "cmdline-before")], log_path, directory, privileged=True)
    phase.note(f"Backed up the original GRUB configuration to {backup}.")


def _prepare_host(env: InstallEnvironment, directory: Path, prep: _Prep) -> None:
    """Install the real-time kernel and set the GRUB boot parameters. The reboot comes after PROe is installed."""
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    log_path = directory / "install.log"
    with env.phase("Backing up the GRUB configuration") as phase:
        _backup_grub(env, directory, log_path, phase)
        host_changes.update_host_changes(directory, grub_backup=str(env.grub_backup_dir))
    if prep.rt_missing:
        with env.phase(f"Installing the real-time kernel ({RT_KERNEL_PACKAGE})") as phase:
            before = installed_packages(env)
            _step(env, _apt(["install", "-y", RT_KERNEL_PACKAGE]), log_path, directory, privileged=True)
            _note_added(phase, _record_added(env, directory, before, kernel=True))

    dropin = env.grub_dropin_dir / host_prep.DROPIN_NAME
    with env.phase("Setting the boot parameters") as phase:
        if prep.boot.add:
            staged = directory / "grub-dropin.tmp"
            fd = os.open(staged, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(host_prep.render_dropin(prep.boot.add, prep.boot.replace_keys))
            try:
                _step(
                    env, ["install", "-D", "-m", "0644", str(staged), str(dropin)], log_path, directory, privileged=True
                )
            finally:
                staged.unlink(missing_ok=True)
            phase.note(f"Wrote {dropin}.")
            host_changes.update_host_changes(directory, grub_dropin=str(dropin))
        _step(env, ["update-grub"], log_path, directory, privileged=True)
        phase.note(
            f"To revert: delete {dropin} and run update-grub. Your original GRUB files are saved in "
            f"{env.grub_backup_dir}."
        )
    with env.phase("Checking the GRUB configuration") as phase:
        _check_host_prepared(env, prep, dropin, phase)


def _check_host_prepared(env: InstallEnvironment, prep: _Prep, dropin: Path, phase: Phase) -> None:
    """Sanity check after update-grub, before anything else is installed or rebooted."""
    problems: List[str] = []
    if prep.boot.add and not env.exists(str(dropin)):
        problems.append(f"{dropin} was not written")
    try:
        text = env.grub_cfg.read_text(encoding="utf-8", errors="replace")
    except OSError:
        phase.note(f"{env.grub_cfg} cannot be read, so the generated boot entries were not checked.")
        phase.result = "skipped"
    else:
        found = host_prep.grub_cfg_problems(text, prep.cmdline_missing, prep.rt_missing)
        problems.extend(f"{env.grub_cfg}: {problem}" for problem in found)
    if problems:
        raise InstallError(
            "The sanity check after the host preparation failed:\n  - "
            + "\n  - ".join(problems)
            + "\nNothing was rebooted and PROe was not installed. To revert, run 'esq uninstall proe', or delete "
            f"{dropin} and run update-grub. Your original GRUB files are saved in {env.grub_backup_dir}."
        )
    phase.note("The real-time kernel entry and the boot parameters are in place.")


def _reboot_flow(
    options: InstallOptions,
    env: InstallEnvironment,
    manifest: PackageManifest,
    directory: Path,
    state: str = proe_state.STATE_AWAITING_REBOOT,
    reason: str = "The installation completed its first phase and needs a reboot before it can be verified.",
    consented: bool = False,
) -> int:
    proe_state.write_install_state(
        proe_state.InstallState(state, manifest.zip_sha256, manifest.package_name, env.boot_id()), directory
    )
    env.out(reason)
    if (
        consented
        or options.allow_reboot
        or (env.interactive and env.prompt("Reboot this machine now? [y/N] ").strip().lower() in ("y", "yes"))
    ):
        env.out("Rebooting. After the machine is back, run the same 'esq install proe' command to continue.")
        env.runner.capture(_privileged(env, ["systemctl", "reboot"]), 60)
    else:
        env.out("Reboot later, then run the same 'esq install proe' command to continue.")
    return EXIT_ACTION_REQUIRED


def _reboot_machine(env: InstallEnvironment) -> int:
    """Reboot after every sanity check passed. The consent was given for the whole installation."""
    env.out("All checks passed. Rebooting to activate the real-time kernel and the boot parameters.")
    env.out("PROe is installed and is ready to use after the reboot.")
    env.out("Run 'esq install proe' again at any time to check the host.")
    code, output = env.runner.capture(_privileged(env, ["systemctl", "reboot"]), 60)
    if code != 0:
        env.out(f"The reboot command failed ({output.strip()[:200]}). Reboot this machine manually.")
        return EXIT_ACTION_REQUIRED
    return EXIT_OK


def _check_host(env: InstallEnvironment) -> int:
    """Report whether the running system meets the PROe host requirements for benchmark performance."""
    problems = _host_problems(env)
    if not problems:
        cpupower = _cpupower_problem(env)
        if cpupower:
            problems.append(cpupower)
    if not problems:
        env.out("Host readiness  : ready (real-time kernel, boot parameters and cpupower)")
        return EXIT_OK
    env.out("Host readiness  : NOT READY. PROe benchmarks need these to meet their performance targets:")
    for problem in problems:
        env.out("  - " + problem)
    return EXIT_ACTION_REQUIRED


def _privileged(env: InstallEnvironment, argv: List[str]) -> List[str]:
    if env.is_root:
        return argv
    return ["sudo"] + ([] if env.interactive else ["-n"]) + ["--"] + argv


def _verify_installation(
    env: InstallEnvironment, manifest: PackageManifest, directory: Path
) -> Tuple[str, Tuple[proe_state.RecordedProfile, ...]]:
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
    return version_text, profiles


def _finalize(
    env: InstallEnvironment, manifest: PackageManifest, directory: Path, reboot_pending: bool = False
) -> int:
    """Verify the installation, then report it and whether the host is ready for benchmarks."""
    with env.phase("Verifying the installation"):
        version_text, profiles = _verify_installation(env, manifest, directory)
    env.out("")
    env.out(f"Installed        : yes ({manifest.package_name}; reported version '{version_text}')")
    env.out("Service          : active")
    if profiles:
        env.out("Profiles         : " + ", ".join(p.esq_profile for p in profiles))
    else:
        env.out("Profiles         : none (the package declares no supported single-DUT profile)")
    host_code = EXIT_OK if reboot_pending else _check_host(env)
    if not profiles:
        env.out("Ready            : NO. Installed, but ESQ cannot run PROe benchmarks from this package.")
    elif reboot_pending:
        env.out("Ready           : after the reboot. Then run 'esq list' to see them and 'esq run' to execute one.")
    elif host_code != EXIT_OK:
        env.out("Ready            : NO. Fix the host problems above first.")
    else:
        env.out("Ready           : run 'esq list' to see them and 'esq run' to execute one.")
    env.out(f"Log              : {directory / 'install.log'}")
    return host_code


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
