"""Unit tests for the authority boundary manager."""

import pytest

from core.authority import AuthorityBoundary, BoundaryViolation, ResourceLimit
from core.types import Action


def _action(amount=None, scope=None, session_id="s1", step_id="step_a"):
    return Action(
        session_id=session_id,
        step_id=step_id,
        protocol="test",
        operation="op",
        amount=amount,
        scope=scope,
    )


@pytest.fixture
def boundary():
    return AuthorityBoundary(
        resource_limits={
            "spend": ResourceLimit(
                name="spend",
                max_per_action=100.0,
                max_cumulative=300.0,
            )
        },
        allowed_scopes=["electronics", "clothing"],
        blocked_scopes=["gambling"],
        session_ttl_seconds=1800,
        requires_approval_above=50.0,
    )


# ---------------------------------------------------------------------------
# validate
# ---------------------------------------------------------------------------


def test_validate_allows_within_limit(boundary):
    action = _action(amount=50.0, scope="electronics")
    assert boundary.validate_action(action)


def test_validate_blocks_blocked_scope(boundary):
    action = _action(scope="gambling")
    with pytest.raises(BoundaryViolation) as exc_info:
        boundary.validate_action(action)
    assert "blocked" in str(exc_info.value).lower()


def test_validate_blocks_unknown_scope(boundary):
    action = _action(scope="crypto")
    with pytest.raises(BoundaryViolation) as exc_info:
        boundary.validate_action(action)
    assert "allowed" in str(exc_info.value).lower()


def test_validate_allows_no_scope_when_allowed_list_empty():
    b = AuthorityBoundary(
        resource_limits={},
        allowed_scopes=[],
    )
    b.validate_action(_action(scope="anything"))


def test_validate_blocks_per_action_limit(boundary):
    action = _action(amount=150.0)  # > max_per_action=100
    with pytest.raises(BoundaryViolation) as exc_info:
        boundary.validate_action(action)
    assert "per-action" in str(exc_info.value).lower()


def test_validate_blocks_cumulative_limit(boundary):
    boundary.resource_limits["spend"].current_cumulative = 250.0
    action = _action(amount=60.0)  # 250 + 60 > 300
    with pytest.raises(BoundaryViolation):
        boundary.validate_action(action)


def test_validate_raises_when_revoked(boundary):
    boundary.revoke()
    with pytest.raises(BoundaryViolation, match="revoked"):
        boundary.validate_action(_action(amount=10.0))


# ---------------------------------------------------------------------------
# consume
# ---------------------------------------------------------------------------


def test_consume_updates_cumulative(boundary):
    action = _action(amount=75.0)
    boundary.consume(action)
    assert boundary.resource_limits["spend"].current_cumulative == 75.0


def test_consume_no_amount_is_noop(boundary):
    boundary.consume(_action())
    assert boundary.resource_limits["spend"].current_cumulative == 0.0


# ---------------------------------------------------------------------------
# check_proximity
# ---------------------------------------------------------------------------


def test_check_proximity_below_threshold(boundary):
    boundary.resource_limits["spend"].current_cumulative = 200.0  # 66%
    assert boundary.check_proximity(80.0) == []


def test_check_proximity_at_threshold(boundary):
    boundary.resource_limits["spend"].current_cumulative = 240.0  # 80%
    near = boundary.check_proximity(80.0)
    assert "spend" in near


# ---------------------------------------------------------------------------
# requires_approval
# ---------------------------------------------------------------------------


def test_requires_approval_above_threshold(boundary):
    assert boundary.requires_approval(_action(amount=51.0)) is True


def test_no_approval_below_threshold(boundary):
    assert boundary.requires_approval(_action(amount=49.0)) is False


def test_no_approval_when_threshold_none():
    b = AuthorityBoundary(requires_approval_above=None)
    assert b.requires_approval(_action(amount=1000.0)) is False


# ---------------------------------------------------------------------------
# lock / immutability
# ---------------------------------------------------------------------------


def test_boundary_is_revocable(boundary):
    boundary.revoke()
    assert boundary.is_revoked is True
