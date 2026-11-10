# Copyright (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""
Strict parser for the structured PROe benchmark report.

The parser maps only values that are present in the report. A missing, non-numeric, non-finite or
placeholder value, a wrong unit or an unknown enumeration value is an error, never a default.
ESQ does not derive any value that the report does not contain (for example a maximum latency),
and it does not decide qualification: the PROe ``status`` records whether the PROe pipeline
completed and is surfaced as informational metadata only.
"""

import json
import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from esq.utils.proe.constants import PROE_EXIT_KPI_FAILED, REPORT_CONTRACTS, ReportError

_STATUS_VALUES = ("PASS", "FAIL")
_PTP_VALUES = ("LOCKED", "UNLOCKED")
_LATENCY_SECTIONS = ("rtt", "processing_time", "rx_workload")
_TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


@dataclass(frozen=True)
class ReportMetric:
    """One measured value from a report."""

    name: str
    value: float
    unit: str
    key: bool = False


@dataclass(frozen=True)
class ParsedReport:
    """Validated report content."""

    schema_version: str
    provisional_schema: bool
    profile_id: str
    timestamp_utc: str
    timestamp: datetime
    pipeline_status: str
    ptp_sync_status: str
    metrics: Tuple[ReportMetric, ...]


def parse_timestamp(value: Any, field: str = "timestamp_utc") -> datetime:
    """Parse the ``YYYY-MM-DDTHH:MM:SSZ`` timestamp form used by reports."""
    if not isinstance(value, str) or not _TIMESTAMP_RE.match(value):
        raise ReportError(f"Report field '{field}' must be a UTC timestamp like 2026-01-31T12:00:00Z.")
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def parse_report_text(text: str, expected_profile: str, expected_schema: str) -> ParsedReport:
    """Parse raw report text.

    Args:
        text: Report file content.
        expected_profile: PROe profile identifier that the run requested.
        expected_schema: Report schema version the installed package declares for that profile.

    Raises:
        ReportError: On any contract violation.
    """
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise ReportError(f"The PROe report is not valid JSON: {exc}") from exc
    return parse_report(data, expected_profile, expected_schema)


def parse_report(data: Any, expected_profile: str, expected_schema: str) -> ParsedReport:
    """Validate a decoded report and map it to ESQ metrics. See :func:`parse_report_text`."""
    if not isinstance(data, dict):
        raise ReportError("The PROe report must be a JSON object.")

    schema = data.get("schema_version")
    if schema is None:
        raise ReportError("The PROe report has no schema_version.")
    if schema not in REPORT_CONTRACTS:
        raise ReportError(f"Unsupported PROe report schema_version {schema!r}; supported: {sorted(REPORT_CONTRACTS)}.")
    if schema != expected_schema:
        raise ReportError(
            f"The PROe report uses schema {schema!r} but the installed package declares {expected_schema!r} for this profile."
        )

    profile_id = data.get("profile_id")
    if profile_id != expected_profile:
        raise ReportError(f"The PROe report is for profile {profile_id!r}, expected {expected_profile!r}.")

    timestamp_text = data.get("timestamp_utc")
    timestamp = parse_timestamp(timestamp_text)

    status = data.get("status")
    if status not in _STATUS_VALUES:
        raise ReportError(f"Report field 'status' must be one of {_STATUS_VALUES}, got {status!r}.")

    results = _object(data, "results", "report")
    perf = _object(results, "rt_performance", "results")
    metrics: List[ReportMetric] = []

    for section in _LATENCY_SECTIONS:
        block = _object(perf, section, "results.rt_performance")
        latency = _object(block, "latency", f"results.rt_performance.{section}")
        where = f"results.rt_performance.{section}.latency"
        _require_unit(latency, "us", where)
        metrics.append(ReportMetric(f"{section}_mean_us", _number(latency, "mean", where), "us", key=True))
        metrics.append(ReportMetric(f"{section}_jitter_us", _number(latency, "jitter", where, minimum=0.0), "us"))
        metrics.append(
            ReportMetric(f"{section}_outliers", _integer(block, "outliers", f"results.rt_performance.{section}"), "count")
        )

    loss = _object(perf, "packet_loss", "results.rt_performance")
    _require_unit(loss, "percent", "results.rt_performance.packet_loss")
    metrics.append(
        ReportMetric(
            "packet_loss_percent",
            _number(loss, "value", "results.rt_performance.packet_loss", minimum=0.0, maximum=100.0),
            "percent",
            key=True,
        )
    )

    clock = _object(results, "clock_sync", "results")
    offset = _object(clock, "offset", "results.clock_sync")
    _require_unit(offset, "ns", "results.clock_sync.offset")
    metrics.append(ReportMetric("clock_offset_mean_ns", _number(offset, "mean", "results.clock_sync.offset"), "ns"))
    ptp = clock.get("ptp_sync_status")
    if ptp not in _PTP_VALUES:
        raise ReportError(f"Report field 'ptp_sync_status' must be one of {_PTP_VALUES}, got {ptp!r}.")
    metrics.append(
        ReportMetric("clock_sample_count", _integer(clock, "sample_count", "results.clock_sync", minimum=1), "count")
    )

    return ParsedReport(
        schema_version=schema,
        provisional_schema=REPORT_CONTRACTS[schema].provisional,
        profile_id=profile_id,
        timestamp_utc=timestamp_text,
        timestamp=timestamp,
        pipeline_status=status,
        ptp_sync_status=ptp,
        metrics=tuple(metrics),
    )


def check_run_consistency(report: ParsedReport, exit_code: int) -> Optional[str]:
    """Return a problem text when the CLI exit code and the report disagree, else ``None``."""
    if exit_code == 0 and report.pipeline_status != "PASS":
        return f"PROe exited successfully but the report status is {report.pipeline_status}."
    if exit_code == PROE_EXIT_KPI_FAILED and report.pipeline_status != "FAIL":
        return f"PROe exited with code {exit_code} (KPI failed) but the report status is {report.pipeline_status}."
    return None


def _object(parent: Dict[str, Any], key: str, where: str) -> Dict[str, Any]:
    if key not in parent:
        raise ReportError(f"Report is missing '{key}' in {where}.")
    value = parent[key]
    if not isinstance(value, dict):
        raise ReportError(f"Report field '{where}.{key}' must be an object.")
    return value


def _number(
    parent: Dict[str, Any],
    key: str,
    where: str,
    minimum: Optional[float] = None,
    maximum: Optional[float] = None,
) -> float:
    if key not in parent:
        raise ReportError(f"Report is missing '{key}' in {where}.")
    value = parent[key]
    if value is None or isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ReportError(f"Report field '{where}.{key}' must be a number, got {value!r}.")
    number = float(value)
    if not math.isfinite(number):
        raise ReportError(f"Report field '{where}.{key}' is not a finite number.")
    if minimum is not None and number < minimum:
        raise ReportError(f"Report field '{where}.{key}' is below {minimum}: {number}.")
    if maximum is not None and number > maximum:
        raise ReportError(f"Report field '{where}.{key}' is above {maximum}: {number}.")
    return number


def _integer(parent: Dict[str, Any], key: str, where: str, minimum: int = 0) -> int:
    if key not in parent:
        raise ReportError(f"Report is missing '{key}' in {where}.")
    value = parent[key]
    # PROe writes counts as JSON numbers such as 20.0; a whole number is accepted, a fraction is not.
    if isinstance(value, float) and math.isfinite(value) and value.is_integer():
        value = int(value)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ReportError(f"Report field '{where}.{key}' must be an integer, got {value!r}.")
    if value < minimum:
        raise ReportError(f"Report field '{where}.{key}' is below {minimum}: {value}.")
    return value


def _require_unit(parent: Dict[str, Any], expected: str, where: str) -> None:
    unit = parent.get("unit")
    if unit != expected:
        raise ReportError(f"Report field '{where}.unit' must be {expected!r}, got {unit!r}.")
