"""Asynchronous LookCam service: PPPP core, HTTP/WebSocket, RTSP and static files.

The modules deliberately avoid third-party dependencies: only the Python 3.9+
standard library. The only optional pieces are `ffmpeg`/`cv2` for snapshots.
"""
from __future__ import annotations

import os
import sys
import types

__version__ = "0.1.0"

# Support both supported launch styles:
#   cd lookcam_py          -> python -m lookcam_deepsik.service
#   cd lookcam_deepsik     -> python -m service
# In the second form `service` is a top-level package, so `..vendor` cannot
# be resolved by the import system. Expose the frozen vendor directory as the
# top-level `vendor` package and add the package root for any absolute imports.
if __package__ == "service":
    _PACKAGE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if "vendor" not in sys.modules:
        vendor = types.ModuleType("vendor")
        vendor.__path__ = [os.path.join(_PACKAGE_ROOT, "vendor")]
        vendor.__package__ = "vendor"
        sys.modules["vendor"] = vendor
    if _PACKAGE_ROOT not in sys.path:
        sys.path.insert(0, _PACKAGE_ROOT)
