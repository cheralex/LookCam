"""Make the repository root importable as `lookcam_deepsik` for tools.

The repository may be cloned under any directory name (LookCam, lookcam_deepsik,
...), so the package name cannot be inferred from the current directory name.
This module only aliases the root; the service itself can also run as the
top-level `service` package via `python -m service`.
"""
from __future__ import annotations

import os
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
if "lookcam_deepsik" not in sys.modules:
    pkg = types.ModuleType("lookcam_deepsik")
    pkg.__path__ = [ROOT]
    pkg.__package__ = "lookcam_deepsik"
    sys.modules["lookcam_deepsik"] = pkg
