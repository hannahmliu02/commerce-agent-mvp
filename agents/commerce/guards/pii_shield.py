"""PIIShield — detects and redacts PII in outbound responses."""

from __future__ import annotations

import re
from typing import Any

from core.governance import Guard
from core.types import Direction, GuardOutcome, GuardResult, Message, SessionContext

REDACTED = "[REDACTED]"


def _luhn_check(number: str) -> bool:
    """Validate a credit-card number with the Luhn algorithm."""
    digits = [int(d) for d in number if d.isdigit()]
    if len(digits) < 13:
        return False
    total = 0
    for i, d in enumerate(reversed(digits)):
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


# Regex patterns (applied in order; each replaces with REDACTED)
_PII_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("credit_card", re.compile(r"\b(?:\d[ -]?){13,16}\b")),  # pre-filter by Luhn
    ("ssn", re.compile(r"\b\d{3}[- ]\d{2}[- ]\d{4}\b")),
    ("email", re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")),
    ("phone_us", re.compile(r"\b(?:\+1[\s.-]?)?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}\b")),
    ("ip_address", re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")),
]


def _redact_text(text: str) -> tuple[str, list[str]]:
    detected: list[str] = []
    for label, pattern in _PII_PATTERNS:
        def replacer(m: re.Match, lbl: str = label) -> str:
            raw = m.group(0).replace(" ", "").replace("-", "")
            if lbl == "credit_card" and not _luhn_check(raw):
                return m.group(0)  # Not a valid card — leave it
            detected.append(lbl)
            return REDACTED
        text = pattern.sub(replacer, text)
    return text, detected


def _redact_value(value: Any) -> tuple[Any, list[str]]:
    if isinstance(value, str):
        return _redact_text(value)
    if isinstance(value, dict):
        new_dict: dict = {}
        all_detected: list[str] = []
        for k, v in value.items():
            new_v, det = _redact_value(v)
            new_dict[k] = new_v
            all_detected.extend(det)
        return new_dict, all_detected
    if isinstance(value, list):
        new_list: list = []
        all_detected = []
        for item in value:
            new_item, det = _redact_value(item)
            new_list.append(new_item)
            all_detected.extend(det)
        return new_list, all_detected
    return value, []


class PIIShield(Guard):
    name = "PIIShield"
    direction = Direction.OUTBOUND
    priority = 10

    async def inspect(self, message: Message, context: SessionContext) -> GuardResult:
        redacted, detected = _redact_value(message.content)
        if detected:
            return GuardResult(
                outcome=GuardOutcome.MODIFY,
                guard_name=self.name,
                reason=f"PII redacted: {', '.join(set(detected))}",
                modified_content=redacted,
                metadata={"pii_types": list(set(detected))},
            )
        return GuardResult(
            outcome=GuardOutcome.PASS,
            guard_name=self.name,
        )
