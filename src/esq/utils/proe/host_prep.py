# Copyright (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""
Host preparation that PROe needs before its vendor installer runs.

The PROe binary package does not include these, so ``esq install proe`` prepares them:

- a real-time kernel and the kernel boot parameters PROe needs (GRUB),
- the host tools and ``libyang`` that the PROe README lists as prerequisites.

This module only plans and renders. Running commands stays in :mod:`esq.utils.proe.installer`.
"""

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from esq.utils.proe.constants import InstallError

# Kernel command line that PROe needs, on top of the Ubuntu default ("quiet splash").
BOOT_PARAMS: Tuple[str, ...] = (
    "intel_iommu=on",
    "iommu=pt",
    "isolcpus=1,2",
    "rcu_nocbs=1,2",
    "rcu_nocb_poll",
    "irqaffinity=0",
    "kthread_cpus=0",
    "clocksource=tsc",
    "tsc=reliable",
    "hpet=disable",
    "nmi_watchdog=0",
    "nowatchdog",
    "efi=noruntime",
    "xe.max_vfs=7",
    "xe.force_probe=*",
    "modprobe.blacklist=i915",
    "udmabuf.list_limit=8192",
    "console=ttyS0,115200n8",
)
# Parameters the kernel accepts more than once; a different existing value is not a conflict.
REPEATABLE_KEYS = frozenset({"console", "modprobe.blacklist"})

# PROe needs a real-time kernel of at least this version (major, minor, patch).
MIN_RT_KERNEL = (6, 8, 1)
# apt metapackage whose candidate version is the newest real-time kernel that apt can install.
RT_KERNEL_PROBE_PACKAGE = "linux-image-realtime"

GRUB_DEFAULT_FILE = "/etc/default/grub"
GRUB_DROPIN_DIR = "/etc/default/grub.d"
GRUB_CFG_FILE = "/boot/grub/grub.cfg"
GRUB_BACKUP_DIR = "/var/backups/esq/proe-grub"
DROPIN_NAME = "99-esq-proe.cfg"

# Host tools that the PROe README lists, with the apt packages that provide them.
TOOL_PACKAGES: Dict[str, Tuple[str, ...]] = {
    "git": ("git",),
    "tc": ("iproute2",),
    "ethtool": ("ethtool",),
    "ptp4l": ("linuxptp",),
    "phc2sys": ("linuxptp",),
    "tuna": ("tuna",),
    "cpupower": ("linux-tools-common", "linux-tools-generic"),
    "rdmsr": ("msr-tools",),
    "wrmsr": ("msr-tools",),
    "ssh-audit": ("ssh-audit",),
}
# libyang has no apt package that PROe accepts; the README builds it from source.
LIBYANG_TOOL = "yanglint"
LIBYANG_REPO = "https://github.com/CESNET/libyang.git"
LIBYANG_TAG = "v4.2.2"
LIBYANG_BUILD_PACKAGES: Tuple[str, ...] = (
    "python3-dev",
    "gcc",
    "python3-cffi",
    "libpcre2-dev",
    "libffi-dev",
    "python3-pip",
    "cmake",
    "make",
    "git",
)

_ASSIGN_RE = re.compile(r"""^\s*(?:export\s+)?GRUB_CMDLINE_LINUX(?:_DEFAULT)?=(?:"([^"]*)"|'([^']*)')\s*(?:#.*)?$""")
_SAFE_KEY_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
_VERSION_RE = re.compile(r"^(\d+)\.(\d+)(?:\.(\d+))?")
_CANDIDATE_RE = re.compile(r"^\s*Candidate:\s*(\S+)\s*$", re.MULTILINE)
_REMOVAL_RE = re.compile(r"^(?:Remv|Purg) (\S+)", re.MULTILINE)
_GRUB_LINUX_RE = re.compile(r"^\s*linux(?:efi)?\s+(\S+)(.*)$", re.MULTILINE)


def _key(token: str) -> str:
    return token.split("=", 1)[0]


@dataclass(frozen=True)
class BootParamPlan:
    add: Tuple[str, ...]
    present: Tuple[str, ...]
    conflicts: Tuple[str, ...]
    replace_keys: Tuple[str, ...]


def read_configured_tokens(default_file: Path, dropin_dir: Path) -> List[str]:
    """Kernel parameters already set in /etc/default/grub and its drop-ins (ESQ's own drop-in is ignored)."""
    paths = [default_file]
    if dropin_dir.is_dir():
        paths.extend(sorted(p for p in dropin_dir.glob("*.cfg") if p.name != DROPIN_NAME))
    tokens: List[str] = []
    for path in paths:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            match = _ASSIGN_RE.match(line)
            if match:
                value = match.group(1) if match.group(1) is not None else match.group(2)
                tokens.extend(t for t in value.split() if not t.startswith("$"))
    return tokens


def plan_boot_params(configured: Sequence[str], wanted: Sequence[str] = BOOT_PARAMS) -> BootParamPlan:
    """Split the wanted parameters into those to add, those already set, and existing values to replace."""
    add: List[str] = []
    present: List[str] = []
    conflicts: List[str] = []
    replace_keys: List[str] = []
    for token in wanted:
        if token in configured:
            present.append(token)
            continue
        key = _key(token)
        existing = [c for c in configured if _key(c) == key]
        if existing and key not in REPEATABLE_KEYS:
            conflicts.append(f"{token} (already configured as {', '.join(existing)})")
            replace_keys.append(key)
        add.append(token)
    return BootParamPlan(tuple(add), tuple(present), tuple(conflicts), tuple(replace_keys))


def parse_kernel_version(text: str) -> Optional[Tuple[int, int, int]]:
    """Numeric version at the start of a kernel release or package version, e.g. ``6.8.1-1015-realtime``."""
    match = _VERSION_RE.match(text.strip())
    if match is None:
        return None
    return int(match.group(1)), int(match.group(2)), int(match.group(3) or 0)


def kernel_at_least(text: str, minimum: Sequence[int] = MIN_RT_KERNEL) -> bool:
    version = parse_kernel_version(text)
    return version is not None and version >= tuple(minimum)


def format_version(version: Sequence[int]) -> str:
    return ".".join(str(part) for part in version)


def parse_apt_candidate(policy_output: str) -> Optional[str]:
    """The ``Candidate:`` version from ``apt-cache policy`` output, or None when there is none."""
    match = _CANDIDATE_RE.search(policy_output)
    if match is None or match.group(1) == "(none)":
        return None
    return match.group(1)


def parse_apt_removals(simulation_output: str) -> List[str]:
    """Package names that ``apt-get -s remove`` or ``purge`` says it would remove."""
    return _REMOVAL_RE.findall(simulation_output)


def missing_from_cmdline(cmdline: str, wanted: Sequence[str] = BOOT_PARAMS) -> List[str]:
    running = set(cmdline.split())
    return [token for token in wanted if token not in running]


def grub_cfg_problems(text: str, wanted: Sequence[str], need_rt_kernel: bool) -> List[str]:
    """What the generated grub.cfg lacks: a real-time kernel entry and the boot parameters that PROe needs."""
    entries = [(m.group(1), m.group(2).split()) for m in _GRUB_LINUX_RE.finditer(text)]
    if not entries:
        return ["it contains no kernel entry"]
    problems: List[str] = []
    if need_rt_kernel and not any("realtime" in image for image, _ in entries):
        problems.append("it has no real-time kernel entry")
    scope = "real-time kernel entry" if need_rt_kernel else "kernel entry"
    candidates = [args for image, args in entries if not need_rt_kernel or "realtime" in image]
    problems.extend(
        f"boot parameter {token} is not on any {scope}"
        for token in wanted
        if not any(token in args for args in candidates)
    )
    return problems


def render_dropin(params: Sequence[str], replace_keys: Sequence[str] = ()) -> str:
    """Content of the GRUB drop-in. Remove the file and run update-grub to undo it."""
    for value in params:
        if any(ch in value for ch in '"`$\\\n'):
            raise InstallError(f"Refusing to write an unsafe GRUB value: {value!r}")
    for key in replace_keys:
        if not _SAFE_KEY_RE.match(key):
            raise InstallError(f"Refusing to write an unsafe GRUB parameter name: {key!r}")
    lines = ["# Written by 'esq install proe'. Remove this file and run update-grub to undo it."]
    added = " ".join(params)
    if replace_keys:
        patterns = "|".join(f'"{key}="*' for key in replace_keys)
        lines += [
            "# Drop the existing values of the parameters PROe sets, then add the values PROe needs.",
            "_esq_drop() {",
            "    _esq_out=",
            "    set -f",
            "    for _esq_tok in $1; do",
            '        case "$_esq_tok" in',
            f"            {patterns}) ;;",
            '            *) _esq_out="${_esq_out:+$_esq_out }$_esq_tok" ;;',
            "        esac",
            "    done",
            "    set +f",
            '    printf "%s" "$_esq_out"',
            "}",
            'GRUB_CMDLINE_LINUX="$(_esq_drop "$GRUB_CMDLINE_LINUX") ' + added + '"',
            'GRUB_CMDLINE_LINUX_DEFAULT="$(_esq_drop "$GRUB_CMDLINE_LINUX_DEFAULT")"',
        ]
    elif params:
        lines.append('GRUB_CMDLINE_LINUX="$GRUB_CMDLINE_LINUX ' + added + '"')
    return "\n".join(lines) + "\n"


@dataclass(frozen=True)
class DependencyPlan:
    tools: Tuple[str, ...]
    packages: Tuple[str, ...]
    build_libyang: bool


def plan_dependencies(available: Callable[[str], bool]) -> DependencyPlan:
    """Which host tools are missing, the apt packages to install, and whether libyang must be built."""
    tools: List[str] = []
    packages = set()
    for tool, pkgs in TOOL_PACKAGES.items():
        if not available(tool):
            tools.append(tool)
            packages.update(pkgs)
    build_libyang = not available(LIBYANG_TOOL)
    if build_libyang:
        tools.append(LIBYANG_TOOL)
        packages.update(LIBYANG_BUILD_PACKAGES)
    return DependencyPlan(tuple(tools), tuple(sorted(packages)), build_libyang)
