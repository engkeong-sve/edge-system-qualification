# Copyright (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Fail-closed invocation and evidence validation for single-host PROe runs."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import yaml

_ROLES = ("reference", "mirror")
_WORKLOADS = ("resnet50",)
_MAX_TIMEOUT_S = 7 * 24 * 60 * 60
_SAFE_INTERFACE = re.compile(r"^[A-Za-z0-9_.-]+$")


class ProeContractError(ValueError):
    """Raised when a configured PROe input does not match the ESQ contract."""


@dataclass(frozen=True)
class ProfileBinding:
    path: Path
    profile_id: str
    duration_s: int
    sha256: str
    tcc_enabled: bool
    tcc_file: str | None
    tcc_sha256: str | None
    tsn_files: dict[str, str]
    tsn_hashes: dict[str, str]
    interfaces: dict[str, str]


@dataclass(frozen=True)
class QualificationThresholds:
    max_rtt_us: float
    max_processing_time_us: float
    max_rx_workload_us: float
    max_clock_offset_ns: float
    max_rtt_outliers: int
    max_processing_time_outliers: int
    max_rx_workload_outliers: int
    minimum_packets_per_role: int
    process_timeout_s: float


@dataclass
class EvidenceAssessment:
    metrics: dict[str, float | int | None]
    errors: list[str]

    @property
    def passed(self) -> bool:
        return not self.errors


def _finite_number(value: Any, name: str, *, minimum: float = 0.0) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ProeContractError(f"{name} must be a finite number.")
    number = float(value)
    if not math.isfinite(number) or number < minimum:
        raise ProeContractError(f"{name} must be finite and at least {minimum}.")
    return number


def _integer(value: Any, name: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ProeContractError(f"{name} must be an integer of at least {minimum}.")
    return value


def load_thresholds(config: dict[str, Any], duration_s: int) -> QualificationThresholds:
    raw = config.get("thresholds")
    if not isinstance(raw, dict):
        raise ProeContractError("Explicit PROe qualification thresholds are required.")

    thresholds = QualificationThresholds(
        max_rtt_us=_finite_number(raw.get("max_rtt_us"), "thresholds.max_rtt_us"),
        max_processing_time_us=_finite_number(raw.get("max_processing_time_us"), "thresholds.max_processing_time_us"),
        max_rx_workload_us=_finite_number(raw.get("max_rx_workload_us"), "thresholds.max_rx_workload_us"),
        max_clock_offset_ns=_finite_number(raw.get("max_clock_offset_ns"), "thresholds.max_clock_offset_ns"),
        max_rtt_outliers=_integer(raw.get("max_rtt_outliers"), "thresholds.max_rtt_outliers"),
        max_processing_time_outliers=_integer(
            raw.get("max_processing_time_outliers"), "thresholds.max_processing_time_outliers"
        ),
        max_rx_workload_outliers=_integer(raw.get("max_rx_workload_outliers"), "thresholds.max_rx_workload_outliers"),
        minimum_packets_per_role=_integer(
            raw.get("minimum_packets_per_role"), "thresholds.minimum_packets_per_role", minimum=1
        ),
        process_timeout_s=_finite_number(config.get("process_timeout_s"), "process_timeout_s", minimum=1.0),
    )
    if thresholds.process_timeout_s <= duration_s or thresholds.process_timeout_s > _MAX_TIMEOUT_S:
        raise ProeContractError(
            f"process_timeout_s must exceed the requested {duration_s}s run and not exceed seven days."
        )
    return thresholds


def _mapping(value: Any, name: str, errors: list[str]) -> dict[str, Any]:
    if not isinstance(value, dict):
        errors.append(f"{name} is missing or is not an object.")
        return {}
    return value


def _required(
    value: Any,
    name: str,
    errors: list[str],
    *,
    expected_type: type | tuple[type, ...] | None = None,
) -> Any:
    if value is None or (expected_type is not None and not isinstance(value, expected_type)):
        errors.append(f"{name} is missing or has an invalid type.")
        return None
    return value


def _manifest_errors(value: Any, errors: list[str]) -> list[str]:
    if not isinstance(value, list):
        errors.append("manifest.errors is missing or is not a list.")
        return []
    if any(not isinstance(item, str) for item in value):
        errors.append("manifest.errors contains an invalid entry.")
        return []
    if value:
        errors.append("PROe reported one or more run errors.")
    return value


def _json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ProeContractError("JSON evidence contains a duplicate object key.")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise ProeContractError(f"JSON evidence contains the invalid numeric constant {value}.")


def load_json_file(path: Path) -> dict[str, Any]:
    try:
        if path.stat().st_size > 32 * 1024 * 1024:
            raise ProeContractError("JSON evidence exceeds the 32 MiB safety limit.")
        value = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_json_object,
            parse_constant=_reject_json_constant,
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ProeContractError("A required PROe JSON evidence file is missing or invalid.") from error
    if not isinstance(value, dict):
        raise ProeContractError("A PROe JSON evidence root must be an object.")
    return value


def _profile_file_binding(profile_path: Path, raw_value: Any, name: str) -> tuple[str, str]:
    if not isinstance(raw_value, str) or not raw_value.strip():
        raise ProeContractError(f"{name} must name a configuration file.")
    candidate = Path(raw_value)
    resolved = candidate if candidate.is_absolute() else profile_path.parent / candidate
    try:
        content = resolved.read_bytes()
    except OSError as error:
        raise ProeContractError(f"{name} does not name a readable configuration file.") from error
    return raw_value, hashlib.sha256(content).hexdigest()


def load_profile_binding(path_value: Any, expected_tcc_enabled: Any) -> ProfileBinding:
    if not isinstance(path_value, str) or not path_value.strip():
        raise ProeContractError("profile_file must name the operator's PROe YAML profile.")
    path = Path(path_value)
    if not path.is_absolute():
        raise ProeContractError("profile_file must be an absolute path.")
    if not path.is_file():
        raise ProeContractError("profile_file is not a readable file.")
    if not isinstance(expected_tcc_enabled, bool):
        raise ProeContractError("expected_tcc_enabled must be explicitly true or false.")

    try:
        raw = path.read_bytes()
        document = yaml.safe_load(raw.decode("utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as error:
        raise ProeContractError("profile_file could not be read as valid YAML.") from error
    if not isinstance(document, dict):
        raise ProeContractError("The PROe profile root must be a YAML object.")

    profile = document.get("profile")
    if not isinstance(profile, dict):
        raise ProeContractError("The PROe profile section is missing.")
    profile_id = profile.get("profile_id")
    if not isinstance(profile_id, str) or not profile_id.strip():
        raise ProeContractError("profile.profile_id is required.")
    duration_s = _integer(profile.get("test_duration"), "profile.test_duration", minimum=1)

    services = document.get("services")
    if not isinstance(services, dict):
        raise ProeContractError("The PROe services section is missing.")
    ai_workload = services.get("ai_workload")
    if not isinstance(ai_workload, dict) or ai_workload.get("workload_list") != list(_WORKLOADS):
        raise ProeContractError("services.ai_workload.workload_list must contain only resnet50.")

    tcc = services.get("tcc")
    if not isinstance(tcc, dict) or not isinstance(tcc.get("enabled"), bool):
        raise ProeContractError("services.tcc.enabled must be explicitly true or false.")
    tcc_enabled = tcc["enabled"]
    if tcc_enabled is not expected_tcc_enabled:
        raise ProeContractError("The selected PROe profile TCC mode does not match the ESQ profile.")
    tcc_file = tcc.get("config_file")
    tcc_hash = None
    if tcc_enabled:
        tcc_file, tcc_hash = _profile_file_binding(path, tcc_file, "services.tcc.config_file")
    elif tcc_file not in (None, ""):
        raise ProeContractError("A non-TCC PROe profile must not select a TCC configuration file.")

    tsn = services.get("tsn")
    nic_setup = document.get("nic_setup")
    if not isinstance(tsn, dict) or not isinstance(nic_setup, dict):
        raise ProeContractError("The PROe TSN and NIC setup sections are required.")
    tsn_files: dict[str, str] = {}
    tsn_hashes: dict[str, str] = {}
    interfaces: dict[str, str] = {}
    for role in _ROLES:
        role_tsn = tsn.get(role)
        role_nic = nic_setup.get(role)
        if not isinstance(role_tsn, dict) or not isinstance(role_nic, dict):
            raise ProeContractError(f"PROe {role} configuration is missing.")
        tsn_files[role], tsn_hashes[role] = _profile_file_binding(
            path, role_tsn.get("config_file"), f"services.tsn.{role}.config_file"
        )
        interface = role_nic.get("ptp_interface")
        if not isinstance(interface, str) or not _SAFE_INTERFACE.fullmatch(interface):
            raise ProeContractError(f"nic_setup.{role}.ptp_interface is missing or invalid.")
        interfaces[role] = interface
    if interfaces["reference"] == interfaces["mirror"]:
        raise ProeContractError("The reference and mirror roles must use distinct network interfaces.")

    return ProfileBinding(
        path=path,
        profile_id=profile_id,
        duration_s=duration_s,
        sha256=hashlib.sha256(raw).hexdigest(),
        tcc_enabled=tcc_enabled,
        tcc_file=tcc_file,
        tcc_sha256=tcc_hash,
        tsn_files=tsn_files,
        tsn_hashes=tsn_hashes,
        interfaces=interfaces,
    )


def _check_bool(value: Any, expected: bool, name: str, errors: list[str]) -> None:
    if value is not expected:
        errors.append(f"{name} does not match the required run state.")


def _read_number(
    value: Any,
    name: str,
    errors: list[str],
    *,
    minimum: float = 0.0,
) -> float | None:
    try:
        return _finite_number(value, name, minimum=minimum)
    except ProeContractError:
        errors.append(f"{name} is missing, non-finite, or outside its valid range.")
        return None


def _read_count(value: Any, name: str, errors: list[str]) -> int | None:
    try:
        return _integer(value, name)
    except ProeContractError:
        errors.append(f"{name} is missing or is not a non-negative integer.")
        return None


def _validate_duration(value: Any, name: str, requested_duration_s: int, errors: list[str]) -> float | None:
    duration = _read_number(value, name, errors, minimum=0.0)
    if duration is not None and duration < requested_duration_s:
        errors.append(f"{name} is shorter than the requested run.")
    return duration


def _profile_configuration_matches(report: dict[str, Any], binding: ProfileBinding, errors: list[str]) -> None:
    configuration = _mapping(report.get("configuration"), "configuration", errors)
    if configuration.get("profile_sha256") != binding.sha256:
        errors.append("Report profile hash does not match the invoked YAML bytes.")
    if configuration.get("topology") != "single-host":
        errors.append("Report topology is not single-host.")
    if configuration.get("ai_workloads") != list(_WORKLOADS):
        errors.append("Report AI workload selection does not match resnet50.")

    tcc = _mapping(configuration.get("tcc"), "configuration.tcc", errors)
    _check_bool(tcc.get("enabled"), binding.tcc_enabled, "configuration.tcc.enabled", errors)
    if binding.tcc_enabled:
        if tcc.get("config_file") != binding.tcc_file or tcc.get("sha256") != binding.tcc_sha256:
            errors.append("Report TCC configuration does not match the selected profile file.")
    elif tcc.get("config_file") is not None or tcc.get("sha256") is not None:
        errors.append("A non-TCC run reports an unexpected TCC configuration.")

    tsn = _mapping(configuration.get("tsn"), "configuration.tsn", errors)
    gcl = _mapping(configuration.get("tsn_gcl"), "configuration.tsn_gcl", errors)
    runtime = _mapping(configuration.get("tsn_runtime"), "configuration.tsn_runtime", errors)
    for role in _ROLES:
        selected = _mapping(tsn.get(role), f"configuration.tsn.{role}", errors)
        if selected.get("config_file") != binding.tsn_files[role]:
            errors.append(f"Report {role} TSN file does not match the selected profile file.")
        if selected.get("sha256") != binding.tsn_hashes[role]:
            errors.append(f"Report {role} TSN hash does not match the selected file bytes.")

        schedule = _mapping(gcl.get(role), f"configuration.tsn_gcl.{role}", errors)
        if schedule.get("interface") != binding.interfaces[role]:
            errors.append(f"Report {role} parsed GCL interface does not match the selected interface.")
        if not isinstance(schedule.get("applied"), bool):
            errors.append(f"Report {role} parsed GCL applied field is missing.")
        _read_number(schedule.get("base_time"), f"configuration.tsn_gcl.{role}.base_time", errors)
        if not isinstance(schedule.get("entries"), list) or not schedule["entries"]:
            errors.append(f"Report {role} parsed GCL entries are missing.")

        live = _mapping(runtime.get(role), f"configuration.tsn_runtime.{role}", errors)
        if live.get("interface") != binding.interfaces[role]:
            errors.append(f"Report {role} runtime interface does not match the selected interface.")
        qdiscs = live.get("qdisc")
        if not isinstance(qdiscs, list) or not any(
            isinstance(qdisc, dict) and qdisc.get("kind") == "taprio" for qdisc in qdiscs
        ):
            errors.append(f"Report {role} runtime qdisc does not contain taprio.")


def _validate_packet_snapshot(
    results_packet: Any,
    execution_packet: Any,
    thresholds: QualificationThresholds,
    metrics: dict[str, float | int | None],
    errors: list[str],
) -> None:
    result_data = _mapping(results_packet, "results.packet_integrity", errors)
    execution_data = _mapping(execution_packet, "execution.packet_counters", errors)
    for field in ("scope", "consistent", "final_snapshot_verified", "complete", "errors", "roles"):
        if result_data.get(field) != execution_data.get(field):
            errors.append(f"Packet counter copies disagree on {field}.")
    _check_bool(result_data.get("consistent"), True, "results.packet_integrity.consistent", errors)
    _check_bool(
        result_data.get("final_snapshot_verified"),
        True,
        "results.packet_integrity.final_snapshot_verified",
        errors,
    )
    _check_bool(result_data.get("complete"), True, "results.packet_integrity.complete", errors)
    _manifest_errors(result_data.get("errors"), errors)

    roles = _mapping(result_data.get("roles"), "results.packet_integrity.roles", errors)
    for role in _ROLES:
        role_data = _mapping(roles.get(role), f"results.packet_integrity.roles.{role}", errors)
        sent = _read_count(role_data.get("sent"), f"packet_integrity.{role}.sent", errors)
        received = _read_count(role_data.get("received"), f"packet_integrity.{role}.received", errors)
        if sent is not None and sent < thresholds.minimum_packets_per_role:
            errors.append(f"Packet count for {role} sent is below the configured minimum.")
        if received is not None and received < thresholds.minimum_packets_per_role:
            errors.append(f"Packet count for {role} received is below the configured minimum.")
        metrics[f"{role}_sent_packets"] = sent
        metrics[f"{role}_received_packets"] = received


def assess_run(
    manifest: dict[str, Any],
    report: dict[str, Any] | None,
    binding: ProfileBinding,
    invocation_id: str,
    thresholds: QualificationThresholds,
    *,
    process_returncode: int,
) -> EvidenceAssessment:
    errors: list[str] = []
    metrics: dict[str, float | int | None] = {
        "max_rtt_us": None,
        "max_processing_time_us": None,
        "max_rx_workload_us": None,
        "clock_offset_mean_ns": None,
        "rtt_outliers": None,
        "processing_time_outliers": None,
        "rx_workload_outliers": None,
        "reference_duration_s": None,
        "mirror_duration_s": None,
        "ai_workload_duration_s": None,
        "reference_sent_packets": None,
        "reference_received_packets": None,
        "mirror_sent_packets": None,
        "mirror_received_packets": None,
    }

    if manifest.get("schema_version") != "1.0":
        errors.append("Manifest schema version is not 1.0.")
    if manifest.get("invocation_id") != invocation_id:
        errors.append("Manifest invocation ID does not match this run.")
    if manifest.get("profile_id") != binding.profile_id:
        errors.append("Manifest profile ID does not match the selected YAML.")
    if manifest.get("profile_sha256") != binding.sha256:
        errors.append("Manifest profile hash does not match the invoked YAML bytes.")
    _check_bool(manifest.get("success"), True, "manifest.success", errors)
    _manifest_errors(manifest.get("errors"), errors)

    status = manifest.get("status")
    if status != "PASS":
        errors.append(f"PROe run status is {status if isinstance(status, str) else 'invalid'}, not PASS.")
    if process_returncode != 0:
        errors.append(f"PROe CLI returned non-zero exit status {process_returncode}.")
    if report is None:
        errors.append("A complete PROe report was not available.")
        return EvidenceAssessment(metrics, errors)

    if report.get("schema_version") != "1.1":
        errors.append("Report schema version is not 1.1.")
    _profile_configuration_matches(report, binding, errors)

    execution = _mapping(report.get("execution"), "execution", errors)
    if execution.get("invocation_id") != invocation_id:
        errors.append("Report invocation ID does not match this run.")
    if execution.get("requested_duration_s") != binding.duration_s:
        errors.append("Report duration does not match profile.test_duration.")
    _check_bool(execution.get("completed"), True, "execution.completed", errors)

    role_data = _mapping(execution.get("roles"), "execution.roles", errors)
    for role in _ROLES:
        item = _mapping(role_data.get(role), f"execution.roles.{role}", errors)
        _check_bool(item.get("completed"), True, f"execution.roles.{role}.completed", errors)
        if item.get("run_error") not in (None, ""):
            errors.append(f"PROe reported a {role} execution error.")
        metrics[f"{role}_duration_s"] = _validate_duration(
            item.get("actual_duration_s"),
            f"execution.roles.{role}.actual_duration_s",
            binding.duration_s,
            errors,
        )

    ai = _mapping(execution.get("ai_workload"), "execution.ai_workload", errors)
    if ai.get("workloads") != list(_WORKLOADS):
        errors.append("Executed AI workload list does not match resnet50.")
    _check_bool(ai.get("completed"), True, "execution.ai_workload.completed", errors)
    if ai.get("run_error") not in (None, ""):
        errors.append("PROe reported an AI workload execution error.")
    if ai.get("run_mode") != "single-benchmark-app":
        errors.append("AI workload execution mode is not the verified single benchmark-app mode.")
    requested_runs = _read_count(ai.get("requested_runs"), "execution.ai_workload.requested_runs", errors)
    completed_runs = _read_count(ai.get("completed_runs"), "execution.ai_workload.completed_runs", errors)
    if requested_runs is not None and requested_runs < 1:
        errors.append("The AI workload requested no runs.")
    if completed_runs is not None and (requested_runs is None or completed_runs != requested_runs):
        errors.append("AI workload completed-run count does not match the request.")
    metrics["ai_workload_duration_s"] = _validate_duration(
        ai.get("actual_duration_s"),
        "execution.ai_workload.actual_duration_s",
        binding.duration_s,
        errors,
    )

    results = _mapping(report.get("results"), "results", errors)
    _validate_packet_snapshot(
        results.get("packet_integrity"),
        execution.get("packet_counters"),
        thresholds,
        metrics,
        errors,
    )

    performance = _mapping(results.get("rt_performance"), "results.rt_performance", errors)
    metric_fields = (
        ("rtt", "max_rtt_us", "max_rtt_us", "rtt_outliers", "max_rtt_outliers"),
        (
            "processing_time",
            "max_processing_time_us",
            "max_processing_time_us",
            "processing_time_outliers",
            "max_processing_time_outliers",
        ),
        (
            "rx_workload",
            "max_rx_workload_us",
            "max_rx_workload_us",
            "rx_workload_outliers",
            "max_rx_workload_outliers",
        ),
    )
    for data_key, max_metric, threshold_key, outlier_metric, outlier_threshold_key in metric_fields:
        item = _mapping(performance.get(data_key), f"results.rt_performance.{data_key}", errors)
        latency = _mapping(item.get("latency"), f"results.rt_performance.{data_key}.latency", errors)
        if latency.get("unit") != "us":
            errors.append(f"{data_key} latency unit is not us.")
        maximum = _read_number(latency.get("maximum"), f"{data_key}.latency.maximum", errors)
        outliers = _read_count(item.get("outliers"), f"{data_key}.outliers", errors)
        metrics[max_metric] = maximum
        metrics[outlier_metric] = outliers
        if maximum is not None and maximum > getattr(thresholds, threshold_key):
            errors.append(f"{data_key} maximum exceeds its configured threshold.")
        if outliers is not None and outliers > getattr(thresholds, outlier_threshold_key):
            errors.append(f"{data_key} outlier count exceeds its configured threshold.")

    clock_sync = _mapping(results.get("clock_sync"), "results.clock_sync", errors)
    offset = _mapping(clock_sync.get("offset"), "results.clock_sync.offset", errors)
    if offset.get("unit") != "ns":
        errors.append("Clock offset unit is not ns.")
    offset_mean = _read_number(offset.get("mean"), "clock_sync.offset.mean", errors, minimum=-math.inf)
    metrics["clock_offset_mean_ns"] = offset_mean
    if offset_mean is not None and abs(offset_mean) > thresholds.max_clock_offset_ns:
        errors.append("Clock offset mean exceeds its configured threshold.")
    if not isinstance(clock_sync.get("ptp_sync_status"), str) or not clock_sync["ptp_sync_status"].strip():
        errors.append("Clock synchronization status is missing.")
    sample_count = _read_count(clock_sync.get("sample_count"), "clock_sync.sample_count", errors)
    if sample_count is not None and sample_count == 0:
        errors.append("Clock synchronization has no samples.")

    return EvidenceAssessment(metrics, errors)


def _ubuntu_pro_rt_entitlement(command_runner: Callable[..., Any] | None = None) -> tuple[bool, str]:
    if command_runner is None:
        from sysagent.utils.core import run_command

        command_runner = run_command
    try:
        result = command_runner(["pro", "status", "--format", "json"], timeout=20)
    except (OSError, RuntimeError):
        return False, "Canonical Ubuntu Pro status could not be read."
    if getattr(result, "returncode", None) != 0:
        return False, "Canonical Ubuntu Pro status command failed."
    try:
        status = json.loads(result.stdout, object_pairs_hook=_json_object, parse_constant=_reject_json_constant)
    except (TypeError, json.JSONDecodeError, ProeContractError):
        return False, "Canonical Ubuntu Pro status was not valid JSON."
    if not isinstance(status, dict) or status.get("attached") is not True:
        return False, "Ubuntu Pro is not confirmed as attached."
    services = status.get("services")
    if not isinstance(services, list):
        return False, "Ubuntu Pro realtime-kernel entitlement is not confirmed."
    realtime = next(
        (item for item in services if isinstance(item, dict) and item.get("name") == "realtime-kernel"),
        None,
    )
    if not isinstance(realtime, dict) or realtime.get("entitled") is not True:
        return False, "Ubuntu Pro realtime-kernel entitlement is not confirmed."
    if realtime.get("status") != "enabled":
        return False, "Ubuntu Pro realtime-kernel service is not enabled."
    return True, ""


def check_host_readiness(
    binding: ProfileBinding,
    *,
    command_runner: Callable[..., Any] | None = None,
    preempt_rt_detector: Callable[[], tuple[bool, dict[str, Any]]] | None = None,
    os_release: dict[str, str] | None = None,
    sys_root: Path = Path("/sys"),
) -> list[str]:
    blockers: list[str] = []
    if os_release is None:
        os_release = {}
        try:
            for line in Path("/etc/os-release").read_text(encoding="utf-8").splitlines():
                key, separator, value = line.partition("=")
                if separator:
                    os_release[key] = value.strip().strip('"')
        except OSError:
            pass
    if os_release.get("ID") != "ubuntu" or os_release.get("VERSION_ID") != "24.04":
        blockers.append("The host is not Canonical Ubuntu 24.04.")

    if preempt_rt_detector is None:
        from esq.suites.realtime.platform.preempt_rt import detect_preempt_rt

        preempt_rt_detector = detect_preempt_rt
    try:
        is_rt, _ = preempt_rt_detector()
    except (OSError, RuntimeError, ValueError):
        is_rt = False
    if not is_rt:
        blockers.append("The active kernel does not confirm CONFIG_PREEMPT_RT=y.")

    entitled, entitlement_blocker = _ubuntu_pro_rt_entitlement(command_runner)
    if not entitled:
        blockers.append(entitlement_blocker)

    for role in _ROLES:
        interface = binding.interfaces[role]
        interface_path = sys_root / "class" / "net" / interface
        try:
            driver = (interface_path / "device" / "driver").resolve(strict=True).name
            carrier = (interface_path / "carrier").read_text(encoding="ascii").strip()
            operstate = (interface_path / "operstate").read_text(encoding="ascii").strip()
        except OSError:
            blockers.append(f"The {role} interface is unavailable or has no readable physical link.")
            continue
        if driver != "igc":
            blockers.append(f"The {role} interface is not using the igc network driver.")
        if carrier != "1" or operstate != "up":
            blockers.append(f"The {role} interface does not have an active link.")
    return blockers


def _resolve_proe_executable(value: Any) -> str:
    if not isinstance(value, str) or not value.strip() or any(char.isspace() for char in value):
        raise ProeContractError("proe_executable must name one executable, not a command line.")
    candidate = shutil.which(value)
    if candidate is None:
        path = Path(value)
        if not path.is_absolute() or not path.is_file() or not os.access(path, os.X_OK):
            raise ProeContractError("The configured PROe executable is unavailable.")
        candidate = str(path)
    return candidate


def _write_run_logs(run_dir: Path) -> tuple[Any, Any]:
    run_dir.mkdir(parents=True, mode=0o700, exist_ok=False)
    if os.name != "nt":
        os.chmod(run_dir, 0o700)
    return (
        (run_dir / "stdout.log").open("w", encoding="utf-8"),
        (run_dir / "stderr.log").open("w", encoding="utf-8"),
    )


def run_benchmark(config: dict[str, Any]) -> EvidenceAssessment:
    try:
        binding = load_profile_binding(config.get("profile_file"), config.get("expected_tcc_enabled"))
        thresholds = load_thresholds(config, binding.duration_s)
        blockers = check_host_readiness(binding)
        if blockers:
            return EvidenceAssessment({}, blockers)
        executable = _resolve_proe_executable(config.get("proe_executable", "proe"))
    except ProeContractError as error:
        return EvidenceAssessment({}, [str(error)])

    invocation_id = str(uuid.uuid4())
    workspace = Path.cwd().resolve()
    run_root = Path(os.environ.get("CORE_DATA_DIR", workspace / "esq_data")).resolve()
    if not run_root.is_relative_to(workspace):
        run_root = workspace / "esq_data"
    run_root = run_root / "data" / "suites" / "realtime" / "proe" / "runs"
    run_dir = run_root / invocation_id
    try:
        stdout_file, stderr_file = _write_run_logs(run_dir)
    except OSError:
        return EvidenceAssessment({}, ["The local PROe run directory could not be created."])

    manifest_path = run_dir / "result-manifest.json"
    command = [
        executable,
        "profile",
        "run-benchmark",
        "--file",
        str(binding.path),
        "--topology",
        "single-host",
        "--result-manifest",
        str(manifest_path),
        "--invocation-id",
        invocation_id,
    ]
    errors: list[str] = []
    process_returncode = -1
    handle = None
    try:
        from sysagent.utils.core import start_process

        handle = start_process(command, stdout=stdout_file, stderr=stderr_file)
        process_returncode = handle.wait(timeout=thresholds.process_timeout_s)
        if process_returncode == -1 and handle.poll() is None:
            handle.terminate()
            errors.append("PROe exceeded the configured process timeout and was stopped.")
    except KeyboardInterrupt:
        if handle is not None:
            handle.terminate()
        raise
    except (OSError, RuntimeError, ValueError):
        errors.append("The PROe command could not be started or monitored.")
    finally:
        stdout_file.close()
        stderr_file.close()

    try:
        manifest = load_json_file(manifest_path)
    except ProeContractError:
        return EvidenceAssessment({}, errors + ["PROe did not write a valid result manifest."])

    report: dict[str, Any] | None = None
    report_path_value = manifest.get("report_path")
    if manifest.get("status") == "PASS":
        if not isinstance(report_path_value, str) or not Path(report_path_value).is_absolute():
            errors.append("A PASS manifest did not contain an absolute report path.")
        else:
            try:
                report = load_json_file(Path(report_path_value))
            except ProeContractError:
                errors.append("The report referenced by the manifest is missing or invalid.")

    assessment = assess_run(
        manifest,
        report,
        binding,
        invocation_id,
        thresholds,
        process_returncode=process_returncode,
    )
    assessment.errors = errors + assessment.errors
    return assessment
