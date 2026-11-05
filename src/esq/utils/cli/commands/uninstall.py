# Copyright (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""``esq uninstall`` command: undo the installation of an optional backend."""

from esq.utils.proe.installer import EXIT_ERROR, InstallEnvironment, SubprocessRunner
from esq.utils.proe.progress import live_enabled
from esq.utils.proe.uninstaller import UninstallOptions, run_uninstall

SUPPORTED_TARGETS = ("proe",)


def uninstall_package(
    target: str,
    assume_yes: bool = False,
    keep_data: bool = False,
    verbose: bool = False,
) -> int:
    """Uninstall an optional backend and undo the host changes that its installation made.

    Args:
        target: Backend to uninstall (only ``proe`` is supported).
        assume_yes: Consent to the previewed removal without prompting.
        keep_data: Keep the PROe configuration and the logs and benchmark results.
        verbose: Show the raw output of the commands instead of one progress line per phase.

    Returns:
        Exit code: 0 success, 1 error, 2 user action required (declined, reboot pending).
    """
    if target not in SUPPORTED_TARGETS:
        print(f"ERROR: unsupported uninstall target '{target}'. Supported: {', '.join(SUPPORTED_TARGETS)}")
        return EXIT_ERROR
    options = UninstallOptions(assume_yes=assume_yes, keep_data=keep_data)
    env = InstallEnvironment(runner=SubprocessRunner(echo=verbose), spinner=live_enabled(verbose))
    return run_uninstall(options, env)
