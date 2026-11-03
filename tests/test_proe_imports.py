# Copyright (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Exercise utility initialization and profile discovery in fresh interpreters."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _python(source: str) -> None:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src")
    env["SYSAGENT_CLI_PACKAGE"] = "esq"
    result = subprocess.run(
        [sys.executable, "-c", source],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("package", ["sysagent.utils", "esq.utils"])
def test_utility_packages_do_not_eagerly_import_features(package):
    _python(
        f"import importlib, sys; package = importlib.import_module({package!r}); "
        "assert not any(name.startswith(package.__name__ + '.') for name in sys.modules); "
        "assert not any(name in sys.modules for name in ('kagglehub', 'openvino', 'torch'))"
    )


def test_real_profile_discovery_and_explicit_parser_selection():
    _python(
        """
from sysagent.utils.config import list_profiles
from sysagent.utils.cli.parsers import get_argument_parser
from esq.suites.realtime.proe.runner import ProeContractError, load_profile_binding

expected = {
    "profile.suite.realtime.proe-single-host-tcc": True,
    "profile.suite.realtime.proe-single-host-non-tcc": False,
}
profiles = {
    item["configs"]["name"]: item["configs"]
    for group in list_profiles(include_examples=True).values()
    for item in group
}
defaults = {
    item["configs"].get("name")
    for group in list_profiles(include_examples=False).values()
    for item in group
}
assert expected.keys() <= profiles.keys()
assert not expected.keys() & defaults
for name, mode in expected.items():
    args = get_argument_parser().parse_args(["run", "--profile", name, "--force", "--no-cache"])
    assert args.command == "run" and args.profile == name and args.no_cache and args.force
    suite, = profiles[args.profile]["suites"]
    sub_suite, = suite["sub_suites"]
    assert suite["name"] == "realtime" and sub_suite["name"] == "proe"
    config, = sub_suite["tests"]["test_proe_single_host"]["params"]
    assert config["proe_enabled"] is True and config["expected_tcc_enabled"] is mode
    try:
        load_profile_binding(config["profile_file"], config["expected_tcc_enabled"])
    except ProeContractError as error:
        assert "profile_file" in str(error)
    else:
        raise AssertionError("Unset example unexpectedly accepted")
"""
    )
