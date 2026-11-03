# Copyright (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""
Realtime Platform — PREEMPT-RT Kernel Check.

Detects whether the running Linux kernel provides full real-time preemption
(``PREEMPT_RT``) and, optionally, whether required kernel boot parameters are
present in ``/proc/cmdline``.

Detection uses ``CONFIG_PREEMPT_RT=y`` in ``/boot/config-<release>`` — the
authoritative compile-time record on all mainstream distributions. The Ubuntu*
*lowlatency* kernel (``CONFIG_PREEMPT=y``) is **not** a real-time kernel: it
reduces average latency but gives no bounded worst-case guarantee. The kernel
preemption model (``none`` / ``voluntary`` / ``full`` / ``rt``) is captured as
a supporting metric.

Key metric: ``preempt_rt`` (1 = ready, 0 = not ready). Without
``kernel_boot_params`` in the profile it equals ``preempt_rt_kernel``. With
``kernel_boot_params``, both a PREEMPT_RT kernel and full boot parameter
compliance are required.
"""

import csv
import io
import logging

import allure
import pytest
from sysagent.utils.core import Metrics, Result

from esq.suites.realtime.platform.preempt_rt import detect_preempt_rt

logger = logging.getLogger(__name__)


def _attach_boot_params_csv(results: Result) -> None:
    """Attach a CSV summary of kernel boot parameter check results to the Allure report.

    Only generates the attachment when ``boot_params_checked`` is present in
    ``results.extended_metadata`` (i.e., when ``kernel_boot_params`` was
    configured for the test).
    """
    boot_params_checked = results.extended_metadata.get("boot_params_checked")
    if not boot_params_checked:
        return

    present_set = set(results.extended_metadata.get("boot_params_present", []))

    csv_buf = io.StringIO()
    writer = csv.writer(csv_buf)
    writer.writerow(["#", "boot_param", "status"])
    for idx, param in enumerate(boot_params_checked, start=1):
        writer.writerow([idx, param, "present" if param in present_set else "missing"])

    allure.attach(
        csv_buf.getvalue(),
        name="kernel_boot_params.csv",
        attachment_type=allure.attachment_type.CSV,
    )


@allure.title("PREEMPT-RT Kernel Check")
def test_preempt_rt(
    request,
    configs,
    cached_result,
    cache_result,
    get_kpi_config,
    validate_test_results,
    summarize_test_results,
    validate_system_requirements_from_configs,
    execute_test_with_cache,
    prepare_test,
):
    """
    Verify the running kernel provides full real-time preemption (PREEMPT_RT).

    Key metric: ``preempt_rt`` (1 = ready, 0 = not ready). Without
    ``kernel_boot_params`` it equals ``preempt_rt_kernel``; with
    ``kernel_boot_params`` it requires both a PREEMPT_RT kernel and full
    boot parameter compliance.
    """
    # ================================================================
    # STEP 1: Extract Parameters
    # ================================================================
    test_name = request.node.name.split("[")[0]
    test_id = configs.get("test_id", test_name)
    test_display_name = configs.get("display_name", test_name)

    test_description = configs.get("description")
    if test_description:
        allure.dynamic.description(test_description)

    # Qualification tests fail the run on interrupt; data-collection tests
    # surface it as a runtime error instead. This flag drives that decision.
    is_qualification = configs.get("labels", {}).get("type") == "qualification"

    logger.info(f"Starting PREEMPT-RT Kernel Check: {test_display_name}")

    # ================================================================
    # STEP 2: Validate System Requirements
    # ================================================================
    validate_system_requirements_from_configs(configs)

    # Outcome tracking — initialized before the try block so post-run
    # blocks can always reference them.
    results = None
    test_failed = False
    test_interrupted = False
    failure_message = ""

    try:
        # ============================================================
        # STEP 3: Prepare Assets/Dependencies
        # (no external assets required — skipped)
        # ============================================================

        # ============================================================
        # STEP 4: Execute Test Logic (with caching)
        # ============================================================
        def _run_detection():
            required_boot_params = configs.get("kernel_boot_params")
            is_rt, details = detect_preempt_rt(required_boot_params=required_boot_params)

            logger.info(
                f"PREEMPT-RT check: {'DETECTED' if is_rt else 'NOT DETECTED'} "
                f"(model={details['preemption_model']}, kernel={details['kernel_release']})"
            )

            # Compute boot params compliance when configured; None means not checked.
            boot_params_compliant: bool | None = None
            if "boot_params" in details:
                bp = details["boot_params"]
                boot_params_compliant = bp["compliant"]
                if bp["missing"]:
                    logger.warning(
                        f"Missing kernel boot params "
                        f"({len(bp['missing'])}/{len(required_boot_params)}): {bp['missing']}"
                    )
                logger.info(
                    f"Kernel boot params: {len(bp['present'])}/{len(required_boot_params)} present, "
                    f"compliant={boot_params_compliant}"
                )

            # Single key metric: PREEMPT-RT ready when RT kernel is confirmed
            # and (when configured) all required boot parameters are present.
            preempt_rt = is_rt and (boot_params_compliant if boot_params_compliant is not None else True)

            metrics = {
                # Single key metric — covers both check modes.
                "preempt_rt": Metrics(unit=None, value=1 if preempt_rt else 0, is_key_metric=True),
                # Supporting metrics for detailed reporting.
                # CONFIG_PREEMPT_RT=y present in /boot/config-<release>.
                "preempt_rt_kernel": Metrics(unit=None, value=1 if is_rt else 0, is_key_metric=False),
                # Kernel preemption model: "rt" | "full" | "voluntary" | "none" | "unknown".
                "preemption_model": Metrics(unit=None, value=details["preemption_model"], is_key_metric=False),
            }

            if boot_params_compliant is not None:
                metrics["kernel_boot_params"] = Metrics(
                    unit=None, value=1 if boot_params_compliant else 0, is_key_metric=False
                )

            # Store boot params check details for CSV attachment generation.
            # Persisted in extended_metadata so cached results carry the data.
            extended_metadata: dict = {}
            if "boot_params" in details:
                bp = details["boot_params"]
                extended_metadata["boot_params_checked"] = list(required_boot_params) if required_boot_params else []
                extended_metadata["boot_params_present"] = bp["present"]
                extended_metadata["boot_params_missing"] = bp["missing"]
                extended_metadata["boot_params_cmdline_available"] = bp["cmdline_available"]

            return Result(
                name=f"{test_id} - {test_display_name}",
                metrics=metrics,
                metadata={"status": preempt_rt},
                extended_metadata=extended_metadata,
            )

        results = execute_test_with_cache(
            cached_result=cached_result,
            cache_result=cache_result,
            run_test_func=_run_detection,
            test_name=test_name,
            configs=configs,
        )

    except KeyboardInterrupt:
        failure_message = "Interrupt detected during PREEMPT-RT Kernel Check"
        test_interrupted = True
        logger.error(failure_message)
    except Exception as e:
        test_failed = True
        failure_message = f"Unexpected error during PREEMPT-RT Kernel Check: {e}"
        logger.error(failure_message, exc_info=True)

    # Ensure a result object always exists so validation and summarization
    # can record the outcome even when execution was interrupted.
    if results is None:
        results = Result(
            name=f"{test_id} - {test_display_name}",
            metadata={"status": False},
            extended_metadata={"message": failure_message or "PREEMPT-RT check did not complete"},
            metrics={},
        )

    # Generate CSV attachment from results — works for both fresh execution and
    # cached results because boot params data lives in extended_metadata.
    _attach_boot_params_csv(results)

    # ================================================================
    # STEP 5: Validate Results Against KPIs (optional)
    # Only enforced when kpi_refs are configured for the test.
    # ================================================================
    try:
        validate_test_results(
            results=results,
            test_name=test_name,
            configs=configs,
            get_kpi_config=get_kpi_config,
        )
    except Exception as validation_error:
        logger.error(f"Validation failed: {validation_error}")

    # ================================================================
    # STEP 6: Generate Summary (always runs)
    # ================================================================
    try:
        summarize_test_results(
            results=results,
            test_name=test_name,
            configs=configs,
            get_kpi_config=get_kpi_config,
        )
    except Exception as summary_error:
        logger.error(f"Test result summarization failed: {summary_error}", exc_info=True)

    # Caching is handled by execute_test_with_cache: cached only when
    # preempt_rt=1 (status=True). A non-ready result is not cached so
    # the test re-runs automatically after a kernel upgrade.

    logger.info(f"PREEMPT-RT Kernel Check completed: {test_display_name}")

    # ================================================================
    # STEP 7: Surface the outcome
    # Report interrupts and failures as proper pytest results instead of
    # leaving a broken/errored status behind.
    # ================================================================
    if test_interrupted:
        if is_qualification:
            pytest.fail(failure_message)
        else:
            raise RuntimeError(failure_message)
    if test_failed:
        pytest.fail(failure_message)
