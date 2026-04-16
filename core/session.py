"""Session Manager — ties state machine, authority, governance, adapters, and audit together.

One SessionManager instance manages a single agent session from start to kill.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from .protocol_adapter import AdapterRegistry, PermanentAdapterError
from .audit import AuditEvent, AuditLogger
from .authority import AuthorityBoundary, BoundaryViolation
from .governance import GuardPipeline, PipelineBlockedError
from .state_machine import ApprovalError, FlowGraph, StateMachineError
from .types import (
    Action,
    Direction,
    Disposition,
    EventType,
    GuardOutcome,
    Message,
    PolicyDecision,
    SessionContext,
    SessionStatus,
)

logger = logging.getLogger(__name__)


class SessionManager:
    """Orchestrates a single agent session end-to-end."""

    def __init__(
        self,
        session_id: str,
        domain: str,
        flow: FlowGraph,
        adapters: AdapterRegistry,
        guard_pipeline: GuardPipeline,
        authority: AuthorityBoundary,
        audit: AuditLogger,
    ) -> None:
        self.session_id = session_id
        self.domain = domain
        self._flow = flow
        self._adapters = adapters
        self._guards = guard_pipeline
        self._authority = authority
        self._audit = audit
        self._context = SessionContext(
            session_id=session_id,
            domain=domain,
            status=SessionStatus.PENDING,
        )
        self._started_at: Optional[datetime] = None

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def status(self) -> SessionStatus:
        return self._flow.status

    @property
    def current_step_id(self) -> str:
        return self._flow.current_step

    @property
    def context(self) -> SessionContext:
        return self._context

    @property
    def authority(self) -> AuthorityBoundary:
        return self._authority

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> dict[str, Any]:
        """Initialise the session and run adapter health checks."""
        health = await self._adapters.health_check_all()
        if not self._adapters.all_healthy(health):
            unhealthy = [p for p, h in health.items() if not h.healthy]
            raise SessionStartError(
                f"Adapters not healthy before session start: {unhealthy}"
            )

        self._authority.lock()
        await self._flow.start(self._context)
        self._context = self._context.model_copy(
            update={"status": SessionStatus.ACTIVE}
        )
        self._started_at = datetime.now(timezone.utc)

        self._audit.step_transition(
            session_id=self.session_id,
            from_step=None,
            to_step=self._flow.current_step,
            metadata={"domain": self.domain},
        )
        logger.info("Session '%s' started — domain=%s", self.session_id, self.domain)
        return {
            "session_id": self.session_id,
            "status": self.status,
            "first_step": self._flow.current_step,
        }

    async def execute_step(
        self, step_id: str, inputs: dict[str, Any]
    ) -> dict[str, Any]:
        """Execute the named step with the given inputs."""
        if self.status == SessionStatus.KILLED:
            raise SessionKilledError(f"Session '{self.session_id}' has been killed")
        if self.status == SessionStatus.PAUSED:
            raise SessionPausedError(
                f"Session is paused pending approval for step '{self._flow.current_step}'"
            )
        if step_id != self._flow.current_step:
            raise OutOfOrderError(
                f"Requested step '{step_id}' but current step is '{self._flow.current_step}'"
            )

        step = self._flow.current_step_obj

        # 1. Run governance pipeline on the inbound message
        inbound_msg = Message(
            session_id=self.session_id,
            direction=Direction.INBOUND,
            content=inputs,
            source="consumer",
        )
        inbound_msg, guard_results = await self._run_guards(inbound_msg)
        inputs = inbound_msg.content  # use (potentially modified) inputs

        # 2. Build Action and validate against authority boundary
        action = Action(
            session_id=self.session_id,
            step_id=step_id,
            protocol=step.protocol,
            operation=step_id,
            parameters=inputs,
            amount=inputs.get("amount"),
            scope=inputs.get("scope"),
        )
        self._check_boundary(action)

        # 3. Execute step handler
        try:
            result = await self._flow.execute_current(self._context, inputs)
        except Exception as exc:
            self._audit.log(
                AuditEvent(
                    session_id=self.session_id,
                    step_id=step_id,
                    event_type=EventType.ERROR,
                    actor="agent",
                    action=f"execute {step_id}",
                    disposition=Disposition.FAILURE,
                    metadata={"error": str(exc)},
                )
            )
            raise

        # 4. Consume resources on success
        self._authority.consume(action)

        # 5. Run outbound governance pipeline on result
        outbound_msg = Message(
            session_id=self.session_id,
            direction=Direction.OUTBOUND,
            content=result,
            source="agent",
        )
        outbound_msg, _ = await self._run_guards(outbound_msg)
        result = outbound_msg.content

        # 6. Log proximity alerts
        near_limits = self._authority.check_proximity()
        for resource in near_limits:
            self._audit.escalation(
                session_id=self.session_id,
                step_id=step_id,
                trigger=f"spend_proximity:{resource}",
                severity="Warning",
            )

        # 7. Advance state machine if not paused for approval
        response: dict[str, Any] = {
            "session_id": self.session_id,
            "step_id": step_id,
            "result": result,
            "status": self.status,
        }

        if self.status == SessionStatus.PAUSED:
            # Approval required — don't advance yet
            response["pending_approval"] = True
            response["next_step"] = step_id
        else:
            next_id = await self._flow.advance(self._context)
            self._context = self._context.model_copy(
                update={
                    "current_step_id": next_id,
                    "status": self._flow.status,
                    "step_history": self._context.step_history + [step_id],
                }
            )
            self._audit.step_transition(
                session_id=self.session_id,
                from_step=step_id,
                to_step=next_id,
            )
            response["next_step"] = next_id
            response["status"] = self._flow.status

        return response

    async def approve(self, step_id: str, approval_token: str) -> dict[str, Any]:
        """Provide human approval for a paused step."""
        if self.status != SessionStatus.PAUSED:
            raise StateMachineError(f"Session is not paused (status={self.status})")

        self._audit.log(
            AuditEvent(
                session_id=self.session_id,
                step_id=step_id,
                event_type=EventType.APPROVAL,
                actor="consumer",
                action=f"approve step {step_id}",
                disposition=Disposition.SUCCESS,
                metadata={"token_prefix": approval_token[:8]},
            )
        )

        await self._flow.resume(approval_token, self._context)
        next_id = await self._flow.advance(self._context)
        self._context = self._context.model_copy(
            update={
                "current_step_id": next_id,
                "status": self._flow.status,
            }
        )
        self._audit.step_transition(
            session_id=self.session_id,
            from_step=step_id,
            to_step=next_id,
            metadata={"event": "post-approval advance"},
        )
        return {
            "session_id": self.session_id,
            "approved_step": step_id,
            "next_step": next_id,
            "status": self._flow.status,
        }

    async def cancel(self, reason: str) -> dict[str, Any]:
        """Cancel the session with rollback of the current step."""
        await self._flow.rollback(self._context)
        self._flow.status = SessionStatus.FAILED
        self._context = self._context.model_copy(update={"status": SessionStatus.FAILED})
        self._audit.log(
            AuditEvent(
                session_id=self.session_id,
                step_id=self._flow.current_step,
                event_type=EventType.STEP_TRANSITION,
                actor="consumer",
                action="cancel",
                disposition=Disposition.FAILURE,
                metadata={"reason": reason},
            )
        )
        return {"session_id": self.session_id, "status": SessionStatus.FAILED, "reason": reason}

    async def kill(self, operator_id: str) -> dict[str, Any]:
        """Emergency stop."""
        await self._flow.kill(self._context, operator_id)
        self._authority.revoke()
        self._context = self._context.model_copy(update={"status": SessionStatus.KILLED})
        self._audit.kill_event(
            session_id=self.session_id,
            step_id=self._flow.current_step,
            operator_id=operator_id,
        )
        logger.warning(
            "Session '%s' KILLED by operator '%s'", self.session_id, operator_id
        )
        return {
            "session_id": self.session_id,
            "status": SessionStatus.KILLED,
            "operator_id": operator_id,
        }

    def get_status(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "domain": self.domain,
            "status": self._flow.status,
            "current_step": self._flow.current_step,
            "history": [
                {
                    "from": t.from_step,
                    "to": t.to_step,
                    "event": t.event,
                    "timestamp": t.timestamp.isoformat(),
                }
                for t in self._flow.history
            ],
        }

    def get_audit_trail(
        self,
        event_type: Optional[EventType] = None,
        step_id: Optional[str] = None,
    ) -> list[dict]:
        events = self._audit.query(
            session_id=self.session_id,
            event_type=event_type,
            step_id=step_id,
        )
        return [e.model_dump(mode="json") for e in events]

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _run_guards(
        self, message: Message
    ) -> tuple[Message, list]:
        try:
            msg, results = await self._guards.run(message, self._context)
            for r in results:
                self._audit.guard_result(
                    session_id=self.session_id,
                    step_id=self._flow.current_step,
                    guard_name=r.guard_name,
                    direction=message.direction.value,
                    outcome=r.outcome.value,
                    reason=r.reason,
                )
            return msg, results
        except PipelineBlockedError as exc:
            for r in exc.results:
                self._audit.guard_result(
                    session_id=self.session_id,
                    step_id=self._flow.current_step,
                    guard_name=r.guard_name,
                    direction=message.direction.value,
                    outcome=r.outcome.value,
                    reason=r.reason,
                )
            self._audit.escalation(
                session_id=self.session_id,
                step_id=self._flow.current_step,
                trigger=f"guard_block:{exc.guard_name}",
                severity="High",
            )
            raise

    def _check_boundary(self, action: Action) -> None:
        try:
            self._authority.validate_action(action)
            self._audit.boundary_check(
                session_id=self.session_id,
                step_id=action.step_id,
                action_desc=action.operation,
                decision=PolicyDecision.ALLOW,
            )
        except BoundaryViolation as exc:
            self._audit.boundary_check(
                session_id=self.session_id,
                step_id=action.step_id,
                action_desc=action.operation,
                decision=PolicyDecision.DENY,
                reason=str(exc),
            )
            self._audit.escalation(
                session_id=self.session_id,
                step_id=action.step_id,
                trigger=f"boundary_breach:{exc.resource}",
                severity="Critical",
            )
            self._flow.pause()  # Pause for operator escalation
            raise


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class SessionStartError(Exception):
    pass


class SessionKilledError(Exception):
    pass


class SessionPausedError(Exception):
    pass


class OutOfOrderError(Exception):
    pass
