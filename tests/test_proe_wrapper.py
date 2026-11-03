# Copyright (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

import hashlib
from pathlib import Path

import pytest

from esq.suites.realtime.proe.runner import (
    ProeContractError,
    QualificationThresholds,
    assess_run,
    load_profile_binding,
    load_thresholds,
)


def _write_profile(tmp_path: Path, *, tcc_enabled: bool = True) -> Path:
    (tmp_path / "tsn-reference.xml").write_text("<gcl/>", encoding="utf-8")
    (tmp_path / "tsn-mirror.xml").write_text("<gcl/>", encoding="utf-8")
    (tmp_path / "tcc.xml").write_text("<tcc/>", encoding="utf-8")
    tcc_config = '    config_file: "tcc.xml"\n' if tcc_enabled else ""
    profile = f"""profile:
  profile_id: "synthetic-profile"
  test_duration: 60
services:
  ai_workload:
    workload_list: ["resnet50"]
  tcc:
    enabled: {str(tcc_enabled).lower()}
{tcc_config}  tsn:
    reference:
      config_file: "tsn-reference.xml"
    mirror:
      config_file: "tsn-mirror.xml"
nic_setup:
  reference:
    ptp_interface: "eth-test0"
  mirror:
    ptp_interface: "eth-test1"
"""
    path = tmp_path / "profile.yml"
    path.write_text(profile, encoding="utf-8")
    return path


def _thresholds() -> QualificationThresholds:
    return QualificationThresholds(
        max_rtt_us=10.0,
        max_processing_time_us=20.0,
        max_rx_workload_us=30.0,
        max_clock_offset_ns=100.0,
        max_rtt_outliers=0,
        max_processing_time_outliers=0,
        max_rx_workload_outliers=0,
        minimum_packets_per_role=1,
        process_timeout_s=120.0,
    )


def _valid_evidence(binding, invocation_id: str):
    roles = {
        "reference": {"sent": 100, "received": 100},
        "mirror": {"sent": 100, "received": 100},
    }
    counters = {
        "roles": roles,
        "scope": "verified-final-snapshot",
        "consistent": True,
        "final_snapshot_verified": True,
        "complete": True,
        "errors": [],
    }
    report = {
        "schema_version": "1.1",
        "configuration": {
            "profile_sha256": binding.sha256,
            "topology": "single-host",
            "ai_workloads": ["resnet50"],
            "tcc": {
                "enabled": binding.tcc_enabled,
                "config_file": binding.tcc_file,
                "sha256": binding.tcc_sha256,
            },
            "tsn": {
                role: {"config_file": binding.tsn_files[role], "sha256": binding.tsn_hashes[role]}
                for role in ("reference", "mirror")
            },
            "tsn_gcl": {
                role: {
                    "applied": True,
                    "base_time": 1,
                    "interface": binding.interfaces[role],
                    "entries": [{"gate_mask": "0x1"}],
                }
                for role in ("reference", "mirror")
            },
            "tsn_runtime": {
                role: {"interface": binding.interfaces[role], "qdisc": [{"kind": "taprio"}]}
                for role in ("reference", "mirror")
            },
        },
        "execution": {
            "invocation_id": invocation_id,
            "requested_duration_s": binding.duration_s,
            "completed": True,
            "roles": {
                role: {"actual_duration_s": 60.0, "completed": True, "run_error": None}
                for role in ("reference", "mirror")
            },
            "ai_workload": {
                "workloads": ["resnet50"],
                "completed": True,
                "actual_duration_s": 60.0,
                "run_error": None,
                "completed_runs": 1,
                "requested_runs": 1,
                "run_mode": "single-benchmark-app",
            },
            "packet_counters": counters,
        },
        "results": {
            "packet_integrity": counters,
            "rt_performance": {
                "rtt": {"latency": {"maximum": 8.0, "unit": "us"}, "outliers": 0},
                "processing_time": {"latency": {"maximum": 18.0, "unit": "us"}, "outliers": 0},
                "rx_workload": {"latency": {"maximum": 25.0, "unit": "us"}, "outliers": 0},
            },
            "clock_sync": {
                "offset": {"mean": 20.0, "unit": "ns"},
                "ptp_sync_status": "synchronized",
                "sample_count": 10,
            },
        },
    }
    manifest = {
        "schema_version": "1.0",
        "invocation_id": invocation_id,
        "profile_id": binding.profile_id,
        "profile_sha256": binding.sha256,
        "success": True,
        "status": "PASS",
        "report_path": str(Path("report.json").resolve()),
        "errors": [],
    }
    return manifest, report


def test_profile_binding_checks_hashes_and_tcc_mode(tmp_path):
    path = _write_profile(tmp_path, tcc_enabled=False)
    binding = load_profile_binding(str(path), False)

    assert binding.profile_id == "synthetic-profile"
    assert binding.duration_s == 60
    assert binding.sha256 == hashlib.sha256(path.read_bytes()).hexdigest()
    assert binding.tcc_sha256 is None
    assert binding.interfaces == {"reference": "eth-test0", "mirror": "eth-test1"}

    with pytest.raises(ProeContractError, match="TCC mode"):
        load_profile_binding(str(path), True)


def test_thresholds_require_finite_explicit_values():
    config = {
        "process_timeout_s": 120,
        "thresholds": {
            "max_rtt_us": 10,
            "max_processing_time_us": 20,
            "max_rx_workload_us": 30,
            "max_clock_offset_ns": 100,
            "max_rtt_outliers": 0,
            "max_processing_time_outliers": 0,
            "max_rx_workload_outliers": 0,
            "minimum_packets_per_role": 1,
        },
    }
    assert load_thresholds(config, 60).max_rtt_us == 10

    config["thresholds"]["max_rtt_us"] = float("inf")
    with pytest.raises(ProeContractError, match="finite"):
        load_thresholds(config, 60)


def test_complete_invocation_bound_evidence_passes(tmp_path):
    binding = load_profile_binding(str(_write_profile(tmp_path)), True)
    invocation_id = "0194f0e2-9049-7ef7-9e7d-5b13a0c9d6c1"
    manifest, report = _valid_evidence(binding, invocation_id)

    assessment = assess_run(manifest, report, binding, invocation_id, _thresholds(), process_returncode=0)

    assert assessment.passed
    assert assessment.metrics["max_rtt_us"] == 8.0
    assert assessment.metrics["reference_sent_packets"] == 100


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("final_snapshot_verified", False),
        ("complete", False),
        ("consistent", False),
    ],
)
def test_packet_snapshot_incomplete_never_passes(tmp_path, field, value):
    binding = load_profile_binding(str(_write_profile(tmp_path)), True)
    invocation_id = "0194f0e2-9049-7ef7-9e7d-5b13a0c9d6c1"
    manifest, report = _valid_evidence(binding, invocation_id)
    report["execution"]["packet_counters"][field] = value
    report["results"]["packet_integrity"][field] = value

    assessment = assess_run(manifest, report, binding, invocation_id, _thresholds(), process_returncode=0)

    assert not assessment.passed


def test_stale_manifest_and_report_cannot_pass(tmp_path):
    binding = load_profile_binding(str(_write_profile(tmp_path)), True)
    invocation_id = "0194f0e2-9049-7ef7-9e7d-5b13a0c9d6c1"
    manifest, report = _valid_evidence(binding, invocation_id)
    manifest["invocation_id"] = "0194f0e2-9049-7ef7-9e7d-5b13a0c9d2"
    report["execution"]["invocation_id"] = manifest["invocation_id"]

    assessment = assess_run(manifest, report, binding, invocation_id, _thresholds(), process_returncode=0)

    assert not assessment.passed
    assert any("invocation ID" in error for error in assessment.errors)


def test_missing_maximum_and_nonzero_process_cannot_pass(tmp_path):
    binding = load_profile_binding(str(_write_profile(tmp_path)), True)
    invocation_id = "0194f0e2-9049-7ef7-9e7d-5b13a0c9d6c1"
    manifest, report = _valid_evidence(binding, invocation_id)
    del report["results"]["rt_performance"]["rtt"]["latency"]["maximum"]

    assessment = assess_run(manifest, report, binding, invocation_id, _thresholds(), process_returncode=2)

    assert not assessment.passed
    assert any("maximum" in error for error in assessment.errors)
    assert any("non-zero" in error for error in assessment.errors)


def test_current_incomplete_manifest_is_not_a_pass(tmp_path):
    binding = load_profile_binding(str(_write_profile(tmp_path)), True)
    invocation_id = "0194f0e2-9049-7ef7-9e7d-5b13a0c9d6c1"
    manifest, report = _valid_evidence(binding, invocation_id)
    manifest["status"] = "INCOMPLETE"
    manifest["success"] = False
    report["execution"]["packet_counters"]["final_snapshot_verified"] = False
    report["results"]["packet_integrity"]["final_snapshot_verified"] = False
    report["execution"]["packet_counters"]["complete"] = False
    report["results"]["packet_integrity"]["complete"] = False

    assessment = assess_run(manifest, report, binding, invocation_id, _thresholds(), process_returncode=0)

    assert not assessment.passed
    assert any("not PASS" in error for error in assessment.errors)


def test_latency_thresholds_use_maximum_not_mean_or_jitter(tmp_path):
    binding = load_profile_binding(str(_write_profile(tmp_path)), True)
    invocation_id = "0194f0e2-9049-7ef7-9e7d-5b13a0c9d6c1"
    manifest, report = _valid_evidence(binding, invocation_id)
    rtt = report["results"]["rt_performance"]["rtt"]["latency"]
    rtt["mean"] = 0.1
    rtt["jitter"] = 0.1
    rtt["maximum"] = 11.0

    assessment = assess_run(manifest, report, binding, invocation_id, _thresholds(), process_returncode=0)

    assert not assessment.passed
    assert any("maximum exceeds" in error for error in assessment.errors)
