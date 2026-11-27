import importlib.util
from pathlib import Path
from types import ModuleType

from rd_worker.main import ACTORS_MODULE, build_args

REPO = Path(__file__).resolve().parents[1]


def _load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, REPO / "scripts" / f"{name}.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_package_coverage_separates_lines_and_branches() -> None:
    cov = _load("check_coverage")
    report = {
        "files": {
            "packages/core/src/rd_core/a.py": {
                "summary": {"covered_lines": 8, "num_statements": 10, "covered_branches": 1, "num_branches": 2}
            },
            "packages/core/src/rd_core/b.py": {
                "summary": {"covered_lines": 10, "num_statements": 10, "covered_branches": 0, "num_branches": 0}
            },
            "apps/worker/src/rd_worker/c.py": {"summary": {"covered_lines": 0, "num_statements": 4}},
            "tests/test_x.py": {"summary": {"covered_lines": 1, "num_statements": 1}},
        }
    }
    result = cov.package_coverage(report)
    assert result["rd_core"] == (100.0 * 18 / 20, 50.0)
    assert result["rd_worker"] == (0.0, 100.0)
    assert set(result) == {"rd_core", "rd_worker"}
    assert "rd_quant" in cov.STRICT
    assert {"rd_core", "rd_db", "rd_api", "rd_worker", "rd_scheduler"} <= cov.workspace_packages()


def test_worker_launcher_always_skips_dramatiq_logging() -> None:
    assert build_args(["--processes", "2"]) == [ACTORS_MODULE, "--skip-logging", "--processes", "2"]
