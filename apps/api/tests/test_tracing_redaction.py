"""Spans exported from the real app never carry query strings or secrets (P0 review round 3, N1)."""

from fastapi.testclient import TestClient
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from rd_api.app import create_app
from rd_core import tracing
from rd_core.bootstrap import Runtime
from rd_core.config import load_config
from rd_core.settings import Settings


def test_exported_spans_have_no_query_strings_or_secrets() -> None:
    settings = Settings(database_url="postgresql+asyncpg://nobody@127.0.0.1:1/none", redis_url="redis://127.0.0.1:1/0")
    exporter = InMemorySpanExporter()
    provider = tracing.configure_tracing(settings, service="rd-api", exporter=exporter)
    secret = "SpanSecret" + "Zq8xVw3pL"
    tok = "Tok" + "123456789"
    with TestClient(create_app(Runtime(settings=settings, config=load_config(settings.config_dir)))) as client:
        assert client.get(f"/v1/meta?api_key={secret}&token={tok}").status_code == 200
    provider.force_flush()
    spans = exporter.get_finished_spans()
    assert spans, "instrumentation produced no spans"
    dumped = repr([(s.name, dict(s.attributes or {}), [dict(e.attributes or {}) for e in s.events]) for s in spans])
    assert secret not in dumped
    assert tok not in dumped
    assert "api_key=" not in dumped
    urls = [str(s.attributes.get("http.url") or s.attributes.get("url.full") or "") for s in spans if s.attributes]
    assert any(u.endswith("/v1/meta") for u in urls), urls


def test_sanitize_attributes_rules() -> None:
    out = tracing.sanitize_attributes(
        {
            "http.target": "/v1/x?token=abc",
            "url.query": "token=abc",
            "db.password": "pw",
            "http.status_code": 200,
            "note": "Authorization: Bearer " + "q" * 20,
            "tags": ["ok", "sk-" + "ant-" + "z" * 20],
        }
    )
    assert out["http.target"] == "/v1/x"
    assert "url.query" not in out
    assert out["db.password"] == "[REDACTED]"
    assert out["http.status_code"] == 200
    assert "q" * 20 not in out["note"]
    assert out["tags"][0] == "ok"
    assert "z" * 20 not in out["tags"][1]
