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
UPSTREAM_CI = ".github/workflows/ci.yml"
PRODUCT_CI = ".github/workflows/binance-portfolio-margin-ci.yml"


def workflow_event_branches(relative_path: str, event: str) -> tuple[str, ...]:
    """Read a simple branch allowlist from a top-level workflow event."""
    lines = Path(relative_path).read_text(encoding="utf-8").splitlines()
    try:
        on_index = lines.index("on:")
    except ValueError:
        return ()

    event_marker = f"  {event}:"
    event_index = next(
        (index for index in range(on_index + 1, len(lines)) if lines[index] == event_marker),
        None,
    )
    if event_index is None:
        return ()

    event_end = next(
        (
            index
            for index in range(event_index + 1, len(lines))
            if lines[index].strip() and len(lines[index]) - len(lines[index].lstrip()) <= 2
        ),
        len(lines),
    )
    branch_marker = "    branches:"
    branch_index = next(
        (index for index in range(event_index + 1, event_end) if lines[index] == branch_marker),
        None,
    )
    if branch_index is None:
        return ()

    branches: list[str] = []
    for line in lines[branch_index + 1 :]:
        if line.startswith("      - "):
            branches.append(line.removeprefix("      - ").strip("\"'"))
            continue
        if line.strip() and not line.startswith("      "):
            break
    return tuple(branches)


def tracked_and_untracked_files() -> list[Path]:
    """Return repository files while honoring the repository ignore rules."""
    output = subprocess.check_output(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"]
    )
    return [Path(value.decode()) for value in output.split(b"\0") if value]


def main() -> int:
    violations: list[str] = []

    upstream_pr_branches = workflow_event_branches(UPSTREAM_CI, "pull_request")
    if upstream_pr_branches != ("stable", "develop"):
        violations.append(f"{UPSTREAM_CI} pull_request branches must be exactly develop and stable")

    product_pr_branches = workflow_event_branches(PRODUCT_CI, "pull_request")
    if product_pr_branches != ("binance-portfolio-margin",):
        violations.append(
            f"{PRODUCT_CI} must exclusively handle binance-portfolio-margin pull requests"
        )

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
