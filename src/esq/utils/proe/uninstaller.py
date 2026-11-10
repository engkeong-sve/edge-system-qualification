# Copyright (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""
Orchestration of ``esq uninstall proe``.

Undoes what ``esq install proe`` did, using the record that the installer kept in the ESQ state directory:

1. refuse to touch a PROe installation that ESQ did not install,
2. show a preview of exactly what will be removed and obtain one consent (``--yes`` answers it, ``--keep-data``
   keeps ``/etc/proe`` and ``/var/log/proe``),
3. remove PROe: the service, the ``proe`` command, ``/opt/proe``, ``/etc/proe`` and ``/var/log/proe``,
4. purge the apt packages that the installer added (host tools), after a simulation shows that apt would remove
   nothing else; if it would, those packages are left installed and ESQ names them,
5. remove the files that the libyang source build installed, and the PROe GRUB drop-in, then run ``update-grub``,
6. purge the real-time kernel packages that the installer added. A running kernel cannot be removed, and the
   newest real-time kernel is the default boot entry again once the drop-in is gone (GRUB sorts by version).
   So while the real-time kernel runs, ESQ rewrites the drop-in to boot the newest normal kernel, runs
   ``update-grub`` and reboots. The state file records the boot id, so the same command can tell 'still waiting
   for the reboot' from 'rebooted, finish now'. After the reboot, running the command again finishes the
   removal without a second question and deletes the drop-in,
7. delete the ESQ state for PROe, except the uninstall log.

Every step checks the current state of the machine, so the command can be repeated after an interruption. ESQ
never removes packages or files that it did not add, and it keeps the GRUB backup in
``/var/backups/esq/proe-grub``. A PROe installation that ESQ did not install is never touched. Exit codes: 0
finished, 1 error, 2 action required (declined, reboot pending, or some steps could not be completed).
"""

import os
import shutil
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from esq.utils.proe import host_changes, host_prep
from esq.utils.proe import state as proe_state
from esq.utils.proe.constants import VENDOR_SERVICE_NAME, InstallError, ProeError
from esq.utils.proe.installer import (
    EXIT_ACTION_REQUIRED,
    EXIT_ERROR,
    EXIT_OK,
    MAX_PACKAGE_NOTES,
    InstallEnvironment,
    _apt,
    _consent,
    _prime_sudo,
    _privileged,
    _step,
    _watch_output,
    installed_packages,
)
from esq.utils.proe.progress import Phase, Progress

LOG_NAME = "uninstall.log"
_RM_CHUNK = 100


@dataclass
class UninstallOptions:
    assume_yes: bool = False
    keep_data: bool = False


@dataclass(frozen=True)
class _Plan:
    """What is still present and will be removed."""

    proe_paths: tuple[str, ...]
    link: str
    link_foreign: str
    service: bool
    tool_packages: tuple[str, ...]
    libyang_files: tuple[str, ...]
    dropin: str
    kernel_packages: tuple[str, ...]
    rt_running: bool
    packages_unknown: bool

    @property
    def has_work(self) -> bool:
        return bool(
            self.proe_paths
            or self.link
            or self.service
            or self.tool_packages
            or self.libyang_files
            or self.dropin
            or self.kernel_packages
        )


def run_uninstall(options: UninstallOptions, env: InstallEnvironment) -> int:
    """Run the uninstall flow and return a process exit code (0 ok, 1 error, 2 action required)."""
    try:
        return _run(options, env)
    except ProeError as exc:
        env.out(f"ERROR: {exc}")
        return EXIT_ERROR


def _run(options: UninstallOptions, env: InstallEnvironment) -> int:
    directory = env.directory()
    previous = proe_state.read_install_state(directory)
    changes = host_changes.read_host_changes(directory)

    if previous is None and changes is None:
        present = [p for p in _proe_paths(env, keep_data=False) if env.exists(p)]
        if present:
            raise InstallError(
                "PROe files exist on this machine ("
                + ", ".join(present)
                + "), but they were not installed by 'esq install proe', so ESQ does not remove them. Remove them "
                "with the uninstall.sh script of the package that installed them."
            )
        env.out("PROe was not installed by ESQ on this machine. Nothing to uninstall.")
        return EXIT_OK

    resuming = previous is not None and previous.state == proe_state.STATE_UNINSTALL_REBOOT
    if resuming and previous is not None and env.boot_id() and env.boot_id() == previous.detail:
        env.out("The uninstall is waiting for a reboot of this machine. Reboot, then run this command again.")
        return EXIT_ACTION_REQUIRED

    recorded = changes or host_changes.HostChanges()
    plan = _plan(env, directory, recorded, options)
    if resuming and plan.rt_running:
        env.out(
            "This machine was rebooted but is still running the real-time kernel that ESQ installed. Select a "
            "different kernel in the boot menu (GRUB: Advanced options for Ubuntu), then run this command again."
        )
        return EXIT_ACTION_REQUIRED

    _print_preview(env, plan, options, recorded)
    if resuming:
        env.out("Continuing the uninstall after the reboot...")
    elif plan.has_work:
        question = "This will remove PROe and undo the host changes listed above"
        if plan.rt_running:
            question += ", and then REBOOT this machine"
        if not _consent(env, options.assume_yes, question + ". Continue? [y/N] "):
            env.out("Uninstall cancelled. Nothing was changed.")
            return EXIT_ACTION_REQUIRED
    return _execute(env, directory, plan, previous, recorded)


def _proe_paths(env: InstallEnvironment, *, keep_data: bool) -> list[str]:
    paths = [str(env.installed_root)]
    if not keep_data:
        paths += [str(env.config_dir), str(env.log_dir)]
    return paths


def _plan(
    env: InstallEnvironment, directory: Path, recorded: host_changes.HostChanges, options: UninstallOptions
) -> _Plan:
    installed = installed_packages(env)
    packages_unknown = installed is None and bool(recorded.tool_packages or recorded.kernel_packages)
    present: set[str] = installed or set()

    link = ""
    link_foreign = ""
    if env.exists(env.cli_path):
        expected = str(env.installed_root / "cli.dist" / "proe")
        if env.link_target(env.cli_path) == expected:
            link = env.cli_path
        else:
            link_foreign = env.cli_path

    kernel = tuple(p for p in recorded.kernel_packages if p in present)
    release = env.kernel_release()
    rt_running = f"linux-image-{release}" in kernel or f"linux-image-unsigned-{release}" in kernel
    return _Plan(
        proe_paths=tuple(p for p in _proe_paths(env, keep_data=options.keep_data) if env.exists(p)),
        link=link,
        link_foreign=link_foreign,
        service=env.exists(str(env.service_path)),
        tool_packages=tuple(p for p in recorded.tool_packages if p in present),
        libyang_files=tuple(
            f for f in host_changes.libyang_files(directory, recorded, env.libyang_root) if env.exists(f)
        ),
        dropin=recorded.grub_dropin if recorded.grub_dropin and env.exists(recorded.grub_dropin) else "",
        kernel_packages=kernel,
        rt_running=rt_running,
        packages_unknown=packages_unknown,
    )


def _print_preview(
    env: InstallEnvironment, plan: _Plan, options: UninstallOptions, recorded: host_changes.HostChanges
) -> None:
    lines = ["", "PROe uninstall preview", "----------------------"]
    if plan.proe_paths or plan.service or plan.link:
        removed = list(plan.proe_paths) + ([plan.link] if plan.link else [])
        lines.append("PROe             : remove " + ", ".join(removed))
        if plan.service:
            lines.append(f"Service          : stop, disable and remove {env.service_path}")
        if not options.keep_data and (str(env.config_dir) in plan.proe_paths or str(env.log_dir) in plan.proe_paths):
            lines.append("Data             : the PROe configuration and the logs and benchmark results are deleted")
    else:
        lines.append("PROe             : already removed")
    if plan.link_foreign:
        lines.append(f"                   {plan.link_foreign} was not created by PROe and is left alone")
    if plan.tool_packages:
        lines.append("Host tools       : purge " + " ".join(plan.tool_packages) + " (installed by ESQ)")
    if plan.libyang_files:
        lines.append(f"libyang          : remove {len(plan.libyang_files)} files that ESQ built and installed")
    if _switches_kernel(plan):
        lines.append(
            "Boot parameters  : remove them and set the next boot to the normal kernel (the ESQ GRUB drop-in is "
            "rewritten, and deleted when the removal finishes)"
        )
    elif plan.dropin:
        lines.append(f"Boot parameters  : delete {plan.dropin} and run update-grub")
    if plan.kernel_packages:
        text = "purge " + " ".join(plan.kernel_packages) + " (installed by ESQ)"
        if plan.rt_running:
            text += (
                ". The real-time kernel is running, so ESQ first sets the next boot to the normal kernel, reboots, "
                "and finishes the removal when you run this command again (no second question)"
            )
        lines.append("Real-time kernel : " + text)
    if plan.packages_unknown:
        lines.append("WARNING          : the installed packages could not be listed, so no packages are removed")
    if not plan.has_work:
        lines.append("Nothing is left to remove except the ESQ state for PROe.")
    kept = ["packages that were installed before ESQ"]
    if recorded.grub_backup:
        kept.insert(0, f"the GRUB backup in {recorded.grub_backup}")
    lines.append("Kept             : " + ", ".join(kept))
    lines.append("")
    env.out("\n".join(lines))


def _purge(
    env: InstallEnvironment,
    directory: Path,
    log_path: Path,
    wanted: Sequence[str],
    allowed: set[str],
    label: str,
    phase: Phase,
) -> bool:
    """Purge packages that ESQ added. False when nothing was removed because apt would remove other packages too."""
    if not wanted:
        return True
    code, output = env.runner.capture(["apt-get", "-s", "purge", *wanted], 120)
    if code != 0:
        env.out(f"WARNING: apt could not simulate removing the {label} ({', '.join(wanted)}); they are left installed.")
        return False
    extra = sorted(set(host_prep.parse_apt_removals(output)) - allowed)
    if extra:
        env.out(
            f"WARNING: removing the {label} would also remove packages that ESQ did not add ({', '.join(extra)}), so "
            f"the {label} are left installed. Remove them yourself if you do not need those packages."
        )
        return False
    _step(env, _apt(["purge", "-y", *wanted]), log_path, directory, privileged=True)
    for name in wanted[:MAX_PACKAGE_NOTES]:
        phase.note(f"- {name}")
    if len(wanted) > MAX_PACKAGE_NOTES:
        phase.note(f"... and {len(wanted) - MAX_PACKAGE_NOTES} more (see the log)")
    return True


def _switches_kernel(plan: _Plan) -> bool:
    """Whether the real-time kernel runs and must be replaced by a normal kernel before it can be purged."""
    return plan.rt_running and bool(plan.kernel_packages) and not plan.packages_unknown


def _count_phases(plan: _Plan) -> int:
    """The number of numbered phases that the uninstall shows."""
    removes_packages = bool(plan.kernel_packages) and not plan.packages_unknown and not plan.rt_running
    return (
        int(bool(plan.proe_paths or plan.service or plan.link))
        + int(bool(plan.tool_packages) and not plan.packages_unknown)
        + int(bool(plan.libyang_files))
        + int(bool(plan.dropin) or _switches_kernel(plan))
        + int(removes_packages)
        + int(bool(plan.dropin or plan.kernel_packages))
    )


def _execute(
    env: InstallEnvironment,
    directory: Path,
    plan: _Plan,
    previous: proe_state.InstallState | None,
    recorded: host_changes.HostChanges,
) -> int:
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    log_path = directory / LOG_NAME
    allowed = set(recorded.kernel_packages) | set(recorded.tool_packages)

    _prime_sudo(env)
    env.progress = Progress(env.out, _count_phases(plan), spinner=env.spinner)
    _watch_output(env)
    proe_state.remove_capability_record(directory)  # PROe profiles disappear first
    if plan.proe_paths or plan.service or plan.link:
        with env.phase("Removing PROe"):
            _remove_proe(env, directory, log_path, plan)
    incomplete = plan.packages_unknown
    if plan.tool_packages and not plan.packages_unknown:
        with env.phase("Removing the host tools") as phase:
            if not _purge(env, directory, log_path, plan.tool_packages, allowed, "host tools", phase):
                incomplete = True
                phase.result = "skipped"
    if plan.libyang_files:
        with env.phase("Removing libyang") as phase:
            _remove_libyang(env, directory, log_path, plan, phase)
    switched = False
    if plan.dropin or _switches_kernel(plan):
        with env.phase("Removing the boot parameters") as phase:
            if _switches_kernel(plan):
                switched = _boot_normal_kernel_next(env, directory, log_path, plan, recorded, phase)
            if not switched and plan.dropin:
                _step(env, ["rm", "-f", "--", plan.dropin], log_path, directory, privileged=True)
                phase.note(f"Deleted {plan.dropin}.")
    if switched:
        return _reboot_before_kernel_removal(env, directory, log_path, previous)

    if plan.kernel_packages and not plan.packages_unknown:
        if plan.rt_running:
            env.out(
                "WARNING: the real-time kernel is running and no normal kernel is installed to boot instead, so it "
                "is left installed. Install one (for example: apt-get install linux-generic), reboot into it, then "
                "run this command again."
            )
            incomplete = True
        else:
            with env.phase("Removing the real-time kernel") as phase:
                if not _purge(env, directory, log_path, plan.kernel_packages, allowed, "real-time kernel", phase):
                    incomplete = True
                    phase.result = "skipped"
    if plan.dropin or plan.kernel_packages:
        with env.phase("Updating the boot menu"):
            _step(env, ["update-grub"], log_path, directory, privileged=True)
    if incomplete:
        env.out("")
        env.out("Some steps could not be completed (see the warnings above).")
        env.out("The ESQ record is kept, so you can run this command again.")
        env.out(f"Log              : {log_path}")
        return EXIT_ACTION_REQUIRED

    _clean_state(directory)
    env.out("")
    env.out("Uninstalled      : PROe and the host changes that 'esq install proe' made.")
    if recorded.grub_backup:
        env.out(f"Kept             : the GRUB backup in {recorded.grub_backup} (delete it when you no longer need it).")
    env.out(f"Log              : {log_path}")
    return EXIT_OK


def _remove_proe(env: InstallEnvironment, directory: Path, log_path: Path, plan: _Plan) -> None:
    if plan.service:
        env.runner.capture(_privileged(env, ["systemctl", "disable", "--now", VENDOR_SERVICE_NAME]), 60)
        _step(env, ["rm", "-f", "--", str(env.service_path)], log_path, directory, privileged=True)
        env.runner.capture(_privileged(env, ["systemctl", "daemon-reload"]), 60)
    if plan.link:
        _step(env, ["rm", "-f", "--", plan.link], log_path, directory, privileged=True)
    for path in plan.proe_paths:
        _step(env, ["rm", "-rf", "--", path], log_path, directory, privileged=True)


def _remove_libyang(env: InstallEnvironment, directory: Path, log_path: Path, plan: _Plan, phase: Phase) -> None:
    if not plan.libyang_files:
        return
    files = list(plan.libyang_files)
    for start in range(0, len(files), _RM_CHUNK):
        _step(env, ["rm", "-f", "--", *files[start : start + _RM_CHUNK]], log_path, directory, privileged=True)
    _step(env, ["ldconfig"], log_path, directory, privileged=True)
    phase.note(f"{len(files)} files removed.")


def _boot_normal_kernel_next(
    env: InstallEnvironment,
    directory: Path,
    log_path: Path,
    plan: _Plan,
    recorded: host_changes.HostChanges,
    phase: Phase,
) -> bool:
    """Rewrite the ESQ GRUB drop-in so that the next boot is the newest normal kernel. False when there is none.

    Without this, GRUB would boot the real-time kernel again after the reboot, because it sorts first by version.
    """
    try:
        text = env.grub_cfg.read_text(encoding="utf-8", errors="replace")
    except OSError:
        text = ""
    entry = host_prep.grub_normal_kernel_entry(text)
    if entry is None:
        return False
    dropin = plan.dropin or recorded.grub_dropin or str(env.grub_dropin_dir / host_prep.DROPIN_NAME)
    staged = directory / "grub-dropin.tmp"
    fd = os.open(staged, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(host_prep.render_dropin((), (), entry))
    try:
        _step(env, ["install", "-D", "-m", "0644", str(staged), dropin], log_path, directory, privileged=True)
    finally:
        staged.unlink(missing_ok=True)
    host_changes.update_host_changes(directory, grub_dropin=dropin)
    phase.note(f"Rewrote {dropin} so that the next boot is the normal kernel.")
    return True


def _reboot_before_kernel_removal(
    env: InstallEnvironment, directory: Path, log_path: Path, previous: proe_state.InstallState | None
) -> int:
    with env.phase("Updating the boot menu"):
        _step(env, ["update-grub"], log_path, directory, privileged=True)
    sha = previous.package_sha256 if previous is not None else "0" * 64
    name = previous.package_name if previous is not None else "unknown"
    state = proe_state.InstallState(proe_state.STATE_UNINSTALL_REBOOT, sha, name, env.boot_id())
    proe_state.write_install_state(state, directory)
    env.out("The real-time kernel cannot be removed while it is running. Rebooting now. After the machine is back,")
    env.out("run the same 'esq uninstall proe' command to finish removing the real-time kernel.")
    code, output = env.runner.capture(_privileged(env, ["systemctl", "reboot"]), 60)
    if code != 0:
        env.out(
            f"The reboot command failed ({output.strip()[:200]}). Reboot this machine manually, then run the "
            "same 'esq uninstall proe' command."
        )
    return EXIT_ACTION_REQUIRED


def _clean_state(directory: Path) -> None:
    """Delete the ESQ state for PROe, except the uninstall log."""
    for entry in Path(directory).iterdir():
        if entry.name == LOG_NAME:
            continue
        if entry.is_dir() and not entry.is_symlink():
            shutil.rmtree(entry, ignore_errors=True)
        else:
            entry.unlink(missing_ok=True)
