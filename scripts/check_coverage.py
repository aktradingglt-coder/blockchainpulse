"""Per-package coverage gate (PROMPT §15.2). Reads ``coverage.json`` written by ``make coverage``.

Line+branch coverage per top-level ``rd_*`` package must reach its floor: 90% for the packages §15.2 names,
80% for everything else.
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


def package_coverage(report: dict[str, Any]) -> dict[str, float]:
    files: dict[str, Any] = report["files"]
    hit: dict[str, int] = defaultdict(int)
    total: dict[str, int] = defaultdict(int)
    for path, data in files.items():
        m = _PKG.search("/" + path.replace("\\", "/"))
        if m is None:
            continue
        summary = data["summary"]
        pkg = m.group(1)
        hit[pkg] += summary["covered_lines"] + summary.get("covered_branches", 0)
        total[pkg] += summary["num_statements"] + summary.get("num_branches", 0)
    return {pkg: (100.0 * hit[pkg] / total[pkg]) if total[pkg] else 100.0 for pkg in total}


def main(path: str = "coverage.json") -> int:
    report = json.loads((REPO / path).read_text())
    failures = []
    for pkg, pct in sorted(package_coverage(report).items()):
        floor = STRICT_FLOOR if pkg in STRICT else DEFAULT_FLOOR
        status = "ok" if pct >= floor else "FAIL"
        sys.stdout.write(f"{pkg:<16} {pct:6.2f}%  (floor {floor:.0f}%)  {status}\n")
        if pct < floor:
            failures.append(pkg)
    if failures:
        sys.stdout.write(f"per-package coverage gate FAILED: {', '.join(failures)}\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(*sys.argv[1:]))
