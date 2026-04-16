"""Protocol Adapter Layer — standard interface for all external systems.

Domain agents register adapters; the Mother Agent calls them via a uniform API.
"""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod

from .types import (
    Action,
    AdapterResponse,
    AdapterStatus,
    HealthStatus,
    RollbackResult,
    ValidationResult,
)

logger = logging.getLogger(__name__)

# Retry configuration
MAX_RETRIES = 3
BASE_BACKOFF_S = 1.0
TRANSIENT_ERRORS = {"timeout", "rate_limit", "network_error", "service_unavailable"}


# ---------------------------------------------------------------------------
# Abstract base
# ---------------------------------------------------------------------------


class ProtocolAdapter(ABC):
    """Every external connector implements this interface."""

    name: str
    protocol: str

    @abstractmethod
    async def execute(self, action: Action) -> AdapterResponse:
        """Execute an action through this adapter."""

    @abstractmethod
    async def validate(self, action: Action) -> ValidationResult:
        """Pre-validate an action before execution."""

    @abstractmethod
    async def rollback(self, action_id: str) -> RollbackResult:
        """Reverse a previously executed action."""

    @abstractmethod
    async def health_check(self) -> HealthStatus:
        """Check if the external system is reachable."""

    async def execute_with_retry(self, action: Action) -> AdapterResponse:
        """Execute with exponential back-off on transient errors."""
        last_error: Exception | None = None
        for attempt in range(MAX_RETRIES):
            try:
                response = await self.execute(action)
                if not response.success and response.retryable:
                    raise TransientAdapterError(response.error or "retryable failure")
                return response
            except TransientAdapterError as exc:
                last_error = exc
                wait = BASE_BACKOFF_S * (2**attempt)
                logger.warning(
                    "Adapter '%s' transient error (attempt %d/%d): %s — retrying in %.1fs",
                    self.name,
                    attempt + 1,
                    MAX_RETRIES,
                    exc,
                    wait,
                )
                await asyncio.sleep(wait)
            except PermanentAdapterError:
                raise
        raise MaxRetriesExceededError(
            f"Adapter '{self.name}' failed after {MAX_RETRIES} retries: {last_error}"
        )


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class AdapterRegistry:
    """Maintains the set of registered protocol adapters."""

    def __init__(self) -> None:
        self._adapters: dict[str, ProtocolAdapter] = {}

    def register(self, adapter: ProtocolAdapter) -> None:
        if adapter.protocol in self._adapters:
            raise DuplicateAdapterError(
                f"An adapter for protocol '{adapter.protocol}' is already registered"
            )
        self._adapters[adapter.protocol] = adapter
        logger.info("Registered adapter '%s' for protocol '%s'", adapter.name, adapter.protocol)

    def get(self, protocol: str) -> ProtocolAdapter:
        if protocol not in self._adapters:
            raise AdapterNotFound(f"No adapter registered for protocol '{protocol}'")
        return self._adapters[protocol]

    async def health_check_all(self) -> dict[str, HealthStatus]:
        results: dict[str, HealthStatus] = {}
        for protocol, adapter in self._adapters.items():
            try:
                results[protocol] = await adapter.health_check()
            except Exception as exc:
                results[protocol] = HealthStatus(
                    healthy=False,
                    adapter_name=adapter.name,
                    details={"error": str(exc)},
                )
        return results

    def all_healthy(self, statuses: dict[str, HealthStatus]) -> bool:
        return all(s.healthy for s in statuses.values())

    def list_protocols(self) -> list[str]:
        return list(self._adapters.keys())


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class AdapterError(Exception):
    pass


class TransientAdapterError(AdapterError):
    pass


class PermanentAdapterError(AdapterError):
    pass


class MaxRetriesExceededError(AdapterError):
    pass


class AdapterNotFound(AdapterError):
    pass


class DuplicateAdapterError(AdapterError):
    pass
