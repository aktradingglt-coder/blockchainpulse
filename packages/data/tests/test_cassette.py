import gzip
import json
from pathlib import Path

import httpx
import pytest

from rd_data.cassette import Cassette, Interaction, RecordingTransport, ReplayTransport, json_shape, shape_drift


def test_json_shape_and_drift() -> None:
    shape = json_shape({"a": 1, "b": [{"x": "s", "y": None}, {"x": "t", "z": True}], "c": None})
    assert shape == {"a": "number", "b": {"[]": {"x": "string", "y": "null", "z": "bool"}}, "c": "null"}
    assert shape_drift(shape, shape) == []
    changed = json_shape({"a": "1", "b": [{"x": "s"}]})
    drift = shape_drift(shape, changed)
    assert "$.a: 'number' became 'string'" in drift
    assert "$.c: missing" in drift
    assert json_shape([]) == {"[]": "empty"}
    assert json_shape([1, "a"])["[]"] == sorted(['"number"', '"string"'])


async def test_recording_redacts_credentials_and_handles_gzip(tmp_path: Path) -> None:
    payload = json.dumps({"rows": [1, 2]}).encode()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=gzip.compress(payload),
            headers={"content-type": "application/json", "content-encoding": "gzip"},
        )

    rec = RecordingTransport(httpx.MockTransport(handler))
    async with httpx.AsyncClient(transport=rec) as client:
        resp = await client.get("https://api.example.gov/v2/data", params={"api_key": "S3CR3T-KEY-VALUE", "q": "1"})
    assert resp.json() == {"rows": [1, 2]}  # decoded exactly once
    cassette = rec.to_cassette("example", "unit test")
    assert cassette.provenance == "recorded"
    path = tmp_path / "c.json"
    cassette.save(path)
    text = path.read_text()
    assert "S3CR3T-KEY-VALUE" not in text
    loaded = Cassette.load(path)
    assert loaded.interactions[0].params == {"api_key": "REDACTED", "q": "1"}
    assert loaded.interactions[0].shape == {"rows": {"[]": "number"}}

    replay = ReplayTransport([loaded])
    async with httpx.AsyncClient(transport=replay) as client:
        again = await client.get("https://api.example.gov/v2/data", params={"q": "1", "api_key": "different-key"})
        assert again.json() == {"rows": [1, 2]}  # credential value ignored when matching
        with pytest.raises(AssertionError, match="not in cassette"):
            await client.get("https://api.example.gov/v2/data", params={"q": "2"})
    assert replay.calls == ["GET https://api.example.gov/v2/data", "GET https://api.example.gov/v2/data"]


def test_interaction_roundtrip() -> None:
    i = Interaction("GET", "https://h/x", {"a": "1"}, 404, "text/plain", "nope")
    assert i.as_dict()["response"]["status"] == 404
    assert i.key() == ("GET", "https://h/x", (("a", "1"),))
