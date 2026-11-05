# Copyright (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Constants and exceptions shared by the PROe integration modules."""

import re
from dataclasses import dataclass
from typing import Dict, Tuple

BACKEND_NAME = "proe"

# Supported host platforms. Binary packages are built per OS release and architecture.
SUPPORTED_OS_ID = "ubuntu"
SUPPORTED_OS_VERSIONS: Tuple[str, ...] = ("24.04", "26.04")
SUPPORTED_ARCH = "x86_64"

# Real-time benchmarks need a PREEMPT_RT kernel. Ubuntu ships it as this apt metapackage (no Ubuntu Pro needed).
RT_KERNEL_PACKAGE = "ubuntu-realtime"
# Present and "1" only while a PREEMPT_RT kernel is running.
RT_KERNEL_SYSFS = "/sys/kernel/realtime"

# Files that a package must contain. Paths are relative to the single package root directory.
PROVENANCE_FILE = "PROVENANCE.json"
CAPABILITIES_FILE = "esq-capabilities.json"
INSTALLER_FILE = "install.sh"
UNINSTALLER_FILE = "uninstall.sh"
OS_VERSION_FILE = "assets/ubuntu-version"
REQUIRED_PACKAGE_FILES: Tuple[str, ...] = (
    PROVENANCE_FILE,
    INSTALLER_FILE,
    UNINSTALLER_FILE,
    OS_VERSION_FILE,
    "cli.dist/proe",
    "daemon.dist/proe",
)
# Installed runtime trees that are re-verified against the package manifest after installation.
INSTALLED_TREES: Tuple[str, ...] = ("cli.dist", "daemon.dist")

# Archive safety limits.
MAX_ARCHIVE_ENTRIES = 5000
MAX_ARCHIVE_UNCOMPRESSED_BYTES = 2 * 1024**3
MAX_MEMBER_BYTES = 1024**3
MAX_COMPRESSION_RATIO = 2000
MAX_JSON_MEMBER_BYTES = 4 * 1024 * 1024
HASH_CHUNK_BYTES = 1024 * 1024

# Package identity, for example ``proe-binary-ubuntu24.04-x86_64-<revision>``.
PACKAGE_NAME_RE = re.compile(r"^proe-binary-ubuntu(?P<os>\d{2}\.\d{2})-(?P<arch>[a-z0-9_]+)-(?P<rev>[0-9a-f]{7,40})$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
# Identifier of a PROe profile as passed on the PROe command line.
PROE_PROFILE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

# Locations created by the vendor installer. Used to detect an existing installation.
VENDOR_RUNTIME_DIR = "/opt/proe"
VENDOR_CLI_PATH = "/usr/local/bin/proe"
VENDOR_SERVICE_NAME = "proe.service"
VENDOR_SERVICE_PATH = "/etc/systemd/system/proe.service"
VENDOR_CONFIG_DIR = "/etc/proe"
VENDOR_LOG_DIR = "/var/log/proe"
VENDOR_APP_CONFIG = "/etc/proe/app_config/proe_app.conf"
DEFAULT_RESULTS_DIR = "/var/log/proe/benchmark_results"
VENDOR_EXISTING_PATHS: Tuple[str, ...] = (VENDOR_RUNTIME_DIR, VENDOR_CLI_PATH, VENDOR_SERVICE_PATH)

CAPABILITIES_SCHEMA = "esq-proe-capabilities/1"
CAPABILITY_RECORD_SCHEMA = "esq-proe-installed/1"
INSTALL_STATE_SCHEMA = "esq-proe-install-state/1"

# The only profile topology that this integration supports.
TOPOLOGY_SINGLE_DUT = "single-dut"


@dataclass(frozen=True)
class KnownProfile:
    """An ESQ profile that this integration may expose once an installed package supports it."""

    esq_name: str
    test_id: str


# ESQ owns the profile names and definitions. A package can only select from this table;
# it can never introduce a profile name or a profile path.
KNOWN_PROFILES: Dict[str, KnownProfile] = {
    "profile.suite.realtime.proe_1ms_ai": KnownProfile("profile.suite.realtime.proe_1ms_ai", "RT-PROE-001"),
    "profile.suite.realtime.proe_control": KnownProfile("profile.suite.realtime.proe_control", "RT-PROE-002"),
}


@dataclass(frozen=True)
class ReportContract:
    """Description of a structured PROe report schema version understood by ESQ."""

    version: str
    provisional: bool
    note: str


# Report schema versions understood by the parser in ``report.py``.
# Version 1.0 has its measurement section mapped, but the single-DUT report contract has not been
# confirmed, so it stays provisional.
REPORT_CONTRACTS: Dict[str, ReportContract] = {
    "1.0": ReportContract(
        version="1.0",
        provisional=True,
        note="Single-DUT report contract not yet confirmed.",
    ),
}


class ProeError(Exception):
    """Base class for PROe integration errors. Messages are safe to show to the user."""


class PackageError(ProeError):
    """The supplied package failed validation."""


class InstallError(ProeError):
    """Installation could not be completed."""


class StateError(ProeError):
    """The persisted installation state or capability record is missing or invalid."""


class ReportError(ProeError):
    """A PROe report is absent, ambiguous or does not satisfy the expected contract."""
