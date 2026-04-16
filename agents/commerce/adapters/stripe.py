"""Stripe Adapter — wraps the Stripe PaymentIntent API.

Uses Stripe test mode (sk_test_...) for development.
On emergency stop, cancels all pending PaymentIntents for the session.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from core.protocol_adapter import PermanentAdapterError, ProtocolAdapter, TransientAdapterError
from core.types import Action, AdapterResponse, HealthStatus, RollbackResult, ValidationResult

logger = logging.getLogger(__name__)


class StripeAdapter(ProtocolAdapter):
    name = "stripe_adapter"
    protocol = "payment"

    def __init__(self, api_key: Optional[str] = None, mock: bool = True) -> None:
        self._api_key = api_key
        self._mock = mock or (api_key is None) or api_key.startswith("sk_test_mock")
        self._pending: dict[str, dict] = {}  # payment_intent_id → intent data

        if not self._mock and api_key:
            try:
                import stripe as _stripe
                _stripe.api_key = api_key
                self._stripe = _stripe
            except ImportError:
                logger.warning("stripe package not installed; falling back to mock mode")
                self._mock = True

    async def validate(self, action: Action) -> ValidationResult:
        params = action.parameters
        if action.operation == "create_payment_intent":
            if not params.get("amount"):
                return ValidationResult(valid=False, reason="amount is required")
            if not params.get("currency"):
                return ValidationResult(valid=False, reason="currency is required")
        return ValidationResult(valid=True)

    async def execute(self, action: Action) -> AdapterResponse:
        op = action.operation
        params = action.parameters

        if self._mock:
            return self._mock_execute(op, params, action.action_id)

        try:
            if op == "create_payment_intent":
                intent = self._stripe.PaymentIntent.create(
                    amount=int(params["amount"] * 100),  # Stripe uses cents
                    currency=params.get("currency", "usd"),
                    metadata=params.get("metadata", {}),
                )
                self._pending[intent["id"]] = intent
                return AdapterResponse(
                    action_id=action.action_id,
                    success=True,
                    data={"payment_intent_id": intent["id"], "status": intent["status"]},
                )

            if op == "confirm_payment_intent":
                intent = self._stripe.PaymentIntent.confirm(
                    params["payment_intent_id"],
                    payment_method=params.get("payment_method"),
                )
                return AdapterResponse(
                    action_id=action.action_id,
                    success=intent["status"] == "succeeded",
                    data={"status": intent["status"]},
                )

            if op == "cancel_payment_intent":
                intent = self._stripe.PaymentIntent.cancel(params["payment_intent_id"])
                self._pending.pop(params["payment_intent_id"], None)
                return AdapterResponse(
                    action_id=action.action_id,
                    success=True,
                    data={"status": intent["status"]},
                )

        except Exception as exc:
            code = getattr(exc, "code", None)
            if code in ("authentication_required", "card_declined", "invalid_request_error"):
                raise PermanentAdapterError(str(exc)) from exc
            raise TransientAdapterError(str(exc)) from exc

        return AdapterResponse(
            action_id=action.action_id,
            success=False,
            error=f"Unknown Stripe operation: {op}",
        )

    def _mock_execute(self, op: str, params: dict, action_id: str) -> AdapterResponse:
        import uuid
        if op == "create_payment_intent":
            pi_id = f"pi_mock_{uuid.uuid4().hex[:12]}"
            self._pending[pi_id] = {
                "id": pi_id,
                "amount": params.get("amount", 0),
                "currency": params.get("currency", "usd"),
                "status": "requires_confirmation",
            }
            return AdapterResponse(
                action_id=action_id,
                success=True,
                data={"payment_intent_id": pi_id, "status": "requires_confirmation"},
            )

        if op == "confirm_payment_intent":
            pi_id = params.get("payment_intent_id", "")
            if pi_id in self._pending:
                self._pending[pi_id]["status"] = "succeeded"
            return AdapterResponse(
                action_id=action_id,
                success=True,
                data={"status": "succeeded"},
            )

        if op == "cancel_payment_intent":
            pi_id = params.get("payment_intent_id", "")
            self._pending.pop(pi_id, None)
            return AdapterResponse(
                action_id=action_id,
                success=True,
                data={"status": "canceled"},
            )

        return AdapterResponse(
            action_id=action_id, success=False, error=f"Unknown operation: {op}"
        )

    async def rollback(self, action_id: str) -> RollbackResult:
        """Cancel all pending PaymentIntents (called on session kill)."""
        cancelled = []
        for pi_id in list(self._pending.keys()):
            if not self._mock:
                try:
                    self._stripe.PaymentIntent.cancel(pi_id)
                except Exception:
                    pass
            self._pending.pop(pi_id, None)
            cancelled.append(pi_id)
        return RollbackResult(
            success=True,
            action_id=action_id,
            details={"cancelled_intents": cancelled},
        )

    async def health_check(self) -> HealthStatus:
        if self._mock:
            return HealthStatus(healthy=True, adapter_name=self.name, details={"mode": "mock"})
        try:
            self._stripe.Balance.retrieve()
            return HealthStatus(healthy=True, adapter_name=self.name)
        except Exception as exc:
            return HealthStatus(healthy=False, adapter_name=self.name, details={"error": str(exc)})
