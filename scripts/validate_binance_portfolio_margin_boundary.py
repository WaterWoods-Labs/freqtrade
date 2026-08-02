#!/usr/bin/env python3
"""Reject XCoin product files or configuration from this product branch."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


SELF = "scripts/validate_binance_portfolio_margin_boundary.py"
FORBIDDEN_MARKER = b"xcoin"
MARKER_TEST = "tests/test_binance_portfolio_margin_boundary.py"
MAINTENANCE_POLICY = "docs/binance-portfolio-margin-maintenance.md"


def tracked_and_untracked_files() -> list[Path]:
    """Return repository files while honoring the repository ignore rules."""
    output = subprocess.check_output(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"]
    )
    return [Path(value.decode()) for value in output.split(b"\0") if value]


def main() -> int:
    violations: list[str] = []
    for path in tracked_and_untracked_files():
        relative = path.as_posix()
        normalized = relative.casefold()
        if FORBIDDEN_MARKER.decode() in normalized:
            violations.append(f"forbidden product path: {relative}")

        if normalized in {SELF, MARKER_TEST, MAINTENANCE_POLICY} or not path.is_file():
            continue

        content = path.read_bytes()
        if b"\0" in content[:8192]:
            continue
        if FORBIDDEN_MARKER in content.lower():
            violations.append(f"forbidden product marker in: {relative}")

    if violations:
        print("Binance Portfolio Margin product boundary validation failed:", file=sys.stderr)
        for violation in violations:
            print(f"- {violation}", file=sys.stderr)
        return 1

    print("Binance Portfolio Margin product boundary validation passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
