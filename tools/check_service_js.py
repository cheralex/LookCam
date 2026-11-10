"""Syntax check of the service's ES modules (node --check)."""
from __future__ import annotations

import pathlib
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent
STATIC = HERE.parent / "service" / "static"
TMP = HERE / "_js_check"


def main() -> int:
    TMP.mkdir(exist_ok=True)
    bad = 0
    for path in sorted(STATIC.glob("*.js")):
        target = TMP / (path.stem + ".mjs")
        target.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
        res = subprocess.run(["node", "--check", str(target)],
                             capture_output=True, text=True, errors="replace")
        if res.returncode == 0:
            print(f"PASS {path.name}")
        else:
            bad += 1
            print(f"FAIL {path.name}")
            print(res.stdout + res.stderr)
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
