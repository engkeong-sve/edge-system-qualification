# Copyright (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Run an explicitly selected PROe single-host profile through ESQ."""

import logging

import allure
import pytest
from sysagent.utils.core import Metrics, Result

from esq.suites.realtime.proe.runner import run_benchmark

logger = logging.getLogger(__name__)


@allure.title("PROe single-host benchmark")
def test_proe_single_host(
    request,
    configs,
    get_kpi_config,
    validate_test_results,
    summarize_test_results,
    validate_system_requirements_from_configs,
):
    if configs.get("proe_enabled") is not True:
        pytest.skip("PROe execution is opt-in; select an example PROe profile to enable it.")

    test_name = request.node.name.split("[")[0]
    test_id = configs.get("test_id", test_name)
    display_name = configs.get("display_name", "PROe single-host benchmark")
    description = configs.get("description")
    if description:
        allure.dynamic.description(description)

    assessment = None
    failure_reason = ""
    try:
        validate_system_requirements_from_configs(configs)
        assessment = run_benchmark(configs)
        failure_reason = "; ".join(assessment.errors)
    except (OSError, RuntimeError, ValueError) as error:
        failure_reason = str(error)

    passed = assessment is not None and assessment.passed
    metrics = assessment.metrics if assessment is not None else {}
    results = Result(
        name=f"{test_id} - {display_name}",
        metadata={"status": passed},
        extended_metadata={
            "qualification_status": "PASS" if passed else "INCOMPLETE_OR_FAIL",
            "errors": [failure_reason] if failure_reason else [],
            "evidence_source": "invocation-bound PROe manifest and report",
        },
        metrics={
            "proe_qualification_pass": Metrics(value=1 if passed else 0, is_key_metric=True),
            "max_rtt_us": Metrics(value=metrics.get("max_rtt_us"), unit="us"),
            "max_processing_time_us": Metrics(value=metrics.get("max_processing_time_us"), unit="us"),
            "max_rx_workload_us": Metrics(value=metrics.get("max_rx_workload_us"), unit="us"),
            "clock_offset_mean_ns": Metrics(value=metrics.get("clock_offset_mean_ns"), unit="ns"),
            "rtt_outliers": Metrics(value=metrics.get("rtt_outliers"), unit="packets"),
            "processing_time_outliers": Metrics(value=metrics.get("processing_time_outliers"), unit="packets"),
            "rx_workload_outliers": Metrics(value=metrics.get("rx_workload_outliers"), unit="packets"),
            "reference_duration_s": Metrics(value=metrics.get("reference_duration_s"), unit="s"),
            "mirror_duration_s": Metrics(value=metrics.get("mirror_duration_s"), unit="s"),
            "ai_workload_duration_s": Metrics(value=metrics.get("ai_workload_duration_s"), unit="s"),
            "reference_sent_packets": Metrics(value=metrics.get("reference_sent_packets"), unit="packets"),
            "reference_received_packets": Metrics(value=metrics.get("reference_received_packets"), unit="packets"),
            "mirror_sent_packets": Metrics(value=metrics.get("mirror_sent_packets"), unit="packets"),
            "mirror_received_packets": Metrics(value=metrics.get("mirror_received_packets"), unit="packets"),
        },
    )
    results.update_timestamps()

    try:
        validate_test_results(
            results=results,
            test_name=test_name,
            configs=configs,
            get_kpi_config=get_kpi_config,
        )
    finally:
        summarize_test_results(
            results=results,
            test_name=test_name,
            configs=configs,
            get_kpi_config=get_kpi_config,
        )

    if not passed:
        pytest.fail(failure_reason or "PROe run evidence was incomplete or failed qualification.", pytrace=False)
