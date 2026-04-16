"""ACP Client — Agentic Commerce Protocol adapter.

Handles browse, checkout, pay, and cancel operations against a merchant backend.
In development mode, uses a mock in-memory merchant store.
"""

from __future__ import annotations

import uuid
from typing import Any, Optional

import httpx

from core.protocol_adapter import PermanentAdapterError, ProtocolAdapter, TransientAdapterError
from core.types import Action, AdapterResponse, HealthStatus, RollbackResult, ValidationResult


class ACPClient(ProtocolAdapter):
    name = "acp_client"
    protocol = "commerce"

    def __init__(
        self,
        merchant_url: Optional[str] = None,
        mock: bool = True,
    ) -> None:
        self._merchant_url = merchant_url
        self._mock = mock or (merchant_url is None)
        self._client = httpx.AsyncClient(timeout=10.0) if not self._mock else None
        self._mock_store = _MockMerchantStore() if self._mock else None

    async def validate(self, action: Action) -> ValidationResult:
        op = action.operation
        if op not in ("browse", "checkout", "pay", "cancel"):
            return ValidationResult(valid=False, reason=f"Unknown ACP operation: {op}")
        return ValidationResult(valid=True)

    async def execute(self, action: Action) -> AdapterResponse:
        if self._mock:
            return await self._mock_execute(action)
        return await self._http_execute(action)

    async def rollback(self, action_id: str) -> RollbackResult:
        if self._mock and self._mock_store:
            self._mock_store.cancel_checkout(action_id)
        return RollbackResult(success=True, action_id=action_id)

    async def health_check(self) -> HealthStatus:
        if self._mock:
            return HealthStatus(healthy=True, adapter_name=self.name, details={"mode": "mock"})
        try:
            resp = await self._client.get(f"{self._merchant_url}/health")
            return HealthStatus(
                healthy=resp.status_code == 200,
                adapter_name=self.name,
                latency_ms=resp.elapsed.total_seconds() * 1000,
            )
        except Exception as exc:
            return HealthStatus(healthy=False, adapter_name=self.name, details={"error": str(exc)})

    # ------------------------------------------------------------------
    # Mock execution
    # ------------------------------------------------------------------

    async def _mock_execute(self, action: Action) -> AdapterResponse:
        store = self._mock_store
        params = action.parameters
        op = action.operation

        if op == "browse":
            results = store.browse(params.get("query", ""), params.get("filters", {}))
            return AdapterResponse(
                action_id=action.action_id,
                success=True,
                data={"products": results},
            )

        if op == "checkout":
            session = store.create_checkout(
                params.get("product_id", ""), params.get("options", {})
            )
            return AdapterResponse(
                action_id=action.action_id,
                success=True,
                data={"checkout_session": session},
            )

        if op == "pay":
            result = store.process_payment(
                params.get("checkout_session_id", ""),
                params.get("payment_token", ""),
            )
            return AdapterResponse(
                action_id=action.action_id,
                success=result["success"],
                data=result,
                error=result.get("error"),
            )

        if op == "cancel":
            store.cancel_checkout(params.get("checkout_session_id", ""))
            return AdapterResponse(
                action_id=action.action_id,
                success=True,
                data={"cancelled": True},
            )

        return AdapterResponse(
            action_id=action.action_id,
            success=False,
            error=f"Unknown operation: {op}",
        )

    async def _http_execute(self, action: Action) -> AdapterResponse:
        try:
            resp = await self._client.post(
                f"{self._merchant_url}/acp/{action.operation}",
                json=action.parameters,
            )
            if resp.status_code >= 500:
                raise TransientAdapterError(f"ACP server error {resp.status_code}")
            if resp.status_code >= 400:
                raise PermanentAdapterError(f"ACP client error {resp.status_code}: {resp.text}")
            return AdapterResponse(
                action_id=action.action_id,
                success=True,
                data=resp.json(),
            )
        except httpx.TimeoutException as exc:
            raise TransientAdapterError(f"ACP request timed out: {exc}") from exc


# ---------------------------------------------------------------------------
# Mock merchant store
# ---------------------------------------------------------------------------


_MOCK_CATALOG = [
    {"id": "p001", "name": "Wireless Headphones", "price": 79.99, "category": "electronics", "in_stock": True},
    {"id": "p002", "name": "Running Shoes", "price": 120.00, "category": "clothing", "in_stock": True},
    {"id": "p003", "name": "USB-C Hub", "price": 49.99, "category": "electronics", "in_stock": True},
    {"id": "p004", "name": "Yoga Mat", "price": 35.00, "category": "sports", "in_stock": True},
]


class _MockMerchantStore:
    def __init__(self) -> None:
        self._sessions: dict[str, dict] = {}

    def browse(self, query: str, filters: dict) -> list[dict]:
        results = [
            p for p in _MOCK_CATALOG
            if (not query or query.lower() in p["name"].lower())
            and (not filters.get("category") or p["category"] == filters["category"])
        ]
        return results

    def create_checkout(self, product_id: str, options: dict) -> dict:
        product = next((p for p in _MOCK_CATALOG if p["id"] == product_id), None)
        if not product:
            product = _MOCK_CATALOG[0]  # Fallback for testing
        session_id = str(uuid.uuid4())
        session = {
            "checkout_session_id": session_id,
            "product": product,
            "options": options,
            "total": product["price"],
            "currency": "USD",
            "shipping_options": [{"method": "standard", "price": 5.99, "days": 5}],
        }
        self._sessions[session_id] = session
        return session

    def process_payment(self, checkout_session_id: str, payment_token: str) -> dict:
        if checkout_session_id not in self._sessions:
            return {"success": False, "error": "Checkout session not found"}
        session = self._sessions[checkout_session_id]
        confirmation_id = str(uuid.uuid4())
        return {
            "success": True,
            "confirmation_id": confirmation_id,
            "amount_charged": session["total"],
            "currency": session["currency"],
        }

    def cancel_checkout(self, checkout_session_id: str) -> None:
        self._sessions.pop(checkout_session_id, None)
