"""Visa TAP Signer — RFC 9421 HTTP Message Signatures using Ed25519.

Signs outbound agent-to-merchant requests. Verifies inbound merchant responses.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from core.protocol_adapter import PermanentAdapterError, ProtocolAdapter
from core.types import Action, AdapterResponse, HealthStatus, RollbackResult, ValidationResult


class TAPSigner(ProtocolAdapter):
    name = "tap_signer"
    protocol = "authentication"

    def __init__(
        self,
        keypair_path: Optional[str] = None,
        mock: bool = True,
    ) -> None:
        self._mock = mock or (keypair_path is None)
        self._keypair_path = Path(keypair_path) if keypair_path else None
        self._private_key: Any = None
        self._public_key: Any = None

        if not self._mock:
            self._load_or_generate_keypair()

    # ------------------------------------------------------------------
    # ProtocolAdapter interface
    # ------------------------------------------------------------------

    async def validate(self, action: Action) -> ValidationResult:
        if action.operation not in ("sign", "verify", "generate_keypair"):
            return ValidationResult(valid=False, reason=f"Unknown TAP operation: {action.operation}")
        return ValidationResult(valid=True)

    async def execute(self, action: Action) -> AdapterResponse:
        op = action.operation
        params = action.parameters

        if op == "sign":
            signature = self.sign(params)
            return AdapterResponse(
                action_id=action.action_id,
                success=True,
                data={"signature": signature, "signed_headers": params.get("headers", {})},
                metadata={"algorithm": "ed25519" if not self._mock else "mock-hmac"},
            )

        if op == "verify":
            valid = self.verify(params)
            return AdapterResponse(
                action_id=action.action_id,
                success=valid,
                data={"verified": valid},
                error=None if valid else "Signature verification failed",
            )

        return AdapterResponse(
            action_id=action.action_id, success=False, error=f"Unknown operation: {op}"
        )

    async def rollback(self, action_id: str) -> RollbackResult:
        return RollbackResult(success=True, action_id=action_id)

    async def health_check(self) -> HealthStatus:
        has_key = self._mock or (self._private_key is not None)
        return HealthStatus(
            healthy=has_key,
            adapter_name=self.name,
            details={"mode": "mock" if self._mock else "ed25519", "has_key": has_key},
        )

    # ------------------------------------------------------------------
    # Signing / verification
    # ------------------------------------------------------------------

    def sign(self, request: dict) -> str:
        """Generate an RFC 9421-style signature for the request dict."""
        if self._mock:
            return self._mock_sign(request)

        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        payload = self._canonical_bytes(request)
        signature_bytes = self._private_key.sign(payload)
        return base64.b64encode(signature_bytes).decode()

    def verify(self, response: dict) -> bool:
        """Verify the TAP signature on an inbound response."""
        if self._mock:
            return self._mock_verify(response)

        sig_b64 = response.get("headers", {}).get("x-signature") or response.get("signature")
        if not sig_b64:
            return False
        try:
            sig_bytes = base64.b64decode(sig_b64)
            payload = self._canonical_bytes(response)
            self._public_key.verify(sig_bytes, payload)
            return True
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Key management
    # ------------------------------------------------------------------

    def _load_or_generate_keypair(self) -> None:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from cryptography.hazmat.primitives.serialization import (
            Encoding,
            NoEncryption,
            PrivateFormat,
            PublicFormat,
        )

        priv_path = self._keypair_path
        if priv_path and priv_path.exists():
            from cryptography.hazmat.primitives.serialization import load_pem_private_key
            pem = priv_path.read_bytes()
            self._private_key = load_pem_private_key(pem, password=None)
        else:
            self._private_key = Ed25519PrivateKey.generate()
            if priv_path:
                priv_path.parent.mkdir(parents=True, exist_ok=True)
                priv_path.write_bytes(
                    self._private_key.private_bytes(
                        Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()
                    )
                )
        self._public_key = self._private_key.public_key()

    # ------------------------------------------------------------------
    # Mock helpers
    # ------------------------------------------------------------------

    _MOCK_SECRET = b"trustx-tap-mock-secret-key"

    def _mock_sign(self, request: dict) -> str:
        payload = self._canonical_bytes(request)
        sig = hmac.new(self._MOCK_SECRET, payload, hashlib.sha256).digest()
        return base64.b64encode(sig).decode()

    def _mock_verify(self, response: dict) -> bool:
        sig_b64 = response.get("headers", {}).get("x-signature") or response.get("signature")
        if not sig_b64:
            return True  # No signature present — pass in mock mode
        expected = self._mock_sign(response)
        try:
            return hmac.compare_digest(sig_b64, expected)
        except Exception:
            return False

    @staticmethod
    def _canonical_bytes(data: dict) -> bytes:
        """Deterministic serialisation for signing/verification."""
        canonical = {
            k: data[k]
            for k in sorted(data.keys())
            if k not in ("signature", "x-signature")
        }
        return json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()
