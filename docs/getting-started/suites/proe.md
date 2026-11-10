# PROe Real-Time Backend

PROe is an optional native real-time benchmark backend. Intel® ESQ installs it from a package that you obtain separately, runs it through the normal `esq run` workflow, and converts its structured report into ESQ results and metrics. Other ESQ profiles do not need it.

!!! note
    ESQ never downloads PROe. Obtain the package ZIP and its trusted SHA-256 checksum through the channel that provides it to you.

## Requirements

- Ubuntu* 26.04 or newer on x86_64, matching the release the package was built for.
- A user that can use `sudo`. The package installer and the benchmark run as root.
- Network access during installation. ESQ installs the real-time kernel and host tools with `apt`, builds `libyang` from source, and the package installer fetches pinned third-party sources.
- One device under test with two network ports cabled to each other, for the single DUT 2 NIC profile.

## Install

Installing ESQ with `uv` installs only ESQ. Install PROe afterwards:

```bash
esq install proe --package /path/to/proe.zip --sha256 <trusted-checksum>
```

!!! important "One command sets up PROe, and it can reboot this machine"
    Run `esq install proe` once. It sets up everything PROe needs: the real-time kernel and the kernel boot parameters that PROe benchmarks need to meet their performance targets, the host tools, and PROe itself. When the host does not have the real-time kernel yet, ESQ **reboots the machine once** at the end. 

| Option | Purpose |
|--------|---------|
| `--yes` | Give consent for the installation preview non-interactively. This includes the host preparation and the reboot at the end. |
| `--allow-reboot` | Give separate consent for a reboot that a package itself requires. |

ESQ always prints the preview before it asks. If you answer no, or run it non-interactively without `--yes`, it stops without changing anything.

### Existing installations

ESQ never overwrites or removes an existing PROe installation.

- Running the same command again for the same package is idempotent and verifies the existing installation.
- If PROe files from another package or an incomplete attempt exist, ESQ stops with an explanation. If ESQ installed them, remove them with [`esq uninstall proe`](#uninstall). Otherwise use the uninstall script of the package that installed them. Then run the command again.
- If a package requires a reboot, ESQ asks for separate consent, records the progress, and resumes when you run the same command after the reboot. When the host preparation also needs a reboot, it is the same single reboot and the one confirmation covers it.

## Uninstall

`esq uninstall proe` undoes what `esq install proe` did and returns the system to how it was before, using the record that the installation kept in `~/.local/state/esq/proe`. It does not need the PROe package.

```bash
esq uninstall proe
```

When the real-time kernel is running, ESQ reboots the machine once. After the reboot, run `esq uninstall proe` again to finish removing the kernel. Do not install PROe again before that second run.

## Configure

The profile runs the PROe `1ms_ai_single_dut` profile: the reference and mirror roles run on this machine, each on its own NIC, with the two NICs cabled to each other. The installed PROe profile file has example NIC names, so edit it once after `esq install proe`, before the first run:

```bash
ip -br link                                              # find the two cabled NICs
sudo nano /etc/proe/profile_configs/1ms_ai_single_dut.yaml
sudo nano /etc/proe/profile_configs/1ms_ai_single_dut_baseline.yaml
```

| Setting | Set it to |
|---------|-----------|
| `nic_setup.reference.ptp_interface`, `nic_setup.mirror.ptp_interface` | The names of the two cabled NICs. |
| `nic_setup.reference.mac_address`, `nic_setup.mirror.mac_address` | Two different MAC addresses. |
| `test_duration` | The measurement time in seconds. The example is 120; use 86400 (24 h) for a qualification run. |

Installing PROe again restores the example file.

## Run

Prime `sudo` credentials first, because the run never prompts for a password:

```bash
sudo -v
esq run --profile profile.suite.realtime.proe_1ms_ai_single_dut
```

The profile `profile.suite.realtime.proe_1ms_ai_single_dut` applies the host tuning that the PROe package provides (an OS tuning script, run before the benchmark, and a host tuning file in the PROe profile). The profile `profile.suite.realtime.proe_1ms_ai_single_dut_baseline` applies neither, to compare against it. Run the baseline first after a reboot: the tuning stays in effect until the next reboot.

When PROe reports a failed KPI, the test fails, and the metrics of that run and the failed KPI rows are still shown in the report.

You can also select the profile in the interactive `esq run` prompt.

## Metrics

ESQ records only values that appear in the PROe report. No value is estimated, defaulted, or derived.

| Metric | Unit |
|--------|------|
| `rtt_mean_us`, `rtt_jitter_us`, `rtt_outliers` | µs, µs, count |
| `processing_time_mean_us`, `processing_time_jitter_us`, `processing_time_outliers` | µs, µs, count |
| `rx_workload_mean_us`, `rx_workload_jitter_us`, `rx_workload_outliers` | µs, µs, count |
| `packet_loss_percent` | percent |
| `clock_offset_mean_ns`, `clock_sample_count` | ns, count |

ESQ reads each metric from the report schema version that the package declares.

## Troubleshooting

| Symptom | Action |
|---------|--------|
| Checksum mismatch | Download the package again and confirm the checksum through a trusted channel. |
| Package rejected | The archive failed validation; do not extract or run it manually. |
| Host not compatible | Use the package built for your Ubuntu release and architecture. |
| Existing files block installation | Run the `uninstall.sh` script of the package that installed them, as root, then retry. |
| `The PROe backend is not installed` | Run `esq install proe` first. |
| The run fails while configuring a NIC | Set the two NIC names in `/etc/proe/profile_configs/1ms_ai_single_dut.yaml` and `1ms_ai_single_dut_baseline.yaml` to the two cabled NICs of this machine. |
| `A previous 'esq uninstall proe' has not finished` | Run `esq uninstall proe` again. Do not reboot again. |
| `sudo` credentials required | Run `sudo -v`, then run again. |
