"""Unit tests for the state machine engine."""

import pytest

from core.state_machine import (
    ApprovalError,
    ConditionError,
    FlowGraph,
    StateMachineError,
    Step,
)
from core.types import SessionContext, SessionStatus


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _noop(ctx, inputs):
    return {"done": True}


async def _rollback_noop(ctx, result):
    pass


def _always_true(ctx):
    return True


def _always_false(ctx):
    return False


@pytest.fixture
def ctx():
    return SessionContext(session_id="s1", domain="test", status=SessionStatus.PENDING)


# ---------------------------------------------------------------------------
# start
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_transitions_to_active(ctx):
    flow = FlowGraph([Step(id="a", name="A", handler=_noop)])
    await flow.start(ctx)
    assert flow.status == SessionStatus.ACTIVE
    assert flow.current_step == "a"


@pytest.mark.asyncio
async def test_start_fails_if_already_active(ctx):
    flow = FlowGraph([Step(id="a", name="A", handler=_noop)])
    await flow.start(ctx)
    with pytest.raises(StateMachineError):
        await flow.start(ctx)


# ---------------------------------------------------------------------------
# advance
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_advance_moves_to_next_step(ctx):
    flow = FlowGraph([
        Step(id="a", name="A", handler=_noop),
        Step(id="b", name="B", handler=_noop),
    ])
    await flow.start(ctx)
    await flow.execute_current(ctx, {})
    next_id = await flow.advance(ctx)
    assert next_id == "b"
    assert flow.current_step == "b"


@pytest.mark.asyncio
async def test_advance_completes_on_last_step(ctx):
    flow = FlowGraph([Step(id="only", name="Only", handler=_noop)])
    await flow.start(ctx)
    await flow.execute_current(ctx, {})
    next_id = await flow.advance(ctx)
    assert next_id is None
    assert flow.status == SessionStatus.COMPLETED


@pytest.mark.asyncio
async def test_exit_condition_failure_blocks_advance(ctx):
    flow = FlowGraph([
        Step(id="a", name="A", handler=_noop, exit_conditions=[_always_false]),
        Step(id="b", name="B", handler=_noop),
    ])
    await flow.start(ctx)
    await flow.execute_current(ctx, {})
    with pytest.raises(ConditionError):
        await flow.advance(ctx)


@pytest.mark.asyncio
async def test_entry_condition_failure_blocks_advance(ctx):
    flow = FlowGraph([
        Step(id="a", name="A", handler=_noop),
        Step(id="b", name="B", handler=_noop, entry_conditions=[_always_false]),
    ])
    await flow.start(ctx)
    await flow.execute_current(ctx, {})
    with pytest.raises(ConditionError):
        await flow.advance(ctx)


# ---------------------------------------------------------------------------
# out-of-order rejection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cannot_advance_when_paused(ctx):
    flow = FlowGraph([
        Step(id="a", name="A", handler=_noop, requires_approval=True),
        Step(id="b", name="B", handler=_noop),
    ])
    await flow.start(ctx)
    await flow.execute_current(ctx, {})
    # Flow is now paused — advance must fail
    assert flow.status == SessionStatus.PAUSED
    with pytest.raises(StateMachineError):
        await flow.advance(ctx)


# ---------------------------------------------------------------------------
# rollback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rollback_moves_back_one_step(ctx):
    flow = FlowGraph([
        Step(id="a", name="A", handler=_noop, rollback_handler=_rollback_noop),
        Step(id="b", name="B", handler=_noop),
    ])
    await flow.start(ctx)
    await flow.execute_current(ctx, {})
    await flow.advance(ctx)
    assert flow.current_step == "b"
    await flow.rollback(ctx)
    assert flow.current_step == "a"


# ---------------------------------------------------------------------------
# resume
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resume_with_valid_token(ctx):
    flow = FlowGraph([
        Step(id="a", name="A", handler=_noop, requires_approval=True),
        Step(id="b", name="B", handler=_noop),
    ])
    await flow.start(ctx)
    await flow.execute_current(ctx, {})
    assert flow.status == SessionStatus.PAUSED
    await flow.resume("valid_token_123", ctx)
    assert flow.status == SessionStatus.ACTIVE


@pytest.mark.asyncio
async def test_resume_with_invalid_token_raises(ctx):
    flow = FlowGraph([
        Step(id="a", name="A", handler=_noop, requires_approval=True),
    ])
    await flow.start(ctx)
    await flow.execute_current(ctx, {})
    with pytest.raises(ApprovalError):
        await flow.resume("short", ctx)  # too short


# ---------------------------------------------------------------------------
# kill
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_kill_sets_status_killed(ctx):
    flow = FlowGraph([Step(id="a", name="A", handler=_noop)])
    await flow.start(ctx)
    await flow.kill(ctx, "operator_1")
    assert flow.status == SessionStatus.KILLED


@pytest.mark.asyncio
async def test_no_transitions_after_kill(ctx):
    flow = FlowGraph([
        Step(id="a", name="A", handler=_noop),
        Step(id="b", name="B", handler=_noop),
    ])
    await flow.start(ctx)
    await flow.kill(ctx, "op")
    with pytest.raises(StateMachineError):
        await flow.execute_current(ctx, {})


# ---------------------------------------------------------------------------
# history immutability
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_history_is_append_only(ctx):
    flow = FlowGraph([Step(id="a", name="A", handler=_noop)])
    await flow.start(ctx)
    h = flow.history
    assert len(h) >= 1
    # Modifying the returned list should not affect the internal history
    h.clear()
    assert len(flow.history) >= 1
