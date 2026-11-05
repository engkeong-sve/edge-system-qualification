# Copyright (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""
Thin adapter around the installed PROe command line.

The adapter runs ``proe profile run-benchmark <profile>`` for a profile that the verified capability
record lists, finds the structured report that this exact run produced, and parses it strictly.
It never fabricates a value and never reuses an earlier report: only a report file that did not exist
before the run started and whose profile and timestamp match the run is accepted.

A run is an error, not a result, when the PROe service is not active, the run times out (one hour by default),
no new report or more than one new report appears, the report is older than the run or belongs to another
profile, a field is missing, null, not finite or has an unexpected unit, type or status value, or the exit code
and the report status contradict each other. The command, its output and the raw report are attached to the
ESQ report as evidence. Network and clock settings belong to the PROe profile of the installed package, not to
ESQ.

The command form ``proe profile run-benchmark <profile>`` is only used for a profile that an installed
package declared as single DUT with two NICs (PROe topology ``single-dut-dual-nic``, for example the PROe
profile ``1ms_ai_single_dut``). PROe picks that topology itself when no remote credentials are given, and
takes the two NIC names from the installed PROe profile file, so ESQ passes no NIC or credential option. It
must not be read as evidence that any PROe revision supports such a run: a package whose benchmark needs
extra arguments is never accepted, so this adapter is never reached for it.
"""

import json
import logging
import os
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Set, Tuple

from esq.utils.proe.availability import resolve_profile
from esq.utils.proe.constants import VENDOR_SERVICE_NAME, ProeError, ReportError
from esq.utils.proe.report import ParsedReport, check_run_consistency, parse_report_text

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_S = 3600
REPORT_GLOB = "benchmark_*.json"
MAX_REPORT_BYTES = 8 * 1024 * 1024


@dataclass(frozen=True)
class CommandOutput:
    """Outcome of one executed command."""

    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False


Executor = Callable[[Sequence[str], int], CommandOutput]


def subprocess_executor(argv: Sequence[str], timeout: int) -> CommandOutput:
    """Run a command (argument list, no shell) and capture its output."""
    try:
        proc = subprocess.run(  # noqa: S603 - argument list, executable path comes from the verified record
            list(argv),
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
            stdin=subprocess.DEVNULL,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return CommandOutput(-1, _text(exc.stdout), _text(exc.stderr), timed_out=True)
    except OSError as exc:
        return CommandOutput(127, "", f"{argv[0]}: {exc}")
    return CommandOutput(proc.returncode, proc.stdout or "", proc.stderr or "")


def _text(value: object) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value if isinstance(value, str) else ""


class RunFailure(ProeError):
    """The benchmark run did not produce a trustworthy result. Carries raw evidence for the test report."""

    def __init__(self, message: str, evidence: Optional[Dict[str, str]] = None):
        super().__init__(message)
        self.evidence: Dict[str, str] = evidence or {}


@dataclass(frozen=True)
class BenchmarkRun:
    """A completed run with its parsed report and the raw evidence."""

    command: Tuple[str, ...]
    exit_code: int
    stdout: str
    stderr: str
    report_path: Path
    report_text: str
    report: ParsedReport
    esq_profile: str
    proe_profile: str
    package_name: str
    source_commit: str
    duration_s: float


def build_command(executable: str, proe_profile: str, is_root: bool) -> List[str]:
    """Return the argument list for a run. No shell, no unvalidated text."""
    base = [executable, "profile", "run-benchmark", proe_profile]
    if is_root:
        return base
    # -n: never prompt from inside a test run; the user primes credentials with 'sudo -v' beforehand.
    return ["sudo", "-n", "--"] + base


def list_reports(results_dir: Path) -> Set[str]:
    """Names of the report files currently in a results directory."""
    try:
        return {entry.name for entry in results_dir.glob(REPORT_GLOB) if entry.is_file()}
    except OSError as exc:
        raise ReportError(f"The PROe results directory cannot be read ({results_dir}): {exc}") from exc


def collect_new_report(results_dir: Path, before: Set[str]) -> Tuple[Path, str]:
    """Return the single report file that appeared after ``before`` was taken."""
    new = sorted(list_reports(results_dir) - before)
    if not new:
        raise ReportError(
            f"PROe produced no new benchmark report in {results_dir}. "
            "A report is only written when metrics were collected; no result can be reported."
        )
    if len(new) > 1:
        raise ReportError(
            f"More than one new PROe report appeared in {results_dir} ({', '.join(new)}); "
            "another PROe run may be active. The result cannot be attributed to this run."
        )
    path = results_dir / new[0]
    try:
        if path.stat().st_size > MAX_REPORT_BYTES:
            raise ReportError(f"The PROe report {path.name} is unexpectedly large.")
        return path, path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ReportError(f"The PROe report {path.name} cannot be read: {exc}") from exc


def run_benchmark(
    esq_profile: str,
    *,
    timeout_s: int = DEFAULT_TIMEOUT_S,
    executor: Executor = subprocess_executor,
    directory: Optional[Path] = None,
    is_root: Optional[bool] = None,
    clock: Callable[[], float] = time.time,
) -> BenchmarkRun:
    """Run one PROe benchmark profile and return its validated report.

    Raises:
        RunFailure: With raw evidence attached, when the run or its report is not trustworthy.
        ProeError: When the installed PROe backend is not usable for this profile.
    """
    record, profile = resolve_profile(esq_profile, directory)
    root = is_root if is_root is not None else (hasattr(os, "geteuid") and os.geteuid() == 0)
    results_dir = Path(record.results_dir)

    version = executor([record.executable, "--version"], 30)
    if version.returncode != 0:
        raise ProeError(f"The PROe command is not usable ('{record.executable} --version' exited {version.returncode}).")
    service = executor(["systemctl", "is-active", VENDOR_SERVICE_NAME], 30)
    if service.returncode != 0 or service.stdout.strip() != "active":
        raise ProeError(f"The {VENDOR_SERVICE_NAME} service is not active; start it before running a benchmark.")

    before = list_reports(results_dir)
    argv = build_command(record.executable, profile.proe_profile, root)
    started = clock()
    start_floor = datetime.fromtimestamp(int(started), tz=timezone.utc)
    logger.info("Running PROe benchmark: " + " ".join(argv))
    outcome = executor(argv, timeout_s)
    duration = clock() - started

    evidence = {"command": " ".join(argv), "stdout": outcome.stdout, "stderr": outcome.stderr}
    if outcome.timed_out:
        raise RunFailure(f"The PROe benchmark did not finish within {timeout_s} seconds.", evidence)
    if outcome.returncode != 0:
        raise RunFailure(
            f"PROe exited with code {outcome.returncode}"
            + (" (sudo needs credentials: run 'sudo -v' first)." if "password" in outcome.stderr.lower() else "."),
            evidence,
        )

    try:
        path, text = collect_new_report(results_dir, before)
        evidence["report_file"] = path.name
        evidence["report"] = text
        report = parse_report_text(text, profile.proe_profile, profile.report_schema)
    except ReportError as exc:
        raise RunFailure(str(exc), evidence) from exc

    if report.timestamp < start_floor:
        raise RunFailure("The PROe report is older than this run; refusing to use a stale result.", evidence)
    inconsistency = check_run_consistency(report, outcome.returncode)
    if inconsistency:
        raise RunFailure(inconsistency, evidence)

    return BenchmarkRun(
        command=tuple(argv),
        exit_code=outcome.returncode,
        stdout=outcome.stdout,
        stderr=outcome.stderr,
        report_path=path,
        report_text=text,
        report=report,
        esq_profile=esq_profile,
        proe_profile=profile.proe_profile,
        package_name=record.package_name,
        source_commit=record.source_commit,
        duration_s=duration,
    )


def report_as_json(run: BenchmarkRun) -> str:
    """Pretty-printed raw report for attaching as evidence."""
    return json.dumps(json.loads(run.report_text), indent=2, sort_keys=True)
