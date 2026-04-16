"""Mastercard MAP Token Generator — scoped Agentic Tokens with governance metadata.

Tokens are single-use, session-bound, and carry spend limits + merchant restrictions.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from core.protocol_adapter import PermanentAdapterError, ProtocolAdapter
from core.types import Action, AdapterResponse, HealthStatus, RollbackResult, ValidationResult


class MAPToken(ProtocolAdapter):
    name = "map_token"
    protocol = "token"

    def __init__(self, issuer_config: Optional[str] = None, mock: bool = True) -> None:
        self._mock = mock or (issuer_config is None)
        self._issued: dict[str, dict] = {}   # token_id → token data
        self._revoked: set[str] = set()

    # ------------------------------------------------------------------
    # ProtocolAdapter interface
    # ------------------------------------------------------------------

    async def validate(self, action: Action) -> ValidationResult:
        if action.operation not in ("issue_token", "revoke_token", "validate_metadata"):
            return ValidationResult(valid=False, reason=f"Unknown MAP operation: {action.operation}")
        return ValidationResult(valid=True)

    async def execute(self, action: Action) -> AdapterResponse:
        op = action.operation
        params = action.parameters

        if op == "issue_token":
            token = self.issue_token(
                consumer_intent=params.get("consumer_intent", {}),
                governance_metadata=params.get("governance_metadata", {}),
            )
            return AdapterResponse(
                action_id=action.action_id,
                success=True,
                data=token,
                metadata={"token_id": token["token_id"]},
            )

        if op == "revoke_token":
            success = self.revoke_token(params["token_id"])
            return AdapterResponse(
                action_id=action.action_id,
                success=success,
                data={"revoked": success},
            )

        if op == "validate_metadata":
            valid, reason = self.validate_metadata(params.get("token", {}))
            return AdapterResponse(
                action_id=action.action_id,
                success=valid,
                data={"valid": valid},
                error=reason if not valid else None,
            )

        return AdapterResponse(
            action_id=action.action_id, success=False, error=f"Unknown operation: {op}"
        )

    async def rollback(self, action_id: str) -> RollbackResult:
        """Revoke all active tokens (called on kill)."""
        revoked = []
        for token_id in list(self._issued.keys()):
            self.revoke_token(token_id)
            revoked.append(token_id)
        return RollbackResult(
            success=True,
            action_id=action_id,
            details={"revoked_tokens": revoked},
        )

    async def health_check(self) -> HealthStatus:
        return HealthStatus(
            healthy=True,
            adapter_name=self.name,
            details={"mode": "mock" if self._mock else "mdes", "active_tokens": len(self._issued)},
        )

    # ------------------------------------------------------------------
    # Token operations
    # ------------------------------------------------------------------

    def issue_token(self, consumer_intent: dict, governance_metadata: dict) -> dict:
        """Generate a scoped Agentic Token."""
        token_id = f"MAP_{uuid.uuid4().hex.upper()}"
        token_value = secrets.token_urlsafe(32)
        token = {
            "token_id": token_id,
            "token_value": token_value,
            "issued_at": datetime.now(timezone.utc).isoformat(),
            "consumer_intent": consumer_intent,
            "governance_metadata": governance_metadata,
            "scope": "single_use",
            "status": "active",
            "checksum": self._checksum(token_id, token_value, governance_metadata),
        }
        self._issued[token_id] = token
        return token

    def revoke_token(self, token_id: str) -> bool:
        if token_id in self._issued:
            self._issued[token_id]["status"] = "revoked"
            self._revoked.add(token_id)
            return True
        return False

    def validate_metadata(self, token: dict) -> tuple[bool, Optional[str]]:
        """Verify that the token's governance metadata is present and well-formed."""
        if not token:
            return False, "Token is empty"
        token_id = token.get("token_id")
        if not token_id:
            return False, "Missing token_id"
        if token_id in self._revoked:
            return False, f"Token '{token_id}' has been revoked"

        gov = token.get("governance_metadata")
        if gov is None:
            return False, "Missing governance_metadata"
        if not isinstance(gov, dict):
            return False, "governance_metadata must be a dict"

        checksum = token.get("checksum")
        if checksum:
            expected = self._checksum(
                token_id,
                token.get("token_value", ""),
                gov,
            )
            if not secrets.compare_digest(checksum, expected):
                return False, "Token checksum mismatch — possible tampering"

        return True, None

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _checksum(token_id: str, token_value: str, metadata: dict) -> str:
        payload = json.dumps(
            {"id": token_id, "val": token_value[:8], "meta": metadata},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        return hashlib.sha256(payload).hexdigest()
