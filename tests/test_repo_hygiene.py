"""Repository-wide gates: pinning, no placeholders in shipped code, documented env vars (PROMPT §1.3, §1.10, §19)."""

import json
import re
import subprocess
import tomllib
from pathlib import Path

import yaml

from rd_core.settings import Settings

REPO = Path(__file__).resolve().parents[1]
SHIPPED_DIRS = ("packages", "apps", "infra", "config", "scripts")
SKIP_PARTS = {"node_modules", ".next", "generated", "__pycache__", "tests"}
SUFFIXES = {".py", ".ts", ".tsx", ".mjs", ".js", ".yaml", ".yml", ".toml", ".sh", ".css", ".json", ".mako"}


def _committable_files() -> list[Path]:
    out = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard"],  # noqa: S607
        cwd=REPO,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    return [REPO / f for f in out if (REPO / f).is_file()]


def _shipped_sources() -> list[Path]:
    return [
        p
        for p in _committable_files()
        if p.relative_to(REPO).parts[0] in SHIPPED_DIRS and p.suffix in SUFFIXES and not SKIP_PARTS & set(p.parts)
    ]


def test_no_todo_or_fixme_in_shipped_code() -> None:
    marker = re.compile(r"\b(TODO|FIXME|XXX|HACK)\b")
    hits = [
        f"{p.relative_to(REPO)}:{i}"
        for p in _shipped_sources()
        for i, line in enumerate(p.read_text().splitlines(), 1)
        if marker.search(line)
    ]
    assert not hits, hits


def test_type_ignores_carry_a_code_and_reason() -> None:
    bad = []
    for p in _committable_files():
        if p.suffix != ".py" or p == Path(__file__).resolve():
            continue
        for i, line in enumerate(p.read_text().splitlines(), 1):
            if "type: ignore" in line and not re.search(r"type: ignore\[[a-z\-, ]+\]\s+#\s*\S", line):
                bad.append(f"{p.relative_to(REPO)}:{i}")
    assert not bad, bad


def test_python_dependencies_pinned_exactly() -> None:
    loose = []
    for py in REPO.glob("**/pyproject.toml"):
        if ".venv" in py.parts or "node_modules" in py.parts:
            continue
        data = tomllib.loads(py.read_text())
        deps = list(data.get("project", {}).get("dependencies", []))
        for group in data.get("dependency-groups", {}).values():
            deps.extend(group)
        for dep in deps:
            name = re.split(r"[\[=<>~! ]", dep, maxsplit=1)[0]
            if name.startswith("rd-") or name.startswith("research-desk"):
                continue  # workspace members
            if "==" not in dep:
                loose.append(f"{py.relative_to(REPO)}: {dep}")
    assert not loose, loose


def test_web_dependencies_pinned_exactly() -> None:
    pkg = json.loads((REPO / "apps/web/package.json").read_text())
    loose = [
        f"{k}@{v}"
        for section in ("dependencies", "devDependencies")
        for k, v in pkg.get(section, {}).items()
        if not re.fullmatch(r"\d+\.\d+\.\d+", v)
    ]
    assert not loose, loose
    assert re.fullmatch(r"pnpm@\d+\.\d+\.\d+\+sha512\.[0-9a-f]{128}", pkg["packageManager"]), "pin pnpm with integrity"
    assert (REPO / "apps/web/pnpm-lock.yaml").is_file()
    assert (REPO / "uv.lock").is_file()


def test_container_images_pinned_by_digest() -> None:
    compose = yaml.safe_load((REPO / "infra/compose/compose.yaml").read_text())
    for name, svc in compose["services"].items():
        image = svc.get("image", "")
        if "build" in svc:
            continue  # built locally from Dockerfiles checked below
        assert re.search(r"@sha256:[0-9a-f]{64}$", image), f"{name}: {image}"
    for dockerfile in (REPO / "infra/docker").glob("*.Dockerfile"):
        for line in dockerfile.read_text().splitlines():
            if line.startswith("ARG") and "_IMAGE=" in line:
                assert re.search(r"@sha256:[0-9a-f]{64}$", line), f"{dockerfile.name}: {line}"


def test_github_actions_pinned_by_commit_sha() -> None:
    unpinned = []
    for wf in (REPO / ".github/workflows").glob("*.yml"):
        for i, line in enumerate(wf.read_text().splitlines(), 1):
            m = re.search(r"uses:\s*([^\s#]+)", line)
            if m and not m.group(1).startswith("./") and not re.search(r"@[0-9a-f]{40}$", m.group(1)):
                unpinned.append(f"{wf.name}:{i} {m.group(1)}")
    assert not unpinned, unpinned


def test_env_example_documents_every_setting() -> None:
    documented = {
        line.split("=", 1)[0].strip()
        for line in (REPO / ".env.example").read_text().splitlines()
        if "=" in line and not line.lstrip().startswith("#")
    }
    expected = {name.upper() for name in Settings.model_fields}
    assert expected <= documented, sorted(expected - documented)
    values = [
        line.split("=", 1)[1].split("#", 1)[0].strip()
        for line in (REPO / ".env.example").read_text().splitlines()
        if "=" in line and not line.lstrip().startswith("#")
    ]
    assert all(v == "" for v in values), "the example file must contain names only"


def test_no_third_party_identity_in_config() -> None:
    product = yaml.safe_load((REPO / "config/product.yaml").read_text())
    identity = product["identity"]
    assert identity["primary_domain"] == "example.com"
    assert identity["support_email"].endswith("@example.com")
    assert "working name" in identity["product_name"]


def test_dockerfile_frontends_and_python_patch_version_pinned() -> None:
    python_version = (REPO / ".python-version").read_text().strip()
    assert re.fullmatch(r"3\.12\.\d+", python_version), "pin the interpreter to a patch version"
    for dockerfile in (REPO / "infra/docker").glob("*.Dockerfile"):
        first = dockerfile.read_text().splitlines()[0]
        assert re.match(r"# syntax=\S+@sha256:[0-9a-f]{64}$", first), f"{dockerfile.name}: {first}"
    py = (REPO / "infra/docker/python.Dockerfile").read_text()
    assert f"python:{python_version}-" in py, "image Python must match .python-version"
    assert "--require-hashes" in py
    ci = (REPO / ".github/workflows/ci.yml").read_text()
    assert f'PYTHON_VERSION: "{python_version}"' in ci


def test_gitleaks_ignores_are_commit_scoped_and_explained() -> None:
    lines = (REPO / ".gitleaksignore").read_text().splitlines()
    entries = [ln for ln in lines if ln.strip() and not ln.startswith("#")]
    assert any(ln.startswith("#") for ln in lines), "every ignore block needs a reason comment"
    for entry in entries:
        assert re.fullmatch(r"[0-9a-f]{40}:[^:]+:[a-z0-9\-]+:\d+", entry), (
            f"ignore must name commit:file:rule:line: {entry}"
        )
