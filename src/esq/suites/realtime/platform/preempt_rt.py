# Copyright (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Reusable PREEMPT_RT kernel detection shared by realtime qualification tests."""

import logging
import os
import re

logger = logging.getLogger(__name__)


def _read_text_file(path: str) -> str | None:
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as file:
            return file.read()
    except OSError:
        return None


def _read_cmdline() -> str | None:
    return _read_text_file("/proc/cmdline")


def _check_boot_params(cmdline: str | None, required_params: list[str]) -> dict:
    if not cmdline:
        return {
            "compliant": False,
            "present": [],
            "missing": list(required_params),
            "cmdline_available": False,
        }

    cmdline_tokens = set(cmdline.strip().split())
    present = [param for param in required_params if param in cmdline_tokens]
    missing = [param for param in required_params if param not in cmdline_tokens]
    return {
        "compliant": not missing,
        "present": present,
        "missing": missing,
        "cmdline_available": True,
    }


def _get_kernel_release() -> str:
    from sysagent.utils.core import run_command

    result = run_command(["uname", "-r"], timeout=10)
    if result and result.returncode == 0 and result.stdout:
        return result.stdout.strip()
    return ""


def _read_kernel_config(kernel_release: str) -> str | None:
    safe_release = re.sub(r"[^A-Za-z0-9._+\-]", "", kernel_release or "")
    if not safe_release:
        return None
    return _read_text_file(os.path.join("/boot", f"config-{safe_release}"))


def _detect_preemption_model(kernel_config: str | None) -> str:
    if not kernel_config:
        return "unknown"
    if re.search(r"^CONFIG_PREEMPT_RT=y", kernel_config, re.MULTILINE):
        return "rt"
    if re.search(r"^CONFIG_PREEMPT(_LL)?=y", kernel_config, re.MULTILINE):
        return "full"
    if re.search(r"^CONFIG_PREEMPT_VOLUNTARY=y", kernel_config, re.MULTILINE):
        return "voluntary"
    if re.search(r"^CONFIG_PREEMPT_NONE=y", kernel_config, re.MULTILINE):
        return "none"
    return "unknown"


def detect_preempt_rt(required_boot_params: list[str] | None = None) -> tuple[bool, dict]:
    """Return whether the running kernel has PREEMPT_RT and optional boot params."""
    kernel_release = _get_kernel_release()
    kernel_config = _read_kernel_config(kernel_release)
    config_rt = bool(kernel_config and re.search(r"^CONFIG_PREEMPT_RT=y", kernel_config, re.MULTILINE))
    details = {
        "kernel_release": kernel_release or "unknown",
        "preemption_model": _detect_preemption_model(kernel_config),
    }
    if required_boot_params is not None:
        details["boot_params"] = _check_boot_params(_read_cmdline(), required_boot_params)
    return config_rt, details
