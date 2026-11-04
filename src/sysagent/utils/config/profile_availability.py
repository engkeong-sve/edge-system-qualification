# Copyright (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""
Optional-backend availability gate for profile discovery.

A profile may declare a top-level ``requires_backend: <name>`` key. The profile is then discovered
(listed, selectable and runnable) only if an availability checker registered for that backend
returns ``True``. Checkers are registered through the ``sysagent.profile_availability`` entry-point
group and are called as ``checker(profile_name, profile_config) -> bool``.

The gate fails closed: an unknown backend or a checker that cannot be loaded makes the profile
unavailable. Checkers must report their own failures as ``False``.
"""

import logging
from importlib.metadata import entry_points
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)

ENTRY_POINT_GROUP = "sysagent.profile_availability"
BACKEND_KEY = "requires_backend"


def _load_checker(backend: str) -> Optional[Callable[[str, Dict[str, Any]], bool]]:
    for entry in entry_points(group=ENTRY_POINT_GROUP):
        if entry.name == backend:
            try:
                return entry.load()
            except (ImportError, AttributeError) as exc:
                logger.warning(f"Availability checker for backend '{backend}' could not be loaded: {exc}")
                return None
    logger.warning(f"No availability checker is registered for backend '{backend}'")
    return None


def is_profile_available(profile_name: str, config: Dict[str, Any]) -> bool:
    """Return whether a profile passes its optional backend requirement."""
    backend = config.get(BACKEND_KEY)
    if backend is None:
        return True
    if not isinstance(backend, str) or not backend:
        logger.warning(f"Profile '{profile_name}' has an invalid '{BACKEND_KEY}' value")
        return False
    checker = _load_checker(backend)
    if checker is None:
        return False
    return bool(checker(profile_name, config))
