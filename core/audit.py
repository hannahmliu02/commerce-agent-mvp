"""Audit and Observability — structured, immutable event logging.

Every action, decision, approval, and policy check is written synchronously
before the next step executes. The file-based backend is append-only JSONL.
"""

from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, Field

from .types import Disposition, EventType, PolicyDecision


# ---------------------------------------------------------------------------
# Event schema
# ---------------------------------------------------------------------------


class AuditEvent(BaseModel):
    event_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    session_id: str
    step_id: Optional[str] = None
    event_type: EventType
    actor: str  # "agent" | "consumer" | "operator" | <guard_name>
    action: str
    inputs: dict[str, Any] = Field(default_factory=dict)
    outputs: dict[str, Any] = Field(default_factory=dict)
    policy_decision: Optional[PolicyDecision] = None
    disposition: Disposition
    metadata: dict[str, Any] = Field(default_factory=dict)

    model_config = {"frozen": True}  # Immutable after creation


# ---------------------------------------------------------------------------
# Storage backend interface
# ---------------------------------------------------------------------------


class AuditBackend:
    """Base class — override write() for different backends."""

    def write(self, event: AuditEvent) -> None:
        raise NotImplementedError

    def query(
        self,
        session_id: Optional[str] = None,
        event_type: Optional[EventType] = None,
        step_id: Optional[str] = None,
    ) -> list[AuditEvent]:
        raise NotImplementedError


class FileAuditBackend(AuditBackend):
    """Append-only JSONL file backend. Thread-safe."""

    def __init__(self, path: str | Path = "audit.jsonl") -> None:
        self._path = Path(path)
        self._lock = threading.Lock()
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, event: AuditEvent) -> None:
        line = event.model_dump_json() + "\n"
        with self._lock:
            with self._path.open("a", encoding="utf-8") as fh:
                fh.write(line)

    def query(
        self,
        session_id: Optional[str] = None,
        event_type: Optional[EventType] = None,
        step_id: Optional[str] = None,
    ) -> list[AuditEvent]:
        if not self._path.exists():
            return []
        events: list[AuditEvent] = []
        with self._lock:
            with self._path.open("r", encoding="utf-8") as fh:
                for raw in fh:
                    raw = raw.strip()
                    if not raw:
                        continue
                    data = json.loads(raw)
                    e = AuditEvent.model_validate(data)
                    if session_id and e.session_id != session_id:
                        continue
                    if event_type and e.event_type != event_type:
                        continue
                    if step_id and e.step_id != step_id:
                        continue
                    events.append(e)
        return events

    def export(self, fmt: str = "json") -> str:
        events = self.query()
        if fmt == "json":
            return json.dumps([e.model_dump(mode="json") for e in events], indent=2)
        if fmt == "csv":
            import csv
            import io
            buf = io.StringIO()
            if not events:
                return ""
            writer = csv.DictWriter(buf, fieldnames=list(events[0].model_fields.keys()))
            writer.writeheader()
            for e in events:
                writer.writerow(e.model_dump(mode="json"))
            return buf.getvalue()
        raise ValueError(f"Unknown export format '{fmt}'")


class InMemoryAuditBackend(AuditBackend):
    """In-memory backend for testing — never persists to disk."""

    def __init__(self) -> None:
        self._events: list[AuditEvent] = []
        self._lock = threading.Lock()

    def write(self, event: AuditEvent) -> None:
        with self._lock:
            self._events.append(event)

    def query(
        self,
        session_id: Optional[str] = None,
        event_type: Optional[EventType] = None,
        step_id: Optional[str] = None,
    ) -> list[AuditEvent]:
        with self._lock:
            result = list(self._events)
        if session_id:
            result = [e for e in result if e.session_id == session_id]
        if event_type:
            result = [e for e in result if e.event_type == event_type]
        if step_id:
            result = [e for e in result if e.step_id == step_id]
        return result

    def clear(self) -> None:
        with self._lock:
            self._events.clear()


# ---------------------------------------------------------------------------
# Logger (facade)
# ---------------------------------------------------------------------------


class AuditLogger:
    """Write AuditEvents through the configured backend.

    The write is synchronous and must succeed before the next step executes.
    If the write fails, a WriteFailedError is raised — the caller must not proceed.
    """

    def __init__(self, backend: Optional[AuditBackend] = None) -> None:
        self._backend = backend or InMemoryAuditBackend()

    def log(self, event: AuditEvent) -> None:
        try:
            self._backend.write(event)
        except Exception as exc:
            raise AuditWriteFailedError(
                f"Audit write failed — step must not proceed: {exc}"
            ) from exc

    def query(
        self,
        session_id: Optional[str] = None,
        event_type: Optional[EventType] = None,
        step_id: Optional[str] = None,
    ) -> list[AuditEvent]:
        return self._backend.query(
            session_id=session_id,
            event_type=event_type,
            step_id=step_id,
        )

    # ------------------------------------------------------------------
    # Convenience builders
    # ------------------------------------------------------------------

    def step_transition(
        self,
        session_id: str,
        from_step: Optional[str],
        to_step: Optional[str],
        actor: str = "agent",
        metadata: Optional[dict] = None,
    ) -> None:
        self.log(
            AuditEvent(
                session_id=session_id,
                step_id=from_step,
                event_type=EventType.STEP_TRANSITION,
                actor=actor,
                action=f"transition {from_step} → {to_step}",
                disposition=Disposition.SUCCESS,
                metadata=metadata or {},
            )
        )

    def guard_result(
        self,
        session_id: str,
        step_id: Optional[str],
        guard_name: str,
        direction: str,
        outcome: str,
        reason: Optional[str] = None,
    ) -> None:
        from .types import Disposition
        disposition = (
            Disposition.BLOCKED if outcome == "BLOCK"
            else Disposition.SUCCESS
        )
        self.log(
            AuditEvent(
                session_id=session_id,
                step_id=step_id,
                event_type=EventType.GUARD_RESULT,
                actor=guard_name,
                action=f"guard inspect [{direction}]",
                disposition=disposition,
                metadata={"outcome": outcome, "reason": reason},
            )
        )

    def boundary_check(
        self,
        session_id: str,
        step_id: Optional[str],
        action_desc: str,
        decision: PolicyDecision,
        reason: Optional[str] = None,
    ) -> None:
        disposition = (
            Disposition.SUCCESS if decision == PolicyDecision.ALLOW
            else Disposition.BLOCKED
        )
        self.log(
            AuditEvent(
                session_id=session_id,
                step_id=step_id,
                event_type=EventType.BOUNDARY_CHECK,
                actor="agent",
                action=action_desc,
                policy_decision=decision,
                disposition=disposition,
                metadata={"reason": reason},
            )
        )

    def escalation(
        self,
        session_id: str,
        step_id: Optional[str],
        trigger: str,
        severity: str,
    ) -> None:
        self.log(
            AuditEvent(
                session_id=session_id,
                step_id=step_id,
                event_type=EventType.ESCALATION,
                actor="agent",
                action=f"escalation: {trigger}",
                disposition=Disposition.ESCALATED,
                metadata={"severity": severity},
            )
        )

    def kill_event(
        self,
        session_id: str,
        step_id: Optional[str],
        operator_id: str,
    ) -> None:
        self.log(
            AuditEvent(
                session_id=session_id,
                step_id=step_id,
                event_type=EventType.KILL,
                actor=operator_id,
                action="emergency kill",
                disposition=Disposition.SUCCESS,
                metadata={"operator_id": operator_id},
            )
        )


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class AuditWriteFailedError(Exception):
    pass
