# Copyright (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""
Real-Time PROe benchmark (optional native backend).

Runs a benchmark profile of an installed, verified PROe package through the thin adapter in
``esq.utils.proe.adapter`` and maps the structured PROe report to native ESQ results. Every run
is executed fresh: results are never read from or written to the ESQ result cache, and only a
report produced by this exact run is accepted.

A completed run is *execution success*, not qualification. ESQ applies no thresholds to these
metrics; the PROe pipeline status is surfaced as information only. When PROe reports status FAIL (exit code 3),
the metrics of its report are still recorded and the test fails with the KPI rows that PROe printed as FAIL.

A profile may name an ``os_tuning_script`` that the installed PROe package provides. ESQ runs it, and attaches its
output, before every benchmark of that profile; the benchmark is not started when the script is missing or fails.
"""

import logging

import allure
import pytest
from sysagent.utils.core import Metrics, Result

from esq.utils.proe.adapter import (
    DEFAULT_TIMEOUT_S,
    BenchmarkRun,
    RunFailure,
    failed_kpi_lines,
    report_as_json,
    run_benchmark,
    run_os_tuning,
)
from esq.utils.proe.constants import KNOWN_PROFILES, ProeError

logger = logging.getLogger(__name__)


def _attach_text(text: str, name: str) -> None:
    if text:
        allure.attach(text, name=name, attachment_type=allure.attachment_type.TEXT)


def _result_from_run(test_id: str, display_name: str, run: BenchmarkRun) -> Result:
    metrics = {
        metric.name: Metrics(value=metric.value, unit=metric.unit, is_key_metric=metric.key)
        for metric in run.report.metrics
    }
    res = Result(
        name=f"{test_id} - {display_name}",
        metadata={"status": run.report.pipeline_status == "PASS"},
        extended_metadata={
            "message": f"PROe run completed; PROe pipeline status {run.report.pipeline_status}"
            + "".join(f"; {kpi}" for kpi in failed_kpi_lines(run.stdout)),
            "qualification": "not assessed: ESQ applies no approved thresholds to PROe metrics",
            "ptp_sync_status": run.report.ptp_sync_status,
            "report_schema_version": run.report.schema_version,
            "report_schema_provisional": run.report.provisional_schema,
            "report_file": run.report_path.name,
            "report_timestamp_utc": run.report.timestamp_utc,
            "command": " ".join(run.command),
            "proe_package": run.package_name,
            "proe_source_commit": run.source_commit,
        },
        metrics=metrics,
    )
    res.parameters["PROe profile"] = run.proe_profile
    res.update_timestamps()
    res.metadata["total_duration_seconds"] = round(run.duration_s, 2)
    return res


@allure.title("Real-Time - PROe benchmark")
def test_proe(
    request,
    configs,
    get_kpi_config,
    summarize_test_results,
):
    """Run one PROe benchmark profile and report its measured values."""
    test_name = request.node.name.split("[")[0]
    test_id = configs.get("test_id", test_name)
    display_name = configs.get("display_name", "PROe benchmark")

    description = configs.get("description")
    if description:
        allure.dynamic.description(description)

    esq_profile = configs.get("esq_profile")
    if esq_profile not in KNOWN_PROFILES:
        pytest.fail(f"Profile configuration does not name a supported PROe profile: {esq_profile!r}")

    timeout_s = configs.get("timeout", DEFAULT_TIMEOUT_S)
    if isinstance(timeout_s, bool) or not isinstance(timeout_s, int) or timeout_s <= 0:
        pytest.fail(f"'timeout' must be a positive number of seconds, got {timeout_s!r}")

    failure_message = ""
    evidence = {}
    run = None
    os_tuning_script = configs.get("os_tuning_script")
    with allure.step("Execute PROe benchmark (fresh run, result cache bypassed)"):
        try:
            if os_tuning_script:
                with allure.step("Apply host OS tuning"):
                    _attach_text(run_os_tuning(os_tuning_script), "proe-os-tuning.txt")
            run = run_benchmark(esq_profile, timeout_s=timeout_s)
        except RunFailure as error:
            failure_message = str(error)
            evidence = error.evidence
        except ProeError as error:
            failure_message = str(error)

    if run is not None:
        _attach_text(run.stdout, "proe-stdout.txt")
        _attach_text(run.stderr, "proe-stderr.txt")
        allure.attach(report_as_json(run), name=run.report_path.name, attachment_type=allure.attachment_type.JSON)
        result = _result_from_run(test_id, display_name, run)
        if not result.metadata["status"]:
            kpis = failed_kpi_lines(run.stdout)
            failure_message = f"PROe reported pipeline status {run.report.pipeline_status}" + (
                f": {'; '.join(kpis)}" if kpis else ""
            )
    else:
        for key, value in evidence.items():
            _attach_text(value, f"proe-{key}.txt")
        result = Result(
            name=f"{test_id} - {display_name}",
            metadata={"status": False},
            extended_metadata={"message": failure_message},
            metrics={},
        )
        result.update_timestamps()

    summarize_test_results(
        results=result,
        test_name=test_name,
        configs=configs,
        get_kpi_config=get_kpi_config,
    )

    if failure_message:
        pytest.fail(failure_message)
