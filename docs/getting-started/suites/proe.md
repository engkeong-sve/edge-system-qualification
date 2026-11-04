# PROe Real-Time Backend

PROe is an optional native real-time benchmark backend. Intel® ESQ installs it from a package that you obtain separately, runs it through the normal `esq run` workflow, and converts its structured report into ESQ results and metrics. Other ESQ profiles do not need it.

!!! warning "Draft: not benchmark-ready"
    This integration is a draft and has not been verified end to end. The single-DUT CLI, profile and report producer contract is pending agreement, and the optional capability metadata described below is a proposal that has not been agreed. The current PROe package exposes no supported profiles, so `esq list` shows no PROe profile and no PROe benchmark can be run.

!!! note
    ESQ never downloads PROe. Obtain the package ZIP and its trusted SHA-256 checksum through the channel that provides it to you.

## Requirements

- Ubuntu* 24.04 or 26.04 on x86_64, matching the release the package was built for.
- A user that can use `sudo`. The package installer and the benchmark run as root.
- Network access during installation. The package installer can install host tools and fetch pinned third-party sources.
- A single device under test with the network interfaces that the PROe profile needs.

## Install

Installing ESQ with `uv` installs only ESQ. Install PROe afterwards:

```bash
esq install proe --package /path/to/proe.zip --sha256 <trusted-checksum>
```

ESQ performs these steps in order:

1. Verifies the SHA-256 checksum of the ZIP before anything is extracted or executed.
2. Validates the archive: it rejects absolute or parent-directory paths, links, special files, unexpected layouts, and oversized contents, and checks every file against the package manifest.
3. Checks that the host operating system release and architecture match the package.
4. Shows a preview of the exact commands and system changes and asks for consent.
5. Runs the package installer as root from a private staging copy whose hashes are verified again.
6. Records the installed package and the profiles it supports.

| Option | Purpose |
|--------|---------|
| `--dry-run` | Verify and show the preview, then stop without changing the system. |
| `--yes` | Give consent for the installation preview non-interactively. |
| `--allow-reboot` | Give separate consent for a reboot, when a package requires one. |

### Existing installations

ESQ never overwrites or removes an existing PROe installation.

- Running the same command again for the same package is idempotent and verifies the existing installation.
- If PROe files from another package or an incomplete attempt exist, ESQ stops with an explanation. Remove them with the uninstall script of the package that installed them, then run the command again.
- If a package requires a reboot, ESQ asks for separate consent, records the progress, and resumes when you run the same command after the reboot.

## Supported profiles

`esq list` shows a PROe profile only when the installed package declares support for it and the installation is complete. Possible profiles are:

| Profile | Test ID |
|---------|---------|
| `profile.suite.realtime.proe_1ms_ai` | RT-PROE-001 |
| `profile.suite.realtime.proe_control` | RT-PROE-002 |

If a profile is not listed, it cannot be selected or run, and the reason is written to the ESQ debug log. Without a PROe installation, `esq list` and `esq run` behave as before.

### Package support contract

ESQ lists a profile only when the installed package gives ESQ evidence that it supports that profile on a single device under test. ESQ never infers support from an installed PROe command or a version string. Until a package provides this evidence, `esq install proe` can still install and verify the package, but no PROe profile is listed or runnable.

The evidence ESQ needs from a package is:

- the PROe profile identifier that each ESQ profile maps to, and the report schema version it produces;
- a single-device topology for that profile;
- how the benchmark is started for that topology and where the report is written.

The report schema that ESQ currently understands is provisional until a single-device report is confirmed.

## Run

Prime `sudo` credentials first, because the run never prompts for a password:

```bash
sudo -v
esq run --profile profile.suite.realtime.proe_1ms_ai
```

You can also select the profile in the interactive `esq run` prompt.

## Configuration

PROe profiles use a one-hour timeout. Network and clock settings are owned by the PROe package configuration, not by ESQ. The PROe service must be active before a run.

## Metrics

ESQ records only values that appear in the PROe report. No value is estimated, defaulted, or derived.

| Metric | Unit |
|--------|------|
| `rtt_mean_us`, `rtt_jitter_us`, `rtt_outliers` | µs, µs, count |
| `processing_time_mean_us`, `processing_time_jitter_us`, `processing_time_outliers` | µs, µs, count |
| `rx_workload_mean_us`, `rx_workload_jitter_us`, `rx_workload_outliers` | µs, µs, count |
| `packet_loss_percent` | percent |
| `clock_offset_mean_ns`, `clock_sample_count` | ns, count |

The report schema that ESQ currently accepts is provisional and may change with the supported PROe package.

## Results

Each run attaches the command, standard output, standard error, and the raw PROe report as evidence to the ESQ report.

!!! important
    A successful run is not a qualification result. The PROe pipeline status is shown for information, and ESQ applies no pass or fail thresholds to PROe metrics until approved thresholds are defined. ESQ reports an error, not a result, when:

    - the run produces no new report, or more than one new report;
    - the report is older than the run or belongs to another profile;
    - a field is missing, null, not finite, or has an unexpected unit, type, or status value;
    - the exit code and the report status contradict each other.

ESQ never reuses an earlier report or a cached result for PROe profiles.

## Troubleshooting

| Symptom | Action |
|---------|--------|
| Checksum mismatch | Download the package again and confirm the checksum through a trusted channel. |
| Package rejected | The archive failed validation; do not extract or run it manually. |
| Host not compatible | Use the package built for your Ubuntu release and architecture. |
| Existing files block installation | Run the `uninstall.sh` script of the package that installed them, as root, then retry. |
| Profile missing from `esq list` | Check that installation finished and that the package declares the profile. |
| `sudo` credentials required | Run `sudo -v`, then run again. |
| No new report | Check that the PROe service is active and that the results directory is writable and not used by another run. |

The installation log is stored in the ESQ state directory (`$XDG_STATE_HOME/esq/proe`, by default `~/.local/state/esq/proe`).
