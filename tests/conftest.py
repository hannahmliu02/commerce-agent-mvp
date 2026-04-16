"""Shared fixtures and mock adapters for the TrustX test suite."""

from __future__ import annotations

import pytest
import pytest_asyncio

from core.protocol_adapter import AdapterRegistry, ProtocolAdapter
from core.audit import AuditLogger, InMemoryAuditBackend
from core.authority import AuthorityBoundary, ResourceLimit
from core.governance import GuardPipeline
from core.state_machine import FlowGraph, Step
from core.types import (
    Action,
    AdapterResponse,
    Direction,
    GuardOutcome,
    GuardResult,
    HealthStatus,
    Message,
    RollbackResult,
    SessionContext,
    SessionStatus,
    ValidationResult,
)
from agents.commerce.guards.pii_shield import PIIShield
from agents.commerce.guards.injection import PromptInjectionGuard


# ---------------------------------------------------------------------------
# Mock adapter
# ---------------------------------------------------------------------------


class MockAdapter(ProtocolAdapter):
    name = "mock_adapter"
    protocol = "mock"

    def __init__(self, healthy: bool = True, should_fail: bool = False) -> None:
        self._healthy = healthy
        self._should_fail = should_fail
        self.executed: list[Action] = []

    async def execute(self, action: Action) -> AdapterResponse:
        self.executed.append(action)
        if self._should_fail:
            return AdapterResponse(action_id=action.action_id, success=False, error="mock failure")
        return AdapterResponse(
            action_id=action.action_id,
            success=True,
            data={"mock": True, "operation": action.operation},
        )

    async def validate(self, action: Action) -> ValidationResult:
        return ValidationResult(valid=True)

    async def rollback(self, action_id: str) -> RollbackResult:
        return RollbackResult(success=True, action_id=action_id)

    async def health_check(self) -> HealthStatus:
        return HealthStatus(healthy=self._healthy, adapter_name=self.name)


# ---------------------------------------------------------------------------
# Mock guard
# ---------------------------------------------------------------------------


class MockGuard(GuardPipeline.__class__):
    pass


class AlwaysPassGuard:
    name = "AlwaysPassGuard"
    direction = Direction.BOTH
    priority = 99

    async def inspect(self, message: Message, context: SessionContext) -> GuardResult:
        return GuardResult(outcome=GuardOutcome.PASS, guard_name=self.name)


class AlwaysBlockGuard:
    name = "AlwaysBlockGuard"
    direction = Direction.BOTH
    priority = 99

    async def inspect(self, message: Message, context: SessionContext) -> GuardResult:
        return GuardResult(
            outcome=GuardOutcome.BLOCK,
            guard_name=self.name,
            reason="always block (test guard)",
        )


class AlwaysModifyGuard:
    name = "AlwaysModifyGuard"
    direction = Direction.BOTH
    priority = 99

    async def inspect(self, message: Message, context: SessionContext) -> GuardResult:
        return GuardResult(
            outcome=GuardOutcome.MODIFY,
            guard_name=self.name,
            reason="modified content",
            modified_content={"modified": True},
        )


# ---------------------------------------------------------------------------
# Simple 3-step flow fixture
# ---------------------------------------------------------------------------


async def _step_a(ctx, inputs):
    return {"step": "a", "inputs": inputs}


async def _step_b(ctx, inputs):
    return {"step": "b", "inputs": inputs}


async def _step_c(ctx, inputs):
    return {"step": "c", "inputs": inputs}


async def _rollback_a(ctx, result):
    pass


@pytest.fixture
def three_step_flow():
    return FlowGraph([
        Step(id="step_a", name="Step A", handler=_step_a, rollback_handler=_rollback_a),
        Step(id="step_b", name="Step B", handler=_step_b),
        Step(id="step_c", name="Step C", handler=_step_c),
    ])


@pytest.fixture
def session_context():
    return SessionContext(
        session_id="test-session-001",
        domain="test",
        status=SessionStatus.ACTIVE,
    )


@pytest.fixture
def mock_adapter():
    return MockAdapter()


@pytest.fixture
def adapter_registry(mock_adapter):
    reg = AdapterRegistry()
    reg.register(mock_adapter)
    return reg


@pytest.fixture
def in_memory_audit():
    return AuditLogger(InMemoryAuditBackend())


@pytest.fixture
def commerce_authority():
    return AuthorityBoundary(
        resource_limits={
            "spend": ResourceLimit(
                name="spend",
                max_per_action=500.0,
                max_cumulative=1000.0,
            )
        },
        allowed_scopes=[],
        session_ttl_seconds=1800,
        requires_approval_above=0.0,
    )


@pytest.fixture
def minimal_guard_pipeline():
    """A pipeline with only the mandatory guards."""
    return GuardPipeline(
        [PromptInjectionGuard(), PIIShield()],
        mandatory_guard_names={"PromptInjectionGuard", "PIIShield"},
    )
