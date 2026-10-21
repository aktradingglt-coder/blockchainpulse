import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "check_licences", Path(__file__).resolve().parents[1] / "scripts/check_licences.py"
)
assert _spec is not None
assert _spec.loader is not None
check_licences = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check_licences)


def test_permissive_and_compound_expressions_pass() -> None:
    ok = check_licences._acceptable
    assert ok("httpx", "BSD-3-Clause")
    assert ok("orjson", "MPL-2.0 AND (Apache-2.0 OR MIT)")
    assert ok("x", "Apache Software License; MIT License")
    assert ok("certifi", "Mozilla Public License 2.0 (MPL 2.0)")
    assert ok("rd-core", "UNKNOWN")


def test_copyleft_and_unknown_fail_unless_excepted() -> None:
    ok = check_licences._acceptable
    assert not ok("somepkg", "GPL-3.0-only")
    assert not ok("somepkg", "AGPL-3.0")
    assert not ok("somepkg", "SSPL-1.0")
    assert not ok("somepkg", "UNKNOWN")
    assert not ok("otherlib", "GNU Lesser General Public License v3 or later (LGPLv3+)")
    assert ok("dramatiq", "GNU Lesser General Public License v3 or later (LGPLv3+)")
    assert ok("caniuse-lite", "CC-BY-4.0")
    assert not ok("random-data", "CC-BY-4.0")
