"""Per-package coverage gate (PROMPT §15.2). Reads ``coverage.json`` written by ``make coverage``.

Line coverage and branch coverage, each on its own, per top-level ``rd_*`` package must reach the floor: 90% for the
packages §15.2 names, 80% for everything else. Every workspace package must appear in the report.
"""

from __future__ import annotations

import json
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
STRICT = {"rd_quant", "rd_data", "rd_compliance", "rd_reports", "rd_billing", "rd_licensing"}
DEFAULT_FLOOR = 80.0
STRICT_FLOOR = 90.0
_PKG = re.compile(r"/src/(rd_[a-z_]+)/")


def workspace_packages() -> set[str]:
    """Every rd_* package in the workspace; each must appear in the coverage report."""
    return {p.name for p in REPO.glob("*/*/src/rd_*") if p.is_dir()}


def package_coverage(report: dict[str, Any]) -> dict[str, tuple[float, float]]:
    """Per package: (line %, branch %). A package with no branches counts as 100% branch coverage."""
    files: dict[str, Any] = report["files"]
    lines_hit: dict[str, int] = defaultdict(int)
    lines_total: dict[str, int] = defaultdict(int)
    br_hit: dict[str, int] = defaultdict(int)
    br_total: dict[str, int] = defaultdict(int)
    for path, data in files.items():
        m = _PKG.search("/" + path.replace("\\", "/"))
        if m is None:
            continue
        summary = data["summary"]
        pkg = m.group(1)
        lines_hit[pkg] += summary["covered_lines"]
        lines_total[pkg] += summary["num_statements"]
        br_hit[pkg] += summary.get("covered_branches", 0)
        br_total[pkg] += summary.get("num_branches", 0)

    def pct(hit: int, total: int) -> float:
        return 100.0 * hit / total if total else 100.0

    return {pkg: (pct(lines_hit[pkg], lines_total[pkg]), pct(br_hit[pkg], br_total[pkg])) for pkg in lines_total}


def main(path: str = "coverage.json") -> int:
    report = json.loads((REPO / path).read_text())
    measured = package_coverage(report)
    failures = [
        f"{pkg}: not measured (add it to [tool.coverage.run] source_pkgs)"
        for pkg in sorted(workspace_packages() - set(measured))
    ]
    for pkg, (line_pct, branch_pct) in sorted(measured.items()):
        floor = STRICT_FLOOR if pkg in STRICT else DEFAULT_FLOOR
        ok = line_pct >= floor and branch_pct >= floor
        sys.stdout.write(
            f"{pkg:<16} lines {line_pct:6.2f}%  branches {branch_pct:6.2f}%  (floor {floor:.0f}%)  "
            f"{'ok' if ok else 'FAIL'}\n"
        )
        if not ok:
            failures.append(f"{pkg}: lines {line_pct:.2f}% / branches {branch_pct:.2f}% < {floor:.0f}%")
    if failures:
        sys.stdout.write("per-package coverage gate FAILED:\n  " + "\n  ".join(failures) + "\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(*sys.argv[1:]))
