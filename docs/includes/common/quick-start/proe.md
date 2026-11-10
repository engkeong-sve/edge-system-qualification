### Optional: PROe real-time backend

Only needed to run the PROe real-time profiles. Install Intel® ESQ first, then install the PROe package that you obtained separately:

```bash
esq install proe --package /path/to/proe.zip --sha256 <trusted-checksum>
```

This one command sets up PROe. When the host does not have the real-time kernel and boot parameters that PROe needs, the same command installs them and **reboots the machine once** at the end. The preview lists this before ESQ asks one question.

See [PROe](suites/proe.md) for the preview, consent, and troubleshooting details.
