import json
from pathlib import Path

import httpx
import pytest

from rd_data import recording
from rd_data.cassette import Cassette, RecordingTransport


def _patch_network(monkeypatch: pytest.MonkeyPatch, body: object) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=body)

    class OfflineRecording(RecordingTransport):
        def __init__(self) -> None:
            super().__init__(httpx.MockTransport(handler))

    monkeypatch.setattr(recording, "RecordingTransport", OfflineRecording)


CASSETTES = Path(__file__).parent / "cassettes"


def _recorded_tff_rows(n: int) -> list[dict[str, str]]:
    """The newest rows of the real recorded EUR FX TFF response (cassettes/cftc)."""
    for path in sorted((CASSETTES / "cftc").glob("*.json")):
        interaction = Cassette.load(path).interactions[0]
        if "gpe5-46if" in interaction.url:
            rows: list[dict[str, str]] = json.loads(interaction.body)
            return rows[:n]
    raise AssertionError("recorded TFF cassette missing")


def test_record_then_check_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    rows = _recorded_tff_rows(2)
    _patch_network(monkeypatch, rows)
    code = recording.main(["--providers", "cftc", "--out", str(tmp_path), "--source", "unit test"])
    out = capsys.readouterr().out
    summaries = [
        json.loads(line.removeprefix("RECORDING ")) for line in out.splitlines() if line.startswith("RECORDING {")
    ]
    assert len(summaries) == 3  # the CFTC recording plan
    assert all(s["provider"] == "cftc" for s in summaries)
    written = sorted(tmp_path.glob("cftc/*.json"))
    assert len(written) == 3
    assert Cassette.load(written[0]).provenance == "recorded"
    assert code in (0, 1)  # the synthetic rows only fit one market; others fail QA or market checks

    # Drift: same shape -> no drift entries; changed type -> drift reported and non-zero exit
    _patch_network(monkeypatch, rows)
    recording.main(["--providers", "cftc", "--out", str(tmp_path), "--check-drift"])
    out = capsys.readouterr().out
    drift_lists = [
        json.loads(line.removeprefix("RECORDING "))["drift"]
        for line in out.splitlines()
        if line.startswith("RECORDING {")
    ]
    assert drift_lists == [[], [], []]
    changed = [{**r, "open_interest_all": 1000} for r in rows]  # string -> number
    _patch_network(monkeypatch, changed)
    assert recording.main(["--providers", "cftc", "--out", str(tmp_path), "--check-drift"]) == 1
    out = capsys.readouterr().out
    assert "open_interest_all" in out


def test_missing_cassette_and_unknown_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _patch_network(monkeypatch, [])
    assert recording.main(["--providers", "us_treasury", "--out", str(tmp_path), "--check-drift"]) == 1
    assert "no stored cassette" in capsys.readouterr().out
    with pytest.raises(SystemExit, match="unknown provider"):
        recording.main(["--providers", "nope", "--out", str(tmp_path)])


def test_cassette_name_is_stable() -> None:
    from rd_data.adapters.base import DataRequest, RequestKind  # noqa: PLC0415

    a = DataRequest(kind=RequestKind.COT, params={"b": 1, "a": 2})
    b = DataRequest(kind=RequestKind.COT, params={"a": 2, "b": 1})
    assert recording.cassette_name(a) == recording.cassette_name(b)
    assert recording.cassette_name(a).startswith("cot-")
