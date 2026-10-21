import json
from pathlib import Path

import httpx
import pytest

from rd_data import recording
from rd_data.adapters.cftc import CftcAdapter
from rd_data.cassette import Cassette, RecordingTransport
from rd_data.registry import ADAPTERS


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
    failed_dir = tmp_path / "failed"
    code = recording.main(
        ["--providers", "cftc", "--out", str(tmp_path), "--source", "unit test", "--failed-out", str(failed_dir)]
    )
    out = capsys.readouterr().out
    summaries = [
        json.loads(line.removeprefix("RECORDING ")) for line in out.splitlines() if line.startswith("RECORDING {")
    ]
    assert len(summaries) == len(CftcAdapter.recording_plan())
    assert all(s["provider"] == "cftc" for s in summaries)
    ok = [s for s in summaries if s["ok"]]
    assert {s["params"]["report_type"] for s in ok} == {"tff"}  # the EUR FX rows answer only the TFF requests
    written = sorted(tmp_path.glob("cftc/*.json"))
    assert len(written) == len(ok)  # a failed fetch never becomes (or replaces) a contract cassette
    assert Cassette.load(written[0]).provenance == "recorded"
    assert len(list(failed_dir.glob("cftc/*.json"))) == len(summaries) - len(ok)
    assert code == 1

    # Drift: same shape -> no drift entries; changed type -> drift reported and non-zero exit
    _patch_network(monkeypatch, rows)
    recording.main(["--providers", "cftc", "--out", str(tmp_path), "--check-drift"])
    out = capsys.readouterr().out
    drift_lists = [
        json.loads(line.removeprefix("RECORDING "))["drift"]
        for line in out.splitlines()
        if line.startswith("RECORDING {")
    ]
    assert drift_lists.count([]) == len(ok)  # the others have no stored cassette to compare against
    assert drift_lists.count(["no stored cassette"]) == len(summaries) - len(ok)
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


def test_extra_requests_are_recorded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Requests listed in the recording-request file are recorded on top of the providers' plans."""
    _patch_network(monkeypatch, _recorded_tff_rows(2))
    request_file = tmp_path / "request.json"
    extra = {"provider": "cftc", "kind": "cot", "params": {"report_type": "tff", "market_code": "099741"}}
    request_file.write_text(json.dumps({"providers": [], "requests": [extra]}))
    out = tmp_path / "out"
    recording.main(["--providers", "", "--out", str(out), "--requests", str(request_file)])
    assert [p.name for p in (out / "cftc").glob("*.json")] == [
        recording.cassette_name(recording.load_extra_requests(request_file)[0][1])
    ]
    request_file.write_text(json.dumps({"requests": [{**extra, "provider": "nope"}]}))
    with pytest.raises(SystemExit, match="nope"):
        recording.main(["--providers", "", "--out", str(out), "--requests", str(request_file)])


def test_repository_recording_request_is_valid() -> None:
    """The committed request file parses and names only known providers (CI would fail otherwise)."""
    path = Path(__file__).resolve().parents[3] / ".github" / "recording-request.json"
    extra = recording.load_extra_requests(path)
    names = {cls.name for cls in ADAPTERS}
    assert {name for name, _ in extra} <= names
    assert set(json.loads(path.read_text())["providers"]) <= names
