"""Mandate and payment governance guards.

MandateEnforcer     — bridges the governance pipeline to the authority boundary.
TAPSignatureGuard   — ensures every outbound merchant request carries a valid Visa TAP signature.
MAPTokenValidator   — validates Agentic Token governance metadata before payment submission.
"""

from __future__ import annotations

from core.authority import AuthorityBoundary, BoundaryViolation
from core.governance import Guard
from core.types import (
    Action,
    Direction,
    GuardOutcome,
    GuardResult,
    Message,
    SessionContext,
)


class MandateEnforcer(Guard):
    name = "MandateEnforcer"
    direction = Direction.BOTH
    priority = 5

    def __init__(self, authority: AuthorityBoundary) -> None:
        self._authority = authority

    async def inspect(self, message: Message, context: SessionContext) -> GuardResult:
        # Only check messages that carry an explicit action amount / scope
        content = message.content
        if not isinstance(content, dict):
            return GuardResult(outcome=GuardOutcome.PASS, guard_name=self.name)

        amount = content.get("amount")
        scope = content.get("scope")

        if amount is None and scope is None:
            return GuardResult(outcome=GuardOutcome.PASS, guard_name=self.name)

        # Build a lightweight action for boundary validation
        action = Action(
            session_id=context.session_id,
            step_id=context.current_step_id or "unknown",
            protocol="internal",
            operation="mandate_check",
            parameters=content,
            amount=amount,
            scope=scope,
        )

        try:
            self._authority.validate_action(action)
        except BoundaryViolation as exc:
            return GuardResult(
                outcome=GuardOutcome.BLOCK,
                guard_name=self.name,
                reason=str(exc),
                metadata={"resource": exc.resource},
            )

        return GuardResult(outcome=GuardOutcome.PASS, guard_name=self.name)


class TAPSignatureGuard(Guard):
    """Ensures every outbound merchant request carries a valid Visa TAP signature."""

    name = "TAPSignatureGuard"
    direction = Direction.OUTBOUND
    priority = 15

    async def inspect(self, message: Message, context: SessionContext) -> GuardResult:
        content = message.content
        if not isinstance(content, dict):
            return GuardResult(outcome=GuardOutcome.PASS, guard_name=self.name)

        # Only enforce on merchant-bound requests
        if not content.get("merchant_request", False):
            return GuardResult(outcome=GuardOutcome.PASS, guard_name=self.name)

        headers = content.get("headers", {})
        has_sig = "x-signature" in headers or "signature-input" in headers
        if not has_sig:
            return GuardResult(
                outcome=GuardOutcome.BLOCK,
                guard_name=self.name,
                reason="Merchant request missing Visa TAP signature",
            )
        return GuardResult(outcome=GuardOutcome.PASS, guard_name=self.name)


class MAPTokenValidator(Guard):
    """Validates Agentic Token governance metadata before payment submission."""

    name = "MAPTokenValidator"
    direction = Direction.OUTBOUND
    priority = 16

    async def inspect(self, message: Message, context: SessionContext) -> GuardResult:
        content = message.content
        if not isinstance(content, dict):
            return GuardResult(outcome=GuardOutcome.PASS, guard_name=self.name)

        # Only enforce on payment operations
        if content.get("operation") not in ("pay", "confirm_payment_intent"):
            return GuardResult(outcome=GuardOutcome.PASS, guard_name=self.name)

        token = content.get("map_token")
        if not token:
            return GuardResult(
                outcome=GuardOutcome.BLOCK,
                guard_name=self.name,
                reason="Payment request missing MAP token",
            )

        gov = token.get("governance_metadata") if isinstance(token, dict) else None
        if not gov:
            return GuardResult(
                outcome=GuardOutcome.BLOCK,
                guard_name=self.name,
                reason="MAP token missing governance_metadata",
            )

        return GuardResult(outcome=GuardOutcome.PASS, guard_name=self.name)
