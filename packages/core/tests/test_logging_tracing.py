import json
import logging

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from rd_core import tracing
from rd_core.logging import configure_logging, get_logger
from rd_core.settings import Settings


def test_json_logs_are_redacted_and_carry_trace_ids(capsys: pytest.CaptureFixture[str]) -> None:
    exporter = InMemorySpanExporter()
    provider = tracing.configure_tracing(Settings(), service="rd-test", exporter=exporter)
    configure_logging(service="rd-test", level="INFO", fmt="json")
    secret = "sk-" + "ant-" + "z" * 30
    with tracing.get_tracer("test").start_as_current_span("unit"):
        get_logger("t").info("calling.provider", api_key=secret, url=f"https://x.example.com/?token={secret}")
    provider.force_flush()
    line = capsys.readouterr().out.strip().splitlines()[-1]
    record = json.loads(line)
    assert secret not in line
    assert record["event"] == "calling.provider"
    assert record["service"] == "rd-test"
    assert len(record["trace_id"]) == 32
    assert [s.name for s in exporter.get_finished_spans()] == ["unit"]


def test_stdlib_logging_goes_through_redaction(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(service="rd-test", level="INFO", fmt="json")
    secret = "gh" + "p_" + "b" * 36
    logging.getLogger("third.party").warning("token is %s", secret)
    out = capsys.readouterr().out
    assert secret not in out
    assert "third.party" in out or "token is" in out


def test_uncaught_exceptions_are_logged_as_redacted_json(capsys: pytest.CaptureFixture[str]) -> None:
    import sys  # noqa: PLC0415
    import threading  # noqa: PLC0415

    configure_logging(service="rd-test", level="INFO", fmt="json")
    secret = "gh" + "p_" + "c" * 36
    exc = RuntimeError(f"boom with {secret}")
    sys.excepthook(type(exc), exc, None)
    t = threading.Thread(target=lambda: (_ for _ in ()).throw(ValueError(f"thread {secret}")), name="probe")
    t.start()
    t.join()
    lines = [json.loads(line) for line in capsys.readouterr().out.strip().splitlines()]
    events = {line["event"] for line in lines}
    assert {"process.uncaught_exception", "thread.uncaught_exception"} <= events
    assert secret not in json.dumps(lines)
