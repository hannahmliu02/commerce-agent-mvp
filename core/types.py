"""Shared enums, dataclasses, and type aliases for the TrustX framework."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class SessionStatus(str, Enum):
    PENDING = "pending"
    ACTIVE = "active"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    KILLED = "killed"


class EventType(str, Enum):
    STEP_TRANSITION = "STEP_TRANSITION"
    GUARD_RESULT = "GUARD_RESULT"
    BOUNDARY_CHECK = "BOUNDARY_CHECK"
    ADAPTER_CALL = "ADAPTER_CALL"
    APPROVAL = "APPROVAL"
    ESCALATION = "ESCALATION"
    ERROR = "ERROR"
    KILL = "KILL"


class Direction(str, Enum):
    INBOUND = "INBOUND"
    OUTBOUND = "OUTBOUND"
    BOTH = "BOTH"


class GuardOutcome(str, Enum):
    PASS = "PASS"
    MODIFY = "MODIFY"
    BLOCK = "BLOCK"


class AdapterStatus(str, Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"


class PolicyDecision(str, Enum):
    ALLOW = "allow"
    DENY = "deny"
    MODIFY = "modify"


class Disposition(str, Enum):
    SUCCESS = "success"
    FAILURE = "failure"
    BLOCKED = "blocked"
    ESCALATED = "escalated"


class TokenScopeType(str, Enum):
    SINGLE_USE = "single_use"
    SESSION_BOUND = "session_bound"
    MULTI_USE = "multi_use"


# ---------------------------------------------------------------------------
# Core data models
# ---------------------------------------------------------------------------


class Action(BaseModel):
    """Represents a proposed action the agent wants to execute."""

    action_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    session_id: str
    step_id: str
    protocol: str
    operation: str
    parameters: dict[str, Any] = Field(default_factory=dict)
    amount: Optional[float] = None
    scope: Optional[str] = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class AdapterResponse(BaseModel):
    """Standardised response from a protocol adapter."""

    action_id: str
    success: bool
    data: dict[str, Any] = Field(default_factory=dict)
    error: Optional[str] = None
    retryable: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)


class ValidationResult(BaseModel):
    valid: bool
    reason: Optional[str] = None
    details: dict[str, Any] = Field(default_factory=dict)


class RollbackResult(BaseModel):
    success: bool
    action_id: str
    details: dict[str, Any] = Field(default_factory=dict)
    error: Optional[str] = None


class HealthStatus(BaseModel):
    healthy: bool
    adapter_name: str
    latency_ms: Optional[float] = None
    details: dict[str, Any] = Field(default_factory=dict)


class Message(BaseModel):
    """A message flowing through the governance pipeline."""

    message_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    session_id: str
    direction: Direction
    content: Any
    content_type: str = "text"
    source: str = "agent"
    tainted: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)


class GuardResult(BaseModel):
    outcome: GuardOutcome
    guard_name: str
    reason: Optional[str] = None
    modified_content: Optional[Any] = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class SessionContext(BaseModel):
    """Contextual information available to guards and handlers."""

    session_id: str
    domain: str
    current_step_id: Optional[str] = None
    status: SessionStatus = SessionStatus.PENDING
    step_history: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class TokenScopeConfig(BaseModel):
    scope_type: TokenScopeType = TokenScopeType.SINGLE_USE
    max_uses: int = 1
    bound_to_session: bool = True


# ---------------------------------------------------------------------------
# Step condition type alias
# ---------------------------------------------------------------------------

# A condition is a callable that takes a SessionContext and returns bool.
Condition = Callable[[SessionContext], bool]
