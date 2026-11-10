"""Start the service from the repository/package directory: python run.py [args].

The directory is the repository root and is self-contained: service code,
vendor helpers, config, cache, tests and tools live below it. No parent
`lookcam_py` checkout is required.
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
from service.main import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
