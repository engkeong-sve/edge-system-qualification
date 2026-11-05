# Copyright (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""
Optional native PROe real-time backend integration.

The PROe binary package is supplied separately by an authorized channel and is never
part of this repository. This package contains only generic, safe interfaces:

- ``package``       Checksum, archive-safety and manifest validation of a local package
- ``installer``     Consent-driven installation orchestration (``esq install proe``)
- ``state``         Persistent installation state and verified capability record
- ``availability``  Resolves an ESQ PROe profile to the installed, verified package
- ``report``        Strict parser for the structured PROe benchmark report
- ``adapter``       Thin PROe CLI adapter that maps a report to native ESQ results

Importing this package has no side effects and requires no third-party dependency.
"""
