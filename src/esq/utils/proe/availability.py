# Copyright (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""
Resolution of an ESQ PROe profile to the installed PROe package.

PROe profiles are listed like every other ESQ profile. They can only run when an installed, verified PROe
package names them in the capability record that ``esq install proe`` writes after a complete installation.
:func:`resolve_profile` checks that record, the installation state and the recorded executable, and raises
:class:`~esq.utils.proe.constants.StateError` with a user-facing reason when the profile cannot run, for
example when PROe is not installed.
"""

import os
from pathlib import Path
from typing import Optional, Tuple

from esq.utils.proe.constants import StateError
from esq.utils.proe.state import (
    STATE_INSTALLED,
    CapabilityRecord,
    RecordedProfile,
    read_capability_record,
    read_install_state,
)


def resolve_profile(esq_profile: str, directory: Optional[Path] = None) -> Tuple[CapabilityRecord, RecordedProfile]:
    """Return the verified record and profile entry for an ESQ profile.

    Raises:
        ProeError: With a user-facing reason when the profile is not available.
    """
    record = read_capability_record(directory)
    if record is None:
        raise StateError(
            "The PROe backend is not installed. Install it with: esq install proe --package <zip> --sha256 <checksum>"
        )
    state = read_install_state(directory)
    if state is None or state.state != STATE_INSTALLED or state.package_sha256 != record.package_sha256:
        raise StateError("The PROe installation is incomplete. Re-run 'esq install proe' to finish or repair it.")
    profile = record.find(esq_profile)
    if profile is None:
        raise StateError(f"The installed PROe package does not support profile '{esq_profile}'.")
    if not (os.path.isfile(record.executable) and os.access(record.executable, os.X_OK)):
        raise StateError(f"The PROe executable recorded at install time is missing: {record.executable}.")
    return record, profile
