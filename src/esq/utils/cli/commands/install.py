# Copyright (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""``esq install`` command: install an optional backend from a local package."""

from pathlib import Path

from esq.utils.proe.installer import (
    EXIT_ERROR,
    InstallEnvironment,
    InstallOptions,
    SubprocessRunner,
    run_install,
)
from esq.utils.proe.progress import live_enabled

SUPPORTED_TARGETS = ("proe",)


def install_package(
    target: str,
    package: str,
    sha256: str,
    assume_yes: bool = False,
    allow_reboot: bool = False,
    verbose: bool = False,
) -> int:
    """Install an optional backend.

    Args:
        target: Backend to install (only ``proe`` is supported).
        package: Path to the manually downloaded package.
        sha256: Trusted SHA-256 of the package.
        assume_yes: Consent to the previewed installation without prompting.
        allow_reboot: Consent to a reboot when the package declares one is required.
        verbose: Show the raw output of the commands instead of one progress line per phase.

    Returns:
        Exit code: 0 success, 1 error, 2 user action required (declined, reboot pending).
    """
    if target not in SUPPORTED_TARGETS:
        print(f"ERROR: unsupported install target '{target}'. Supported: {', '.join(SUPPORTED_TARGETS)}")
        return EXIT_ERROR
    options = InstallOptions(
        package=Path(package),
        sha256=sha256,
        assume_yes=assume_yes,
        allow_reboot=allow_reboot,
    )
    env = InstallEnvironment(runner=SubprocessRunner(echo=verbose), spinner=live_enabled(verbose))
    return run_install(options, env)
