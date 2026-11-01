"""Contract for application logging without a root logger or log files."""

from __future__ import annotations

import io
import json
import logging
from uuid import uuid4

import pytest

from atlas_core.logging import configure_logging, get_logger, log_event


@pytest.fixture
def logger_name() -> str:
    return f"atlas-test-{uuid4().hex}"


def test_text_default_uses_stderr_without_root_or_stdout_changes(logger_name, capsys) -> None:
    root = logging.getLogger()
    original_level, original_handlers = root.level, list(root.handlers)
    logger = configure_logging(logger_name)
    assert logger is get_logger(logger_name)
    assert logger.propagate is False
    logger.debug("not shown")
    logger.info("hello\nworld")
    actual = capsys.readouterr()
    assert actual.out == ""
    assert "INFO " + logger_name + ": hello\\nworld" in actual.err
    assert "not shown" not in actual.err
    assert root.level == original_level and root.handlers == original_handlers


def test_json_context_and_named_event(monkeypatch, logger_name) -> None:
    sink = io.StringIO()
    monkeypatch.setenv("ATLAS_HOST_ID", "control01")
    monkeypatch.setenv("ATLAS_PROGRAM_NAME", "hermes")
    monkeypatch.setenv("ATLAS_COMMAND_NAME", "dns-check")
    monkeypatch.setenv("ATLAS_RUN_ID", "run-1")
    monkeypatch.setenv("ATLAS_PARENT_RUN_ID", "parent-1")
    monkeypatch.setenv("ATLAS_OPERATION_ID", "op-1")
    logger = configure_logging(logger_name, format="json", stream=sink, level="debug")
    log_event(logger, "dns.zone.checked", message="ゾーン正常\nok", fields={
        "zone": "internal", "records": 2, "ok": True, "latency_ms": 2.5, "detail": None,
    })
    result = json.loads(sink.getvalue().strip())
    assert result == {
        "schema": "atlas.application-log/v1",
        "timestamp": result["timestamp"],
        "level": "INFO",
        "logger": logger_name,
        "message": "ゾーン正常\nok",
        "host_id": "control01",
        "program": "hermes",
        "command": "dns-check",
        "run_id": "run-1",
        "parent_run_id": "parent-1",
        "operation_id": "op-1",
        "event": "dns.zone.checked",
        "fields": {
            "zone": "internal", "records": 2, "ok": True, "latency_ms": 2.5, "detail": None,
        },
    }
    assert result["timestamp"].endswith("Z")


def test_text_event_fields_and_current_context(monkeypatch, logger_name) -> None:
    sink = io.StringIO()
    monkeypatch.setenv("ATLAS_RUN_ID", "run-abc")
    monkeypatch.setenv("ATLAS_OPERATION_ID", "op-abc")
    monkeypatch.setenv("ATLAS_HOST_ID", "host-abc")
    logger = configure_logging(logger_name, stream=sink, level=logging.WARNING)
    log_event(logger, "daemon.started", level=logging.WARNING, fields={"port": 4242, "active": True})
    assert 'event="daemon.started"' in sink.getvalue()
    assert "active=true" in sink.getvalue()
    assert "port=4242" in sink.getvalue()
    assert 'run_id="run-abc"' in sink.getvalue()
    assert 'operation_id="op-abc"' in sink.getvalue()
    assert 'host_id="host-abc"' in sink.getvalue()


def test_reconfigure_keeps_unmanaged_handlers_and_one_managed_handler(logger_name) -> None:
    original = io.StringIO()
    later = io.StringIO()
    unmanaged = io.StringIO()
    logger = get_logger(logger_name)
    own_handler = logging.StreamHandler(unmanaged)
    logger.addHandler(own_handler)
    first = configure_logging(logger_name, stream=original)
    second = configure_logging(logger_name, stream=later, format="json")
    assert first is second
    first.info("one")
    assert original.getvalue() == ""
    assert json.loads(later.getvalue())["message"] == "one"
    assert "one" in unmanaged.getvalue()
    assert len(logger.handlers) == 2
    logger.removeHandler(own_handler)


def test_child_logger_uses_configured_parent(logger_name) -> None:
    sink = io.StringIO()
    configure_logging(logger_name, stream=sink)
    get_logger(logger_name + ".child").info("child message")
    assert logger_name + ".child: child message" in sink.getvalue()


def test_missing_context_and_exception(monkeypatch, logger_name) -> None:
    for key in (
        "ATLAS_HOST_ID", "ATLAS_PROGRAM_NAME", "ATLAS_COMMAND_NAME",
        "ATLAS_RUN_ID", "ATLAS_PARENT_RUN_ID", "ATLAS_OPERATION_ID",
    ):
        monkeypatch.delenv(key, raising=False)
    sink = io.StringIO()
    logger = configure_logging(logger_name, stream=sink, format="json")
    try:
        raise ValueError("synthetic detail")
    except ValueError:
        logger.exception("request failed")
    result = json.loads(sink.getvalue())
    assert "ValueError: synthetic detail" in result["exception"]
    assert "run_id" not in result and "event" not in result and "fields" not in result


@pytest.mark.parametrize(
    ("name", "level", "format"),
    [
        ("", "INFO", "text"),
        ("root", "INFO", "text"),
        ("bad\nlogger", "INFO", "text"),
        (123, "INFO", "text"),
        ("tests", "INFO", "xml"),
        ("tests", "INFO", []),
        ("tests", "VERBOSE", "text"),
        ("tests", True, "text"),
        ("tests", -1, "text"),
        ("tests", 51, "text"),
    ],
)
def test_invalid_logging_configuration(name, level, format) -> None:
    with pytest.raises(ValueError):
        configure_logging(name, level=level, format=format)


@pytest.mark.parametrize(
    ("event", "fields"),
    [
        ("", None),
        ("Bad Event", None),
        (3, None),
        ("valid.event", []),
        ("valid.event", {"Bad Key": "value"}),
        ("valid.event", {1: "value"}),
        ("valid.event", {"context": []}),
        ("valid.event", {"latency": float("inf")}),
        ("valid.event", {"latency": float("nan")}),
    ],
)
def test_invalid_event_fields(logger_name, event, fields) -> None:
    logger = configure_logging(logger_name, stream=io.StringIO())
    with pytest.raises(ValueError):
        log_event(logger, event, fields=fields)


def test_text_exception_safely_one_line(logger_name) -> None:
    sink = io.StringIO()
    logger = configure_logging(logger_name, stream=sink)
    try:
        raise ValueError("test error")
    except ValueError:
        logger.exception("oops\r\nfailed")
    assert len(sink.getvalue().splitlines()) == 1
    assert "exception=" in sink.getvalue()
    assert "oops\\r\\nfailed" in sink.getvalue()


def test_empty_fields_do_not_emit_fields_key(logger_name) -> None:
    sink = io.StringIO()
    logger = configure_logging(logger_name, stream=sink, format="json")
    log_event(logger, "daemon.started", fields={})
    payload = json.loads(sink.getvalue())
    assert payload["message"] == "daemon.started"
    assert payload["event"] == "daemon.started"
    assert "fields" not in payload
