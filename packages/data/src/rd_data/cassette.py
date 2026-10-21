"""Cassettes: recorded provider responses for contract tests (PROMPT §15.2), plus schema-drift detection.

A cassette is JSON::

    {"provider": "cftc", "provenance": "recorded" | "schema-derived", "recorded_at": "...", "source": "...",
     "interactions": [{"request": {"method": "GET", "url": "https://host/path", "params": {...}},
                       "response": {"status": 200, "content_type": "application/json", "body": "...",
                                    "shape": {...}}}]}

``provenance`` is honest by construction: only :class:`RecordingTransport` writes ``recorded``. Hand-built fixtures
(for vendors we cannot call without a contract) must say ``schema-derived`` (D-011). Credentials in query strings
are replaced by ``REDACTED`` before anything is written, and request matching ignores their values.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from urllib.parse import parse_qsl, urlsplit, urlunsplit

import httpx

from rd_data.http import redact_params

Provenance = Literal["recorded", "schema-derived"]


def json_shape(value: Any, *, depth: int = 0) -> Any:  # noqa: ANN401, PLR0911 - one return per JSON type
    """Structural signature of JSON: object keys with value shapes, lists by the union of element shapes."""
    if depth > 12:
        return "…"
    if isinstance(value, Mapping):
        return {k: json_shape(v, depth=depth + 1) for k, v in sorted(value.items())}
    if isinstance(value, list):
        shapes: list[Any] = []
        for item in value[:50]:
            s = json_shape(item, depth=depth + 1)
            if s not in shapes:
                shapes.append(s)
        return {"[]": _merge_shapes(shapes)}
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, (int, float)):
        return "number"
    if value is None:
        return "null"
    return "string"


def _merge_shapes(shapes: list[Any]) -> Any:  # noqa: ANN401
    if not shapes:
        return "empty"
    if all(isinstance(s, dict) and "[]" not in s for s in shapes):
        merged: dict[str, Any] = {}
        for s in shapes:
            for k, v in s.items():
                merged.setdefault(k, v)
        return merged
    return shapes[0] if len(shapes) == 1 else sorted(json.dumps(s, sort_keys=True) for s in shapes)


def shape_drift(expected: Any, actual: Any, path: str = "$") -> list[str]:  # noqa: ANN401
    """Differences that break parsers: missing keys and changed types (added keys are tolerated)."""
    if isinstance(expected, dict) and isinstance(actual, dict):
        out: list[str] = []
        for key, sub in expected.items():
            if key not in actual:
                out.append(f"{path}.{key}: missing")
            else:
                out.extend(shape_drift(sub, actual[key], f"{path}.{key}"))
        return out
    if expected in ("null", "empty") or actual in ("null", "empty"):
        return []
    if expected != actual and not (isinstance(expected, list) or isinstance(actual, list)):
        return [f"{path}: {expected!r} became {actual!r}"]
    return []


def _norm_query(query: str) -> tuple[tuple[str, str], ...]:
    pairs = redact_params(dict(parse_qsl(query, keep_blank_values=True)))
    return tuple(sorted(pairs.items()))


@dataclass
class Interaction:
    method: str
    url: str
    params: dict[str, str]
    status: int
    content_type: str
    body: str
    shape: Any = None

    def key(self) -> tuple[str, str, tuple[tuple[str, str], ...]]:
        return (self.method.upper(), self.url, tuple(sorted(self.params.items())))

    def as_dict(self) -> dict[str, Any]:
        return {
            "request": {"method": self.method, "url": self.url, "params": self.params},
            "response": {
                "status": self.status,
                "content_type": self.content_type,
                "body": self.body,
                "shape": self.shape,
            },
        }


@dataclass
class Cassette:
    provider: str
    provenance: Provenance
    recorded_at: str
    source: str
    interactions: list[Interaction] = field(default_factory=list)

    @classmethod
    def load(cls, path: Path) -> Cassette:
        data = json.loads(path.read_text())
        items = [
            Interaction(
                i["request"]["method"],
                i["request"]["url"],
                dict(i["request"].get("params", {})),
                i["response"]["status"],
                i["response"].get("content_type", "application/json"),
                i["response"]["body"],
                i["response"].get("shape"),
            )
            for i in data["interactions"]
        ]
        return cls(data["provider"], data["provenance"], data["recorded_at"], data["source"], items)

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "provider": self.provider,
            "provenance": self.provenance,
            "recorded_at": self.recorded_at,
            "source": self.source,
            "interactions": [i.as_dict() for i in self.interactions],
        }
        # Bodies are stored verbatim (fidelity); credentials never reach them because request parameters are
        # redacted above and public providers do not echo keys. The committed files are covered by gitleaks.
        path.write_text(json.dumps(payload, indent=1, sort_keys=False, ensure_ascii=False) + "\n")


def _split(request: httpx.Request) -> tuple[str, str, dict[str, str]]:
    parts = urlsplit(str(request.url))
    base = urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
    return request.method.upper(), base, dict(_norm_query(parts.query))


class ReplayTransport(httpx.AsyncBaseTransport):
    """Serves recorded responses; any request not in the cassette fails the test loudly."""

    def __init__(self, cassettes: Sequence[Cassette]) -> None:
        self._index: dict[tuple[str, str, tuple[tuple[str, str], ...]], Interaction] = {}
        for c in cassettes:
            for i in c.interactions:
                self._index[i.key()] = i
        self.calls: list[str] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        method, url, params = _split(request)
        key = (method, url, tuple(sorted(params.items())))
        self.calls.append(f"{method} {url}")
        hit = self._index.get(key)
        if hit is None:
            raise AssertionError(f"request not in cassette: {method} {url} {params}")
        return httpx.Response(
            hit.status, content=hit.body.encode(), headers={"content-type": hit.content_type}, request=request
        )


class RecordingTransport(httpx.AsyncBaseTransport):
    """Passes requests to the network and keeps a redacted copy of each exchange."""

    def __init__(self, inner: httpx.AsyncBaseTransport | None = None) -> None:
        self._inner = inner or httpx.AsyncHTTPTransport(retries=0)
        self.interactions: list[Interaction] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        response = await self._inner.handle_async_request(request)
        body = await response.aread()
        method, url, params = _split(request)
        content_type = response.headers.get("content-type", "application/octet-stream").split(";")[0]
        text = body.decode("utf-8", errors="replace")
        shape: Any = None
        if "json" in content_type:
            try:
                shape = json_shape(json.loads(text))
            except json.JSONDecodeError:
                shape = None
        self.interactions.append(Interaction(method, url, params, response.status_code, content_type, text, shape))
        # ``body`` is already decoded: drop transfer/encoding headers so httpx does not decode it a second time.
        headers = [
            (k, v)
            for k, v in response.headers.multi_items()
            if k.lower() not in ("content-encoding", "content-length", "transfer-encoding")
        ]
        return httpx.Response(response.status_code, content=body, headers=headers, request=request)

    def to_cassette(self, provider: str, source: str) -> Cassette:
        now = datetime.now(tz=UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        return Cassette(provider, "recorded", now, source, list(self.interactions))
