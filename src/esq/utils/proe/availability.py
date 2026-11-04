# Copyright (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""
Profile availability provider for the optional PROe backend.

Profiles that declare ``requires_backend: proe`` are listed and runnable only when a verified
capability record names them. This is evaluated on every profile discovery so that ``esq list``,
``esq run`` and interactive selection always agree. Any inconsistency hides the profile
(fail closed) and the reason is available from :func:`explain`.
"""

import logging
import os
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from esq.utils.proe.constants import ProeError, StateError
from esq.utils.proe.state import (
    STATE_INSTALLED,
    CapabilityRecord,
    RecordedProfile,
    read_capability_record,
    read_install_state,
)

logger = logging.getLogger(__name__)


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


def explain(esq_profile: str, directory: Optional[Path] = None) -> Optional[str]:
    """Return why a profile is unavailable, or ``None`` when it is available."""
    try:
        resolve_profile(esq_profile, directory)
    except ProeError as exc:
        return str(exc)
    except OSError as exc:
        return f"PROe state could not be read: {exc}"
    return None


def is_available(profile_name: str, profile_data: Optional[Dict[str, Any]] = None) -> bool:
    """Entry point for the ``sysagent.profile_availability`` group (backend ``proe``)."""
    reason = explain(profile_name)
    if reason:
        logger.debug(f"PROe profile '{profile_name}' hidden: {reason}")
        return False
    return True
