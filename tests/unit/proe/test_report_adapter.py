# Copyright (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

import copy
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from esq.utils.proe import adapter
from esq.utils.proe import state as proe_state
from esq.utils.proe.constants import ProeError, ReportError
from esq.utils.proe.report import check_run_consistency, parse_report, parse_report_text

SUPPORTED_PROFILE = "profile.suite.realtime.proe_1ms_ai"

# Synthetic report in the structure of schema 1.0 (invented numbers).
GOOD_REPORT = {
    "schema_version": "1.0",
    "profile_id": "1ms_ai",
    "timestamp_utc": "2026-01-02T03:04:05Z",
    "status": "PASS",
    "results": {
        "rt_performance": {
            "rtt": {"latency": {"mean": 1500.5, "jitter": 20.0, "unit": "us"}, "outliers": 1},
            "processing_time": {"latency": {"mean": 700.25, "jitter": 30.0, "unit": "us"}, "outliers": 0},
            "rx_workload": {"latency": {"mean": 200.0, "jitter": 50.0, "unit": "us"}, "outliers": 2},
            "packet_loss": {"value": 0.0, "unit": "percent"},
        },
        "clock_sync": {"offset": {"mean": 50.5, "unit": "ns"}, "ptp_sync_status": "LOCKED", "sample_count": 12},
    },
}


def report(**mutations):
    data = copy.deepcopy(GOOD_REPORT)
    for path, value in mutations.items():
        node = data
        keys = path.split("__")
        for key in keys[:-1]:
            node = node[key]
        if value is DELETE:
            del node[keys[-1]]
        else:
            node[keys[-1]] = value
    return data


DELETE = object()


class ReportParsingTests(unittest.TestCase):
    def parse(self, data, profile="1ms_ai", schema="1.0"):
        return parse_report(data, profile, schema)

    def test_maps_every_reported_value(self):
        parsed = self.parse(GOOD_REPORT)
        metrics = {m.name: m for m in parsed.metrics}
        self.assertEqual(metrics["rtt_mean_us"].value, 1500.5)
        self.assertEqual(metrics["rtt_mean_us"].unit, "us")
        self.assertEqual(metrics["rtt_jitter_us"].value, 20.0)
        self.assertEqual(metrics["rx_workload_outliers"].value, 2)
        self.assertEqual(metrics["packet_loss_percent"].unit, "percent")
        self.assertEqual(metrics["clock_offset_mean_ns"].value, 50.5)
        self.assertEqual(metrics["clock_sample_count"].value, 12)
        self.assertEqual(parsed.pipeline_status, "PASS")
        self.assertTrue(parsed.provisional_schema)

    def test_no_derived_maximum_latency(self):
        names = {m.name for m in self.parse(GOOD_REPORT).metrics}
        self.assertFalse([n for n in names if "max" in n])

    def test_text_entry_point_and_invalid_json(self):
        self.assertEqual(parse_report_text(json.dumps(GOOD_REPORT), "1ms_ai", "1.0").profile_id, "1ms_ai")
        with self.assertRaisesRegex(ReportError, "not valid JSON"):
            parse_report_text("{", "1ms_ai", "1.0")

    def test_not_an_object(self):
        with self.assertRaises(ReportError):
            self.parse([])

    def test_missing_schema_version(self):
        with self.assertRaisesRegex(ReportError, "no schema_version"):
            self.parse(report(schema_version=DELETE))

    def test_unknown_schema_version(self):
        with self.assertRaisesRegex(ReportError, "Unsupported"):
            self.parse(report(schema_version="2.0"), schema="2.0")

    def test_schema_must_match_declared(self):
        with self.assertRaisesRegex(ReportError, "declares"):
            self.parse(GOOD_REPORT, schema="1.1")

    def test_profile_mismatch(self):
        with self.assertRaisesRegex(ReportError, "expected"):
            self.parse(GOOD_REPORT, profile="other")

    def test_bad_timestamp(self):
        with self.assertRaisesRegex(ReportError, "timestamp"):
            self.parse(report(timestamp_utc="yesterday"))

    def test_unknown_status(self):
        with self.assertRaisesRegex(ReportError, "status"):
            self.parse(report(status="MAYBE"))

    def test_missing_sections(self):
        for path in (
            "results__rt_performance__rtt",
            "results__rt_performance__packet_loss",
            "results__clock_sync",
            "results__rt_performance__rtt__latency__mean",
            "results__clock_sync__offset",
        ):
            with self.subTest(path=path), self.assertRaisesRegex(ReportError, "missing"):
                self.parse(report(**{path: DELETE}))

    def test_null_packet_loss_is_not_zero(self):
        with self.assertRaisesRegex(ReportError, "must be a number"):
            self.parse(report(results__rt_performance__packet_loss={"value": None, "unit": "percent"}))

    def test_placeholder_text_rejected(self):
        with self.assertRaisesRegex(ReportError, "must be a number"):
            self.parse(report(results__rt_performance__rtt={"latency": {"mean": "Not available", "jitter": 1, "unit": "us"}, "outliers": 0}))

    def test_non_finite_rejected(self):
        for value in (float("nan"), float("inf")):
            with self.subTest(value=value), self.assertRaisesRegex(ReportError, "finite"):
                self.parse(
                    report(
                        results__rt_performance__rtt={"latency": {"mean": value, "jitter": 1, "unit": "us"}, "outliers": 0}
                    )
                )

    def test_boolean_is_not_a_number(self):
        with self.assertRaisesRegex(ReportError, "must be a number"):
            self.parse(
                report(
                    results__rt_performance__rtt={"latency": {"mean": True, "jitter": 1, "unit": "us"}, "outliers": 0}
                )
            )

    def test_wrong_unit_rejected(self):
        with self.assertRaisesRegex(ReportError, "unit"):
            self.parse(
                report(
                    results__rt_performance__rtt={"latency": {"mean": 1, "jitter": 1, "unit": "ms"}, "outliers": 0}
                )
            )
        with self.assertRaisesRegex(ReportError, "unit"):
            self.parse(report(results__rt_performance__packet_loss={"value": 0.0, "unit": "ratio"}))

    def test_packet_loss_range(self):
        with self.assertRaisesRegex(ReportError, "above"):
            self.parse(report(results__rt_performance__packet_loss={"value": 101.0, "unit": "percent"}))

    def test_negative_jitter_rejected(self):
        with self.assertRaisesRegex(ReportError, "below"):
            self.parse(
                report(
                    results__rt_performance__rtt={"latency": {"mean": 1, "jitter": -1, "unit": "us"}, "outliers": 0}
                )
            )

    def test_outliers_must_be_integer(self):
        with self.assertRaisesRegex(ReportError, "integer"):
            self.parse(
                report(
                    results__rt_performance__rtt={"latency": {"mean": 1, "jitter": 1, "unit": "us"}, "outliers": 1.5}
                )
            )

    def test_bad_ptp_status(self):
        with self.assertRaisesRegex(ReportError, "ptp_sync_status"):
            self.parse(report(results__clock_sync={"offset": {"mean": 1, "unit": "ns"}, "ptp_sync_status": "Not set", "sample_count": 1}))

    def test_exit_code_vs_status_contradiction(self):
        parsed = self.parse(report(status="FAIL"))
        self.assertIn("FAIL", check_run_consistency(parsed, 0))
        self.assertIsNone(check_run_consistency(self.parse(GOOD_REPORT), 0))


class FakeExecutor:
    """Simulates ``proe`` and the system tools; can write reports into a results directory."""

    def __init__(self, results_dir: Path, *, writes=None, returncode=0, timed_out=False, service="active", stderr=""):
        self.results_dir = results_dir
        self.writes = writes if writes is not None else [("benchmark_1ms_ai_20260102T030405Z.json", GOOD_REPORT)]
        self.returncode = returncode
        self.timed_out = timed_out
        self.service = service
        self.stderr = stderr
        self.calls = []

    def __call__(self, argv, timeout):
        self.calls.append((list(argv), timeout))
        if argv[-1] == "--version":
            return adapter.CommandOutput(0, "proe 1.0.0", "")
        if argv[0] == "systemctl":
            return adapter.CommandOutput(0 if self.service == "active" else 3, self.service, "")
        for name, content in self.writes:
            text = content if isinstance(content, str) else json.dumps(content)
            (self.results_dir / name).write_text(text)
        return adapter.CommandOutput(self.returncode, "run output", self.stderr, self.timed_out)


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.state = root / "state"
        self.results = root / "results"
        self.results.mkdir()
        cli = root / "proe"
        cli.write_text("x")
        cli.chmod(0o755)
        sha = "b" * 64
        record = proe_state.CapabilityRecord(
            "pkg", sha, "c" * 40, str(cli), (proe_state.RecordedProfile(SUPPORTED_PROFILE, "1ms_ai", "1.0"),), str(self.results)
        )
        proe_state.write_capability_record(record, self.state)
        proe_state.write_install_state(proe_state.InstallState(proe_state.STATE_INSTALLED, sha, "pkg"), self.state)
        # Clock set so the synthetic report timestamp is "during" the run.
        self.now = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc).timestamp()

    def _run(self, executor, **kwargs):
        return adapter.run_benchmark(
            SUPPORTED_PROFILE, executor=executor, directory=self.state, is_root=True, clock=lambda: self.now, **kwargs
        )

    def test_command_construction(self):
        self.assertEqual(
            adapter.build_command("/usr/local/bin/proe", "1ms_ai", True),
            ["/usr/local/bin/proe", "profile", "run-benchmark", "1ms_ai"],
        )
        self.assertEqual(
            adapter.build_command("/usr/local/bin/proe", "1ms_ai", False)[:4],
            ["sudo", "-n", "--", "/usr/local/bin/proe"],
        )

    def test_successful_run(self):
        executor = FakeExecutor(self.results)
        run = self._run(executor)
        self.assertEqual(run.proe_profile, "1ms_ai")
        self.assertEqual(run.report.pipeline_status, "PASS")
        self.assertIn("run-benchmark", run.command)
        self.assertEqual(json.loads(run.report_text)["profile_id"], "1ms_ai")
        self.assertEqual(run.stdout, "run output")

    def test_stale_report_is_never_used(self):
        (self.results / "benchmark_1ms_ai_20260101T000000Z.json").write_text(json.dumps(GOOD_REPORT))
        executor = FakeExecutor(self.results, writes=[])
        with self.assertRaisesRegex(adapter.RunFailure, "no new benchmark report"):
            self._run(executor)

    def test_new_report_with_old_timestamp_is_rejected(self):
        old = report(timestamp_utc="2025-01-01T00:00:00Z")
        executor = FakeExecutor(self.results, writes=[("benchmark_1ms_ai_x.json", old)])
        with self.assertRaisesRegex(adapter.RunFailure, "older than this run"):
            self._run(executor)

    def test_ambiguous_reports_rejected(self):
        executor = FakeExecutor(self.results, writes=[("benchmark_a.json", GOOD_REPORT), ("benchmark_b.json", GOOD_REPORT)])
        with self.assertRaisesRegex(adapter.RunFailure, "More than one"):
            self._run(executor)

    def test_failure_carries_raw_evidence(self):
        executor = FakeExecutor(self.results, writes=[("benchmark_x.json", "{broken")])
        with self.assertRaises(adapter.RunFailure) as ctx:
            self._run(executor)
        self.assertEqual(ctx.exception.evidence["report"], "{broken")
        self.assertEqual(ctx.exception.evidence["stdout"], "run output")

    def test_nonzero_exit(self):
        executor = FakeExecutor(self.results, writes=[], returncode=2, stderr="sudo: a password is required")
        with self.assertRaisesRegex(adapter.RunFailure, "sudo -v"):
            self._run(executor)

    def test_timeout(self):
        executor = FakeExecutor(self.results, writes=[], returncode=-1, timed_out=True)
        with self.assertRaisesRegex(adapter.RunFailure, "did not finish within 5 seconds"):
            self._run(executor, timeout_s=5)
        self.assertEqual(executor.calls[-1][1], 5)

    def test_success_exit_with_failed_report_is_inconsistent(self):
        executor = FakeExecutor(self.results, writes=[("benchmark_x.json", report(status="FAIL"))])
        with self.assertRaisesRegex(adapter.RunFailure, "report status is FAIL"):
            self._run(executor)

    def test_wrong_profile_in_report(self):
        executor = FakeExecutor(self.results, writes=[("benchmark_x.json", report(profile_id="other"))])
        with self.assertRaisesRegex(adapter.RunFailure, "expected"):
            self._run(executor)

    def test_inactive_service_is_reported_before_running(self):
        executor = FakeExecutor(self.results, service="inactive")
        with self.assertRaisesRegex(ProeError, "not active"):
            self._run(executor)
        self.assertFalse(any(call[0][:2] == ["/x", "profile"] for call in executor.calls))
        self.assertEqual([p.name for p in self.results.iterdir()], [])

    def test_unsupported_profile_is_refused(self):
        with self.assertRaisesRegex(ProeError, "does not support"):
            adapter.run_benchmark(
                "profile.suite.realtime.proe_control", executor=FakeExecutor(self.results), directory=self.state
            )

    def test_not_installed_is_refused(self):
        with tempfile.TemporaryDirectory() as empty:
            with self.assertRaisesRegex(ProeError, "not installed"):
                adapter.run_benchmark(SUPPORTED_PROFILE, executor=FakeExecutor(self.results), directory=Path(empty))

    def test_each_run_is_fresh(self):
        first = self._run(FakeExecutor(self.results))
        second = self._run(FakeExecutor(self.results, writes=[("benchmark_1ms_ai_20260102T030406Z.json", GOOD_REPORT)]))
        self.assertNotEqual(first.report_path, second.report_path)


if __name__ == "__main__":
    unittest.main()
