# Copyright (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""
Host preparation that PROe needs before its vendor installer runs.

The PROe binary package does not include these, so ``esq install proe`` prepares them:

- a real-time kernel and the kernel boot parameters PROe needs (GRUB),
- the host tools and ``libyang`` that the PROe README lists as prerequisites.

Real-time kernel: ``apt-get install -y ubuntu-realtime`` installs the newest real-time kernel of the running
Ubuntu release (no Ubuntu Pro subscription is needed) and makes it the default boot entry. ESQ names no kernel
version. Before it installs anything, ESQ checks with ``apt-cache policy`` that apt offers the package.

Boot parameters: :data:`BOOT_PARAMS` are written to ``/etc/default/grub.d/99-esq-proe.cfg``, never to
``/etc/default/grub`` or another drop-in. A parameter that is already set is not added again. A parameter that
is set to a different value, for example ``isolcpus=3``, is shown in the preview as ``replaces: ...`` and the
drop-in removes the old value from ``GRUB_CMDLINE_LINUX`` and ``GRUB_CMDLINE_LINUX_DEFAULT`` first. Parameters
that PROe does not set are kept. After ``update-grub``, the generated ``/boot/grub/grub.cfg`` must contain a
real-time kernel entry that carries every parameter.

Backup and revert: before the first change, ESQ copies ``/etc/default/grub``, the other drop-ins and the running
kernel command line to ``/var/backups/esq/proe-grub/`` (``default-grub``, ``grub.d/``, ``cmdline-before``). An
existing backup is the original and is never overwritten. To revert by hand, delete the drop-in and run
``update-grub``; ``esq uninstall proe`` does this and removes what ESQ installed.

Host tools: the PROe package expects ``git``, ``tc``, ``ethtool``, ``ptp4l``, ``phc2sys``, ``tuna``, ``cpupower``,
``rdmsr``, ``wrmsr``, ``ssh-audit`` and ``yanglint`` (:data:`TOOL_PACKAGES`). ESQ installs the missing ones with
``apt-get``. ``libyang`` is built from source (:data:`LIBYANG_TAG`) and installed to ``/usr/local``. Without
ESQ's step, the PROe installer stops with ``Missing required host tool`` before it changes anything.

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
_SAFE_ENTRY_RE = re.compile(r"^[A-Za-z0-9_.>-]+$")
_REMOVAL_RE = re.compile(r"^(?:Remv|Purg) (\S+)", re.MULTILINE)
_GRUB_LINUX_RE = re.compile(r"^\s*linux(?:efi)?\s+(\S+)(.*)$", re.MULTILINE)
_GRUB_SUBMENU_RE = re.compile(r"^submenu\s+'[^']*'.*?\$menuentry_id_option\s+'([^']+)'", re.MULTILINE)
_GRUB_ENTRY_RE = re.compile(
    r"^([ \t]*)menuentry\s+'([^']*)'.*?\$menuentry_id_option\s+'([^']+)'", re.MULTILINE
)


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


def parse_apt_removals(simulation_output: str) -> List[str]:
    """Package names that ``apt-get -s remove`` or ``purge`` says it would remove."""
    return _REMOVAL_RE.findall(simulation_output)


def missing_from_cmdline(cmdline: str, wanted: Sequence[str] = BOOT_PARAMS) -> List[str]:
    running = set(cmdline.split())
    return [token for token in wanted if token not in running]


def grub_default_entry(text: str, release: str) -> Optional[str]:
    """The ``GRUB_DEFAULT`` value that boots kernel ``release`` (not its recovery mode), or None.

    An entry that sits in the ``Advanced options`` submenu is addressed as ``submenu>entry``.
    """
    submenus = [(m.start(), m.group(1)) for m in _GRUB_SUBMENU_RE.finditer(text)]
    for match in _GRUB_ENTRY_RE.finditer(text):
        indent, title, entry_id = match.groups()
        if not title.endswith(f"Linux {release}"):
            continue
        parents = [sub_id for start, sub_id in submenus if start < match.start()]
        # An indented entry sits inside the 'Advanced options' submenu and is addressed as submenu>entry.
        return f"{parents[-1]}>{entry_id}" if indent and parents else entry_id
    return None


def grub_normal_kernel_entry(text: str) -> Optional[str]:
    """The ``GRUB_DEFAULT`` value of the newest installed kernel that is not a real-time kernel, or None."""
    for match in _GRUB_LINUX_RE.finditer(text):
        release = match.group(1).rsplit("vmlinuz-", 1)[-1]
        if "vmlinuz-" in match.group(1) and "realtime" not in release:
            return grub_default_entry(text, release)
    return None


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


def render_dropin(params: Sequence[str], replace_keys: Sequence[str] = (), default_entry: str = "") -> str:
    """Content of the GRUB drop-in. Remove the file and run update-grub to undo it."""
    if default_entry and not _SAFE_ENTRY_RE.match(default_entry):
        raise InstallError(f"Refusing to write an unsafe GRUB boot entry: {default_entry!r}")
    for value in params:
        if any(ch in value for ch in '"`$\\\n'):
            raise InstallError(f"Refusing to write an unsafe GRUB value: {value!r}")
    for key in replace_keys:
        if not _SAFE_KEY_RE.match(key):
            raise InstallError(f"Refusing to write an unsafe GRUB parameter name: {key!r}")
    lines = ["# Written by 'esq install proe'. Remove this file and run update-grub to undo it."]
    if default_entry:
        lines += [
            "# Boot the normal kernel next, so that the real-time kernel can be removed.",
            f'GRUB_DEFAULT="{default_entry}"',
        ]
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
