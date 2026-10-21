"""API behaviour that needs no running services (dependencies point at closed ports)."""

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from rd_api.app import create_app
from rd_core.bootstrap import Runtime
from rd_core.config import load_config
from rd_core.settings import AppEnv, Settings


def _runtime(**overrides: object) -> Runtime:
    settings = Settings(
        database_url="postgresql+asyncpg://nobody@127.0.0.1:1/none",
        redis_url="redis://127.0.0.1:1/0",
        cors_allowed_origins=["https://app.example.com"],
        **overrides,  # type: ignore[arg-type]  # test-only passthrough of typed fields
    )
    return Runtime(settings=settings, config=load_config(settings.config_dir))


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(create_app(_runtime())) as c:
        yield c


def test_healthz_and_security_headers(client: TestClient) -> None:
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}
    assert r.headers["content-security-policy"].startswith("default-src 'none'")
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["x-frame-options"] == "DENY"
    assert "strict-transport-security" not in r.headers  # dev
    assert len(r.headers["x-request-id"]) == 32


def test_request_id_echo_and_sanitising(client: TestClient) -> None:
    assert client.get("/healthz", headers={"x-request-id": "abc-123-XYZ_9"}).headers["x-request-id"] == "abc-123-XYZ_9"
    bad = client.get("/healthz", headers={"x-request-id": "<script>"}).headers["x-request-id"]
    assert bad != "<script>"
    assert len(bad) == 32


def test_readyz_reports_unavailable_dependencies(client: TestClient) -> None:
    r = client.get("/readyz")
    assert r.status_code == 503
    body = r.json()
    assert body["status"] == "not_ready"
    assert body["checks"]["database"]["ok"] is False
    assert body["checks"]["redis"]["ok"] is False


def test_status_reports_unavailable_components(client: TestClient) -> None:
    body = client.get("/v1/status").json()
    assert body["ok"] is False
    assert body["components"]["rd-worker"]["ok"] is False
    assert body["components"]["rd-scheduler"]["leader_elected"] is False
    assert body["components"]["queue"]["ok"] is False


def test_metrics_count_requests_by_route_template(client: TestClient) -> None:
    client.get("/healthz")
    client.get("/no/such/path")
    text = client.get("/metrics").text
    assert 'rd_http_requests_total{method="GET",route="/healthz",status="200"}' in text
    assert 'route="unmatched"' in text


def test_meta_reads_product_config(client: TestClient) -> None:
    body = client.get("/v1/meta").json()
    assert body["product_name"] == "Research Desk (working name)"
    assert body["working_identity"] is True
    assert body["trial_days"] == 7


def test_cors_allows_only_configured_origins(client: TestClient) -> None:
    pre = {"access-control-request-method": "GET"}
    ok = client.options("/v1/meta", headers={"origin": "https://app.example.com", **pre})
    assert ok.headers.get("access-control-allow-origin") == "https://app.example.com"
    denied = client.options("/v1/meta", headers={"origin": "https://evil.example.net", **pre})
    assert "access-control-allow-origin" not in denied.headers


def test_production_hides_docs_and_sets_hsts() -> None:
    with TestClient(create_app(_runtime(app_env=AppEnv.PRODUCTION))) as c:
        assert c.get("/docs").status_code == 404
        assert c.get("/openapi.json").status_code == 404
        assert "max-age=" in c.get("/healthz").headers["strict-transport-security"]
