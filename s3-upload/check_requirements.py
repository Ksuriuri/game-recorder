#!/usr/bin/env python3
"""Exit 0 when installed packages match requirements.txt exactly."""

from __future__ import annotations

import importlib.metadata as m
import sys
from pathlib import Path

REQUIREMENTS = Path(__file__).resolve().parent / "requirements.txt"


def main() -> int:
    for line in REQUIREMENTS.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "==" not in line:
            continue
        pkg, expected = (part.strip() for part in line.split("==", 1))
        try:
            actual = m.version(pkg)
        except m.PackageNotFoundError:
            return 1
        if actual != expected:
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
