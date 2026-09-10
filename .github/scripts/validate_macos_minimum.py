#!/usr/bin/env python3
"""Require an Intel macOS artifact to target exactly Monterey 12.0."""

from __future__ import annotations

import sys


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: validate_macos_minimum.py VERSION", file=sys.stderr)
        return 2
    value = argv[1]
    parts_text = value.split(".")
    if len(parts_text) not in (2, 3) or any(
        not part or not part.isascii() or not part.isdigit() for part in parts_text
    ):
        print(f"error: invalid macOS minimum version {value!r}", file=sys.stderr)
        return 1
    parts = tuple(int(part) for part in parts_text)
    if parts[:2] != (12, 0) or (len(parts) == 3 and parts[2] != 0):
        print(
            f"error: macOS minimum version {value} is not exactly Monterey 12.0",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
