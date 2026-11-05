# PROe Real-Time Backend

PROe is an optional native real-time benchmark backend. Intel® ESQ installs it from a package that you obtain separately, runs it through the normal `esq run` workflow, and converts its structured report into ESQ results and metrics. Other ESQ profiles do not need it.

!!! note
    ESQ never downloads PROe. Obtain the package ZIP and its trusted SHA-256 checksum through the channel that provides it to you.

## Requirements

- Ubuntu* 24.04 or 26.04 on x86_64, matching the release the package was built for.
- A user that can use `sudo`. The package installer and the benchmark run as root.
- Network access during installation. ESQ installs the real-time kernel and host tools with `apt`, builds `libyang` from source, and the package installer fetches pinned third-party sources.
- A single device under test with the network interfaces that the PROe profile needs.

## Install

Installing ESQ with `uv` installs only ESQ. Install PROe afterwards:

```bash
esq install proe --package /path/to/proe.zip --sha256 <trusted-checksum>
```

!!! important "One command sets up PROe, and it can reboot this machine"
    Run `esq install proe` once. It sets up everything PROe needs: the real-time kernel (6.8.1 or newer) and the kernel boot parameters that PROe benchmarks need to meet their performance targets, the host tools, and PROe itself. When the host does not have the real-time kernel yet, ESQ **reboots the machine once** at the end. The preview lists all of it before ESQ asks one question. Save your work first.

ESQ performs these steps in order:

1. Verifies the SHA-256 checksum of the ZIP before anything is extracted or executed.
2. Validates the archive: it rejects absolute or parent-directory paths, links, special files, unexpected layouts, and oversized contents, and checks every file against the package manifest.
3. Checks that the host operating system release and architecture match the package.
4. Shows a preview of the exact commands and system changes and asks for consent.
5. Asks one question. If the host is not ready, the question covers everything in [Host preparation](#host-preparation): installing the real-time kernel, adding the kernel boot parameters to GRUB, and the reboot at the end.
6. If the host is not ready, prepares it without rebooting: backs up the GRUB files, installs the real-time kernel, writes the boot parameters, runs `update-grub`, and checks the generated `grub.cfg`.
7. Installs the host tools and `libyang` that the PROe package does not include, if they are missing.
8. Runs the package installer as root from a private staging copy whose hashes are verified again.
9. Verifies the installed files, the `proe` command and the service, and records the installed package and the profiles it supports.
10. Reboots, only when every check above passed.

| Option | Purpose |
|--------|---------|
| `--yes` | Give consent for the installation preview non-interactively. This includes the host preparation and the reboot at the end. |
| `--allow-reboot` | Give separate consent for a reboot that a package itself requires. |

ESQ always prints the preview before it asks. If you answer no, or run it non-interactively without `--yes`, it stops without changing anything.

### Host preparation

PROe needs a real-time kernel version **6.8.1 or newer** and a set of kernel boot parameters that are not part of the PROe package. When the running kernel is not a real-time kernel, is older than 6.8.1, or a boot parameter is missing, `esq install proe` asks one question. If you agree, it does the following, installs PROe, and reboots once at the end. If you decline, nothing is changed.

1. Backs up your original GRUB configuration (see below), before anything is changed.
2. Installs the real-time kernel with `apt-get install -y ubuntu-realtime` (no Ubuntu Pro subscription is needed). The package itself makes the `-realtime` kernel the default boot entry. Before installing, ESQ checks the newest version that apt offers (`apt-cache policy linux-image-realtime`). If apt only offers a kernel older than 6.8.1, ESQ stops before changing anything and asks you to enable a repository with a newer real-time kernel.
3. Writes `/etc/default/grub.d/99-esq-proe.cfg`, which adds the boot parameters below to `GRUB_CMDLINE_LINUX`. Your `/etc/default/grub` and other drop-ins are not edited.
4. Runs `update-grub`, then checks the generated `/boot/grub/grub.cfg`: it must contain a real-time kernel entry that carries every boot parameter. If a check fails, ESQ stops before installing PROe or rebooting, and tells you how to revert.

This happens before PROe is installed, and the reboot comes last, so a failure in any earlier step never leaves a reboot pending.

```text
intel_iommu=on iommu=pt isolcpus=1,2 rcu_nocbs=1,2 rcu_nocb_poll irqaffinity=0 kthread_cpus=0
clocksource=tsc tsc=reliable hpet=disable nmi_watchdog=0 nowatchdog efi=noruntime
xe.max_vfs=7 xe.force_probe=* modprobe.blacklist=i915 udmabuf.list_limit=8192 console=ttyS0,115200n8
```

Parameters that are already set are not added again. If a parameter that PROe needs is already set to a different value, for example `isolcpus=3`, ESQ shows it in the preview as `replaces: …`, and the drop-in removes the old value from `GRUB_CMDLINE_LINUX` and `GRUB_CMDLINE_LINUX_DEFAULT` before adding the value PROe needs. Parameters that PROe does not set are kept.

**Backup and revert.** Before the first change, ESQ copies `/etc/default/grub`, the other files in `/etc/default/grub.d/` and the running kernel command line to `/var/backups/esq/proe-grub/` (`default-grub`, `grub.d/` and `cmdline-before`). If that folder already exists, it is kept as the original and not overwritten. To revert the boot parameters, delete `/etc/default/grub.d/99-esq-proe.cfg` and run `update-grub`. Your own GRUB files are never edited, so the backup is a safety copy.

After the reboot PROe is ready to use: it is already installed and its profiles are recorded. To check the host at any time, run the same `esq install proe ...` command again. It installs nothing and asks nothing. It reports whether the real-time kernel is running, whether `/proc/cmdline` contains every parameter and whether `cpupower` works, and explains what is wrong if not (for example, the generic kernel booted) with exit code 2.

### Host tools

The PROe package expects these host tools: `git`, `tc` (iproute2), `ethtool`, `ptp4l` and `phc2sys` (linuxptp), `tuna`, `cpupower`, `rdmsr` and `wrmsr` (msr-tools), `ssh-audit` and `yanglint`. ESQ installs the missing ones with `apt-get`. `libyang` is built from source (version `v4.2.2`, as in the PROe README) and installed to `/usr/local`. ESQ then checks that `cpupower` works with the running kernel. When the installation reboots into another kernel, that check is made after the reboot instead, because `cpupower` is tied to the kernel version.

If `apt` or the network fails, ESQ stops with exit code 1 and the log path, and PROe is not installed. Without ESQ's step, the PROe package installer itself stops with `Missing required host tool: <name>` before it changes anything; it installs only `ssh-audit` itself.

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

It shows a preview and asks one question. Answer no to stop without changing anything. Use `--yes` to answer it without a prompt, and `--keep-data` to keep `/etc/proe` and `/var/log/proe` (the PROe configuration and the benchmark results, which are deleted by default). In order, it does the following:

1. Removes the PROe service, the `proe` command, `/opt/proe`, `/etc/proe` and `/var/log/proe`. PROe profiles disappear from `esq list`.
2. Purges the apt packages that the installation added (the host tools and what they pulled in). Before it does, `apt-get -s purge` must show that apt would remove nothing else. If it would, those packages are left installed and ESQ tells you which ones to remove yourself.
3. Removes the files that the `libyang` build installed to `/usr/local`, and runs `ldconfig`.
4. Deletes `/etc/default/grub.d/99-esq-proe.cfg`, which removes the PROe boot parameters, and runs `update-grub`.
5. Purges the real-time kernel packages that the installation added. A running kernel cannot be removed, so if the real-time kernel is running, ESQ first removes `ubuntu-realtime` (the package that makes it the default boot entry), then **reboots**. Run the same `esq uninstall proe` command after the reboot. It finishes without asking again. If the machine boots the same real-time kernel again, ESQ explains and you can pick the normal kernel in the GRUB menu.
6. Deletes the ESQ state for PROe.

The uninstall only removes what the installation recorded. Packages that were installed before ESQ, and a PROe installation that ESQ did not install, are never touched. The backup of your original GRUB files in `/var/backups/esq/proe-grub/` is kept. Every step checks the current state of the machine, so you can run the command again after an interruption. It returns exit code 0 when finished, 1 on an error, and 2 when it needs you to act (declined, reboot pending, or some steps could not be completed).

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

ESQ reads the report schema version that the package declares.

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

ESQ reads each metric from the report schema version that the package declares.

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
