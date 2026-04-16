"""State Machine Engine — enforces a strict ordered sequence of steps.

No step can be skipped. Rollback is supported. History is append-only.
"""

from __future__ import annotations

import asyncio
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from .types import Condition, SessionContext, SessionStatus


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass
class StepTransition:
    """Immutable record of a single state-machine transition."""

    transition_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    from_step: Optional[str] = None
    to_step: Optional[str] = None
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    event: str = "advance"
    actor: str = "agent"
    metadata: dict[str, Any] = field(default_factory=dict)

    def __setattr__(self, name: str, value: Any) -> None:
        # Make transitions immutable after creation
        if hasattr(self, name):
            raise AttributeError("StepTransition is immutable")
        object.__setattr__(self, name, value)


@dataclass
class Step:
    """A single step in a flow graph."""

    id: str
    name: str
    handler: Callable
    protocol: str = "internal"
    entry_conditions: list[Condition] = field(default_factory=list)
    exit_conditions: list[Condition] = field(default_factory=list)
    rollback_handler: Optional[Callable] = None
    requires_approval: bool = False
    timeout_seconds: Optional[int] = None


class FlowGraph:
    """Ordered collection of steps with transition and rollback logic."""

    def __init__(self, steps: list[Step]) -> None:
        if not steps:
            raise ValueError("FlowGraph requires at least one step")
        self._steps: OrderedDict[str, Step] = OrderedDict(
            (s.id, s) for s in steps
        )
        self._step_ids: list[str] = [s.id for s in steps]
        self.current_step: str = self._step_ids[0]
        self.status: SessionStatus = SessionStatus.PENDING
        self._history: list[StepTransition] = []
        self._approval_pending: bool = False
        self._last_handler_result: Any = None

    # ------------------------------------------------------------------
    # Public read-only properties
    # ------------------------------------------------------------------

    @property
    def steps(self) -> OrderedDict[str, Step]:
        return self._steps

    @property
    def history(self) -> list[StepTransition]:
        return list(self._history)  # return copy — append-only contract

    @property
    def current_step_obj(self) -> Step:
        return self._steps[self.current_step]

    @property
    def approval_pending(self) -> bool:
        return self._approval_pending

    # ------------------------------------------------------------------
    # Transitions
    # ------------------------------------------------------------------

    async def start(self, context: SessionContext) -> None:
        """Transition from PENDING → ACTIVE, checking entry conditions of step 0."""
        if self.status != SessionStatus.PENDING:
            raise StateMachineError(f"Cannot start: session is {self.status}")
        self._check_entry_conditions(self.current_step, context)
        self.status = SessionStatus.ACTIVE
        self._record_transition(None, self.current_step, "start")

    async def execute_current(self, context: SessionContext, inputs: dict[str, Any]) -> Any:
        """Run the current step's handler, respecting timeout."""
        if self.status != SessionStatus.ACTIVE:
            raise StateMachineError(
                f"Cannot execute: session is {self.status}"
            )
        step = self.current_step_obj
        coro = step.handler(context, inputs)
        if step.timeout_seconds:
            try:
                result = await asyncio.wait_for(coro, timeout=step.timeout_seconds)
            except asyncio.TimeoutError:
                raise StepTimeoutError(
                    f"Step '{step.id}' exceeded timeout of {step.timeout_seconds}s"
                )
        else:
            result = await coro
        self._last_handler_result = result
        # If requires_approval, pause before advancing
        if step.requires_approval:
            self.pause()
        return result

    async def advance(self, context: SessionContext) -> Optional[str]:
        """Validate exit conditions, move to next step, return its id (or None if done)."""
        if self.status not in (SessionStatus.ACTIVE,):
            raise StateMachineError(
                f"Cannot advance: session is {self.status}"
            )
        self._check_exit_conditions(self.current_step, context)
        next_id = self._next_step_id()
        if next_id is None:
            self.status = SessionStatus.COMPLETED
            self._record_transition(self.current_step, None, "complete")
            return None
        self._check_entry_conditions(next_id, context)
        from_id = self.current_step
        self.current_step = next_id
        self._record_transition(from_id, next_id, "advance")
        return next_id

    def pause(self) -> None:
        if self.status != SessionStatus.ACTIVE:
            raise StateMachineError(f"Cannot pause: session is {self.status}")
        self.status = SessionStatus.PAUSED
        self._approval_pending = True
        self._record_transition(self.current_step, self.current_step, "pause")

    async def resume(self, approval_token: str, context: SessionContext) -> None:
        """Validate approval token, resume from paused state."""
        if self.status != SessionStatus.PAUSED:
            raise StateMachineError(f"Cannot resume: session is {self.status}")
        if not self._validate_approval_token(approval_token):
            raise ApprovalError("Invalid approval token")
        self.status = SessionStatus.ACTIVE
        self._approval_pending = False
        self._record_transition(self.current_step, self.current_step, "resume",
                                metadata={"approval_token_prefix": approval_token[:8]})

    async def rollback(self, context: SessionContext) -> None:
        """Call rollback_handler on the current step, move back one step."""
        step = self.current_step_obj
        if step.rollback_handler:
            await step.rollback_handler(context, self._last_handler_result)
        prev_id = self._prev_step_id()
        from_id = self.current_step
        if prev_id is not None:
            self.current_step = prev_id
        self._record_transition(from_id, self.current_step, "rollback")

    async def kill(self, context: SessionContext, operator_id: str) -> None:
        """Emergency stop — rollback current step, set status to killed."""
        if self.status == SessionStatus.KILLED:
            return
        step = self.current_step_obj
        if step.rollback_handler:
            try:
                await step.rollback_handler(context, self._last_handler_result)
            except Exception:
                pass  # Best-effort rollback on kill
        self.status = SessionStatus.KILLED
        self._record_transition(self.current_step, None, "kill",
                                metadata={"operator_id": operator_id})

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _next_step_id(self) -> Optional[str]:
        idx = self._step_ids.index(self.current_step)
        if idx + 1 < len(self._step_ids):
            return self._step_ids[idx + 1]
        return None

    def _prev_step_id(self) -> Optional[str]:
        idx = self._step_ids.index(self.current_step)
        if idx > 0:
            return self._step_ids[idx - 1]
        return None

    def _check_entry_conditions(self, step_id: str, context: SessionContext) -> None:
        step = self._steps[step_id]
        for cond in step.entry_conditions:
            if not cond(context):
                raise ConditionError(
                    f"Entry condition failed for step '{step_id}'"
                )

    def _check_exit_conditions(self, step_id: str, context: SessionContext) -> None:
        step = self._steps[step_id]
        for cond in step.exit_conditions:
            if not cond(context):
                raise ConditionError(
                    f"Exit condition failed for step '{step_id}'"
                )

    def _validate_approval_token(self, token: str) -> bool:
        # In production: verify a signed JWT or HMAC token.
        # For now: any non-empty string is valid (tests override this).
        return bool(token and len(token) >= 8)

    def _record_transition(
        self,
        from_step: Optional[str],
        to_step: Optional[str],
        event: str,
        metadata: Optional[dict] = None,
    ) -> None:
        t = StepTransition.__new__(StepTransition)
        object.__setattr__(t, "transition_id", str(uuid.uuid4()))
        object.__setattr__(t, "from_step", from_step)
        object.__setattr__(t, "to_step", to_step)
        object.__setattr__(t, "timestamp", datetime.now(timezone.utc))
        object.__setattr__(t, "event", event)
        object.__setattr__(t, "actor", "agent")
        object.__setattr__(t, "metadata", metadata or {})
        self._history.append(t)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class StateMachineError(Exception):
    pass


class ConditionError(StateMachineError):
    pass


class StepTimeoutError(StateMachineError):
    pass


class ApprovalError(StateMachineError):
    pass


class OutOfOrderExecutionError(StateMachineError):
    pass
