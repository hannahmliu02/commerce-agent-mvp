"""Commerce Agent default authority boundaries (Section 4.3)."""

from __future__ import annotations

from core.authority import AuthorityBoundary, ResourceLimit
from core.types import TokenScopeConfig, TokenScopeType


def default_commerce_boundary(
    max_per_action: float = 500.0,
    max_cumulative: float = 1000.0,
    allowed_categories: list[str] | None = None,
    blocked_categories: list[str] | None = None,
    session_ttl_seconds: int = 1800,
    requires_approval_above: float = 0.0,
) -> AuthorityBoundary:
    """Build the default commerce authority boundary.

    All parameters are configurable per consumer.
    """
    return AuthorityBoundary(
        resource_limits={
            "spend": ResourceLimit(
                name="spend",
                max_per_action=max_per_action,
                max_cumulative=max_cumulative,
            )
        },
        allowed_scopes=allowed_categories or [],
        blocked_scopes=blocked_categories or [],
        session_ttl_seconds=session_ttl_seconds,
        requires_approval_above=requires_approval_above,
        token_scope=TokenScopeConfig(
            scope_type=TokenScopeType.SINGLE_USE,
            max_uses=1,
            bound_to_session=True,
        ),
    )
