# Copyright (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the optional PROe backend integration (synthetic fixtures only)."""

import importlib
import sys
import types
from pathlib import Path


def _ensure_importable() -> None:
    """Allow the stdlib-only PROe modules to be imported without the heavy ESQ dependencies.

    ``esq.utils`` eagerly imports optional AI and data packages. When they are not installed, a
    namespace stand-in is registered so ``esq.utils.proe`` (which needs none of them) can be tested.
    """
    try:
        importlib.import_module("esq.utils")
    except ImportError:
        for name in [n for n in sys.modules if n == "esq.utils" or n.startswith("esq.utils.")]:
            del sys.modules[name]
        esq = importlib.import_module("esq")
        stand_in = types.ModuleType("esq.utils")
        stand_in.__path__ = [str(Path(esq.__file__).parent / "utils")]
        sys.modules["esq.utils"] = stand_in
        esq.utils = stand_in


_ensure_importable()
