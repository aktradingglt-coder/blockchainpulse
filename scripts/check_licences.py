"""Dependency licence gate (PROMPT §15.1). Fails on any licence not on the allow-list unless explicitly excepted.

Usage: ``uv run python scripts/check_licences.py`` (reads pip-licenses and pnpm output itself).
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

# Permissive or weak-copyleft-file-level licences acceptable for a proprietary hosted and self-hosted product.
ALLOWED = {
    "mit",
    "mit license",
    "apache-2.0",
    "apache software license",
    "apache license 2.0",
    "bsd",
    "bsd license",
    "bsd-2-clause",
    "bsd-3-clause",
    "3-clause bsd license",
    "isc",
    "isc license (iscl)",
    "0bsd",
    "psf-2.0",
    "python software foundation license",
    "python-2.0",
    "mpl-2.0",
    "mozilla public license 2.0 (mpl 2.0)",
    "cc0-1.0",
    "blueoak-1.0.0",
    "unlicense",
    "the unlicense (unlicense)",
}
# Data-only licences permitted for named packages (attribution shipped in the licence bundle).
DATA_EXCEPTIONS = {"caniuse-lite": "cc-by-4.0"}
# Reviewed weak-copyleft exceptions: used unmodified as separately replaceable libraries (D-016).
LGPL_EXCEPTIONS = {
    "dramatiq",
    "@img/sharp-libvips-linux-x64",
    "@img/sharp-libvips-linuxmusl-x64",
    "@img/sharp-libvips-linux-arm64",
    "@img/sharp-libvips-linuxmusl-arm64",
}
OWN_PREFIXES = ("rd-", "research-desk", "@research-desk/")


def _terms(expr: str) -> list[str]:
    """Split an SPDX-ish expression into licence names; ``A OR B`` is acceptable if any branch is."""
    return [t.strip().strip("()").lower() for t in re.split(r";|\bAND\b", expr) if t.strip()]


def _acceptable(name: str, expr: str) -> bool:
    if name.startswith(OWN_PREFIXES):
        return True
    if name in LGPL_EXCEPTIONS and "lgpl" in expr.lower().replace("lesser general public", "lgpl"):
        return True
    if DATA_EXCEPTIONS.get(name) == expr.lower() or expr.strip().lower() in ALLOWED:
        return True
    for term in _terms(expr):
        options = [o.strip().strip("()") for o in re.split(r"\bOR\b", term, flags=re.IGNORECASE)]
        if not any(o in ALLOWED for o in options):
            return False
    return True


def python_licences() -> list[tuple[str, str]]:
    out = subprocess.run(
        [sys.executable, "-m", "piplicenses", "--format=json", "--with-system"],
        capture_output=True,
        text=True,
        check=True,
        cwd=REPO,
    ).stdout
    return [(d["Name"].lower(), d["License"]) for d in json.loads(out)]


def web_licences() -> list[tuple[str, str]]:
    out = subprocess.run(
        ["pnpm", "--dir", "apps/web", "licenses", "list", "--json"],  # noqa: S607
        capture_output=True,
        text=True,
        check=True,
        cwd=REPO,
    ).stdout
    data: dict[str, list[dict[str, str]]] = json.loads(out)
    return [(pkg["name"], licence) for licence, pkgs in data.items() for pkg in pkgs]


def main() -> int:
    rows = [("python", n, lic) for n, lic in python_licences()] + [("web", n, lic) for n, lic in web_licences()]
    bad = [f"{eco}: {n} ({lic})" for eco, n, lic in rows if not _acceptable(n, lic)]
    sys.stdout.write(f"checked {len(rows)} packages\n")
    if bad:
        sys.stdout.write("licence check FAILED:\n  " + "\n  ".join(sorted(bad)) + "\n")
        return 1
    sys.stdout.write("licence check ok\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
