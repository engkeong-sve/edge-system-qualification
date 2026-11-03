# Copyright (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""
ESQ-specific utility modules.

This package contains AI and model-specific utilities that extend the core sysagent framework:
- models: Model batch processing, YOLO, OpenVINO, KaggleHub, media resources
- downloads: Model and dataset downloading from HuggingFace and ModelScope
- servers: Server-specific utilities (OVMS, etc.)
- services: Shared multi-service orchestration helpers for suite deployments
- references: Verified reference data handling and filtering
- telemetry: ESQ-specific telemetry modules (e.g., package_power via Intel RAPL)
"""

from importlib import import_module

__all__ = [
    # Modules
    "models",
    "downloads",
    "servers",
    "services",
    "references",
    "telemetry",
]


def __getattr__(name: str):
    if name not in __all__:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module = import_module(f".{name}", __name__)
    globals()[name] = module
    return module
