"""Unit tests for the audit logger and event immutability."""

import json
import tempfile
from pathlib import Path

import pytest

from core.audit import AuditEvent, AuditLogger, AuditWriteFailedError, FileAuditBackend, InMemoryAuditBackend
from core.types import Disposition, EventType, PolicyDecision


def _event(**kwargs) -> AuditEvent:
    defaults = dict(
        session_id="s1",
        event_type=EventType.STEP_TRANSITION,
        actor="agent",
        action="test action",
        disposition=Disposition.SUCCESS,
    )
    defaults.update(kwargs)
    return AuditEvent(**defaults)


# ---------------------------------------------------------------------------
# Immutability
# ---------------------------------------------------------------------------


def test_audit_event_is_immutable():
    e = _event()
    with pytest.raises(Exception):
        e.session_id = "other"  # pydantic frozen model


# ---------------------------------------------------------------------------
# InMemoryAuditBackend
# ---------------------------------------------------------------------------


def test_in_memory_write_and_query():
    backend = InMemoryAuditBackend()
    e1 = _event(session_id="s1", step_id="step_a")
    e2 = _event(session_id="s2", step_id="step_b")
    backend.write(e1)
    backend.write(e2)

    assert len(backend.query()) == 2
    assert len(backend.query(session_id="s1")) == 1
    assert backend.query(session_id="s1")[0].step_id == "step_a"


def test_in_memory_query_by_event_type():
    backend = InMemoryAuditBackend()
    backend.write(_event(event_type=EventType.STEP_TRANSITION))
    backend.write(_event(event_type=EventType.KILL))
    assert len(backend.query(event_type=EventType.KILL)) == 1


def test_in_memory_query_by_step_id():
    backend = InMemoryAuditBackend()
    backend.write(_event(step_id="a"))
    backend.write(_event(step_id="b"))
    assert backend.query(step_id="a")[0].step_id == "a"


# ---------------------------------------------------------------------------
# FileAuditBackend
# ---------------------------------------------------------------------------


def test_file_backend_appends_jsonl():
    with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as f:
        path = f.name
    backend = FileAuditBackend(path)
    backend.write(_event(session_id="file_test"))
    backend.write(_event(session_id="file_test"))
    lines = Path(path).read_text().strip().split("\n")
    assert len(lines) == 2
    data = json.loads(lines[0])
    assert data["session_id"] == "file_test"


def test_file_backend_query_filters():
    with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as f:
        path = f.name
    backend = FileAuditBackend(path)
    backend.write(_event(session_id="A"))
    backend.write(_event(session_id="B"))
    results = backend.query(session_id="A")
    assert len(results) == 1


# ---------------------------------------------------------------------------
# AuditLogger convenience methods
# ---------------------------------------------------------------------------


def test_logger_step_transition():
    logger = AuditLogger(InMemoryAuditBackend())
    logger.step_transition("s1", from_step="a", to_step="b")
    events = logger.query(session_id="s1")
    assert events[0].event_type == EventType.STEP_TRANSITION


def test_logger_escalation():
    logger = AuditLogger(InMemoryAuditBackend())
    logger.escalation("s1", step_id="a", trigger="boundary_breach", severity="Critical")
    events = logger.query(event_type=EventType.ESCALATION)
    assert len(events) == 1
    assert events[0].disposition == Disposition.ESCALATED


def test_logger_kill_event():
    logger = AuditLogger(InMemoryAuditBackend())
    logger.kill_event("s1", step_id="a", operator_id="ops_1")
    events = logger.query(event_type=EventType.KILL)
    assert len(events) == 1


def test_audit_write_failure_raises():
    class FailingBackend(InMemoryAuditBackend):
        def write(self, event):
            raise OSError("disk full")

    logger = AuditLogger(FailingBackend())
    with pytest.raises(AuditWriteFailedError, match="disk full"):
        logger.log(_event())
