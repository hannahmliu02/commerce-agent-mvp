"""Commerce Agent flow graph — five-step state machine (Section 4.1).

Step 1: Product Discovery  — ACP browse (read-only)
Step 2: Product Selection  — ACP checkout
Step 3: Consumer Approval  — requires_approval=True (human gate)
Step 4: Payment Execution  — Stripe + MAP
Step 5: Audit Finalization — logging only
"""

from __future__ import annotations

from typing import Any

from core.state_machine import FlowGraph, Step
from core.types import SessionContext


# ---------------------------------------------------------------------------
# Step handlers
# ---------------------------------------------------------------------------


async def _handle_product_discovery(context: SessionContext, inputs: dict[str, Any]) -> dict:
    """Browse the merchant catalog."""
    return {
        "step": "product_discovery",
        "query": inputs.get("query", ""),
        "filters": inputs.get("filters", {}),
        "status": "products_retrieved",
    }


async def _rollback_product_discovery(context: SessionContext, result: Any) -> None:
    # Read-only — nothing to undo
    pass


async def _handle_product_selection(context: SessionContext, inputs: dict[str, Any]) -> dict:
    """Create a checkout session for the selected product."""
    return {
        "step": "product_selection",
        "product_id": inputs.get("product_id"),
        "options": inputs.get("options", {}),
        "status": "checkout_created",
        "checkout_session_id": inputs.get("checkout_session_id", f"cs_{id(context)}"),
    }


async def _rollback_product_selection(context: SessionContext, result: Any) -> None:
    # Would cancel the checkout session in a real integration
    pass


async def _handle_consumer_approval(context: SessionContext, inputs: dict[str, Any]) -> dict:
    """Present the order summary to the consumer and pause for approval.

    The state machine pauses here because requires_approval=True.
    Execution continues after agent.approve() is called.
    """
    return {
        "step": "consumer_approval",
        "order_summary": inputs.get("order_summary", {}),
        "total": inputs.get("total"),
        "status": "awaiting_approval",
    }


async def _rollback_consumer_approval(context: SessionContext, result: Any) -> None:
    # Cancel checkout session
    pass


async def _handle_payment_execution(context: SessionContext, inputs: dict[str, Any]) -> dict:
    """Execute the payment via Stripe + MAP token."""
    return {
        "step": "payment_execution",
        "checkout_session_id": inputs.get("checkout_session_id"),
        "payment_intent_id": inputs.get("payment_intent_id"),
        "status": "payment_processed",
        "amount_charged": inputs.get("amount"),
    }


async def _rollback_payment_execution(context: SessionContext, result: Any) -> None:
    # Cancel PaymentIntent; revoke MAP token
    pass


async def _handle_audit_finalization(context: SessionContext, inputs: dict[str, Any]) -> dict:
    """Finalise the audit record for the completed transaction."""
    return {
        "step": "audit_finalization",
        "status": "completed",
        "session_id": context.session_id,
    }


# ---------------------------------------------------------------------------
# Flow graph factory
# ---------------------------------------------------------------------------


def build_commerce_flow() -> FlowGraph:
    """Build the five-step commerce flow graph."""
    steps = [
        Step(
            id="product_discovery",
            name="Product Discovery",
            handler=_handle_product_discovery,
            protocol="commerce",
            rollback_handler=_rollback_product_discovery,
            requires_approval=False,
            timeout_seconds=30,
        ),
        Step(
            id="product_selection",
            name="Product Selection",
            handler=_handle_product_selection,
            protocol="commerce",
            rollback_handler=_rollback_product_selection,
            requires_approval=False,
            timeout_seconds=30,
        ),
        Step(
            id="consumer_approval",
            name="Consumer Approval",
            handler=_handle_consumer_approval,
            protocol="internal",
            rollback_handler=_rollback_consumer_approval,
            requires_approval=True,   # ← Human gate
            timeout_seconds=300,
        ),
        Step(
            id="payment_execution",
            name="Payment Execution",
            handler=_handle_payment_execution,
            protocol="payment",
            rollback_handler=_rollback_payment_execution,
            requires_approval=False,  # Pre-approved at step 3
            timeout_seconds=60,
        ),
        Step(
            id="audit_finalization",
            name="Audit Finalization",
            handler=_handle_audit_finalization,
            protocol="audit",
            rollback_handler=None,    # Logging only — no rollback
            requires_approval=False,
        ),
    ]
    return FlowGraph(steps)


# Convenience alias
CommerceFlow = build_commerce_flow
