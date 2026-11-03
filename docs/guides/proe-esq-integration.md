# PROe single-host integration

The PROe integration is intentionally opt-in. Its two example profiles live under
`src/esq/configs/profiles/examples/`, which ESQ excludes from `esq run --all`.
Select only the TCC or non-TCC example when running a qualification:

```bash
esq run --profile profile.suite.realtime.proe-single-host-tcc
esq run --profile profile.suite.realtime.proe-single-host-non-tcc
```

Before running, edit the selected example profile with an absolute `profile_file`,
the matching `expected_tcc_enabled` setting, a bounded `process_timeout_s`, and
finite limits for all required latency, clock-offset, and outlier metrics. The
examples deliberately leave these values unset: ESQ does not invent qualification
thresholds. A missing value prevents PROe from starting. Keep actual profile paths,
interfaces, and other environment-specific values in local development state; do
not publish private machine details in the fork.

The PROe YAML must describe a single-host `resnet50` run, a positive duration,
two distinct network interfaces, both TSN configuration files, and the selected
TCC mode. The wrapper binds the manifest and report to the invocation UUID, exact
profile bytes, and exact selected configuration-file bytes. It also checks the
requested and actual role/workload durations, both roles' packet counters, runtime
`taprio` qdiscs, and measured maximum latency and outlier limits. Parsed GCL
configuration is not treated as proof of runtime scheduling. Clock synchronization
status and offset evidence are checked without treating the legacy `ptp_sync_status`
field as evidence that `ptp4l` ran.

The wrapper does not provision Ubuntu Pro, install a kernel or PROe binary, reboot,
or change NIC, qdisc, TCC, or service state. Before running, an operator must
confirm Canonical Ubuntu 24.04, an active `CONFIG_PREEMPT_RT=y` kernel, an enabled
and entitled Ubuntu Pro `realtime-kernel` service, and two linked `igc` interfaces
connected for the intended topology. If any check fails, ESQ stops before invoking
PROe. Setup and rollback are not automated; in particular, restoration of the
original NIC, qdisc, and TCC state is not implemented.

PROe evidence is never cached. A run can pass only when its manifest is `PASS`,
the report is complete and invocation-bound, all required values are present and
finite, and every explicit threshold passes. Incomplete packet snapshots, missing
maximum-latency values, nonzero process exits, or producer-reported errors fail
closed. The producer's current single-host report marks its final packet snapshot
unverified; such runs remain `INCOMPLETE` and cannot qualify. Exact inference
overlap across the full RT window also remains unverified without inference
start/stop telemetry.

The integration reads the PROe CLI and JSON contract only. PROe implementation,
internal profiles, machine identities, and operational configuration do not belong
in the public ESQ repository.
