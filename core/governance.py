"""Governance Shield — composable middleware pipeline for every message.

Each guard inspects, modifies, or blocks. Guards execute in priority order.
BLOCK halts immediately; MODIFY propagates the changed message forward.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Optional

from .types import Direction, GuardOutcome, GuardResult, Message, SessionContext

logger = logging.getLogger(__name__)

# Guards that are always active for Tier-3 agents and cannot be disabled
MANDATORY_GUARDS = {"PromptInjectionGuard", "PIIShield"}


# ---------------------------------------------------------------------------
# Abstract Guard
# ---------------------------------------------------------------------------


class Guard(ABC):
    name: str
    direction: Direction
    priority: int  # lower number = executes first

    @abstractmethod
    async def inspect(self, message: Message, context: SessionContext) -> GuardResult:
        """
        Returns one of:
          PASS   – message is clean, continue pipeline
          MODIFY – message was modified, continue with modified version
          BLOCK  – message is rejected, halt pipeline, log event, escalate
        """


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------


class GuardPipeline:
    """Ordered pipeline of guards; runs inbound or outbound guards as appropriate."""

    def __init__(
        self,
        guards: list[Guard],
        mandatory_guard_names: Optional[set[str]] = None,
    ) -> None:
        self._guards = sorted(guards, key=lambda g: g.priority)
        self._mandatory = mandatory_guard_names or MANDATORY_GUARDS
        self._validate_mandatory()

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    async def run(
        self, message: Message, context: SessionContext
    ) -> tuple[Message, list[GuardResult]]:
        """Run the pipeline for the given direction.

        Returns (final_message, list_of_guard_results).
        Raises PipelineBlockedError if any guard returns BLOCK.
        """
        results: list[GuardResult] = []
        current_message = message.model_copy(deep=True)

        for guard in self._applicable(message.direction):
            result = await guard.inspect(current_message, context)
            results.append(result)

            logger.debug(
                "Guard '%s' [%s] → %s",
                guard.name,
                message.direction,
                result.outcome,
            )

            if result.outcome == GuardOutcome.BLOCK:
                raise PipelineBlockedError(
                    guard_name=guard.name,
                    reason=result.reason or "blocked by guard",
                    results=results,
                )

            if result.outcome == GuardOutcome.MODIFY:
                current_message = current_message.model_copy(
                    update={"content": result.modified_content}
                )

        return current_message, results

    def add_guard(self, guard: Guard) -> None:
        self._guards.append(guard)
        self._guards.sort(key=lambda g: g.priority)

    def remove_guard(self, name: str) -> None:
        if name in self._mandatory:
            raise MandatoryGuardRemovalError(
                f"Guard '{name}' is mandatory and cannot be removed"
            )
        self._guards = [g for g in self._guards if g.name != name]

    def list_guards(self) -> list[dict]:
        return [
            {
                "name": g.name,
                "direction": g.direction,
                "priority": g.priority,
                "mandatory": g.name in self._mandatory,
            }
            for g in self._guards
        ]

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _applicable(self, direction: Direction) -> list[Guard]:
        return [
            g
            for g in self._guards
            if g.direction in (direction, Direction.BOTH)
        ]

    def _validate_mandatory(self) -> None:
        present = {g.name for g in self._guards}
        missing = self._mandatory - present
        if missing:
            logger.warning(
                "Mandatory guards not registered: %s — they will not be enforced",
                missing,
            )


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class PipelineBlockedError(Exception):
    def __init__(
        self,
        guard_name: str,
        reason: str,
        results: Optional[list[GuardResult]] = None,
    ) -> None:
        super().__init__(f"Pipeline blocked by '{guard_name}': {reason}")
        self.guard_name = guard_name
        self.reason = reason
        self.results = results or []


class MandatoryGuardRemovalError(Exception):
    pass
