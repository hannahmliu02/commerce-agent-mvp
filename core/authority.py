"""Authority Boundary Manager — per-session constraints on what the agent may do.

Every action is validated against the boundary before execution.
The boundary is immutable after session start.
"""

from __future__ import annotations

import threading
from typing import Optional

from pydantic import BaseModel, Field, PrivateAttr

from .types import Action, PolicyDecision, TokenScopeConfig, TokenScopeType


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


class ResourceLimit(BaseModel):
    name: str
    max_per_action: float
    max_cumulative: float
    current_cumulative: float = 0.0

    model_config = {"frozen": False}

    def would_exceed(self, amount: float) -> bool:
        return (self.current_cumulative + amount) > self.max_cumulative

    def would_exceed_per_action(self, amount: float) -> bool:
        return amount > self.max_per_action

    def consume(self, amount: float) -> None:
        self.current_cumulative += amount

    def proximity_pct(self) -> float:
        if self.max_cumulative == 0:
            return 100.0
        return (self.current_cumulative / self.max_cumulative) * 100.0


class BoundaryViolation(Exception):
    """Raised when an action violates the authority boundary."""

    def __init__(self, reason: str, resource: Optional[str] = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.resource = resource


class AuthorityBoundary(BaseModel):
    """Immutable-after-start session constraints."""

    resource_limits: dict[str, ResourceLimit] = Field(default_factory=dict)
    allowed_scopes: list[str] = Field(default_factory=list)
    blocked_scopes: list[str] = Field(default_factory=list)
    session_ttl_seconds: int = 1800
    requires_approval_above: Optional[float] = 0.0
    token_scope: TokenScopeConfig = Field(
        default_factory=lambda: TokenScopeConfig(scope_type=TokenScopeType.SINGLE_USE)
    )
    proximity_alert_threshold_pct: float = 80.0

    model_config = {"arbitrary_types_allowed": True}

    # Private mutable state — not part of the pydantic schema
    _locked: bool = PrivateAttr(default=False)
    _revoked: bool = PrivateAttr(default=False)
    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def lock(self) -> None:
        """Called at session start — boundary is immutable from this point."""
        self._locked = True

    def revoke(self) -> None:
        """Immediately invalidate; all subsequent actions rejected."""
        self._revoked = True

    @property
    def is_revoked(self) -> bool:
        return self._revoked

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    def validate_action(self, action: Action) -> PolicyDecision:
        """Check action against all boundary rules. Raises BoundaryViolation on deny."""
        if self._revoked:
            raise BoundaryViolation("Authority boundary has been revoked")

        # Scope checks
        if action.scope:
            if action.scope in self.blocked_scopes:
                raise BoundaryViolation(
                    f"Scope '{action.scope}' is explicitly blocked",
                    resource="scope",
                )
            if self.allowed_scopes and action.scope not in self.allowed_scopes:
                raise BoundaryViolation(
                    f"Scope '{action.scope}' is not in the allowed list",
                    resource="scope",
                )

        # Resource limit checks
        if action.amount is not None:
            for resource_name, limit in self.resource_limits.items():
                if limit.would_exceed_per_action(action.amount):
                    raise BoundaryViolation(
                        f"Action amount {action.amount} exceeds per-action limit "
                        f"{limit.max_per_action} for '{resource_name}'",
                        resource=resource_name,
                    )
                if limit.would_exceed(action.amount):
                    raise BoundaryViolation(
                        f"Action would push cumulative '{resource_name}' spend "
                        f"({limit.current_cumulative + action.amount:.2f}) above "
                        f"session cap {limit.max_cumulative}",
                        resource=resource_name,
                    )

        return PolicyDecision.ALLOW

    def requires_approval(self, action: Action) -> bool:
        """Return True if this action needs explicit human approval."""
        if self.requires_approval_above is None:
            return False
        if action.amount is None:
            return False
        return action.amount > self.requires_approval_above

    # ------------------------------------------------------------------
    # Consumption
    # ------------------------------------------------------------------

    def consume(self, action: Action) -> None:
        """Update cumulative counters after a successful action."""
        if action.amount is None:
            return
        with self._lock:  # type: ignore[attr-defined]
            for limit in self.resource_limits.values():
                limit.consume(action.amount)

    # ------------------------------------------------------------------
    # Proximity / alerting
    # ------------------------------------------------------------------

    def check_proximity(self, threshold_pct: Optional[float] = None) -> list[str]:
        """Return names of resources at or above the proximity threshold."""
        thr = threshold_pct if threshold_pct is not None else self.proximity_alert_threshold_pct
        return [
            name
            for name, limit in self.resource_limits.items()
            if limit.proximity_pct() >= thr
        ]
