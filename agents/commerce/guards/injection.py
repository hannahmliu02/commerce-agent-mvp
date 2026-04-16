"""Prompt injection detection guards.

PromptInjectionGuard — detects adversarial instructions in inbound content.
MerchantCatalogIntegrity — detects embedded adversarial instructions in product data.
"""

from __future__ import annotations

import re

from core.governance import Guard
from core.types import Direction, GuardOutcome, GuardResult, Message, SessionContext

# Patterns that suggest prompt injection attempts
_INJECTION_PATTERNS: list[re.Pattern] = [
    # Instruction overrides — allow multiple modifiers ("all previous", "all prior above", etc.)
    re.compile(r"ignore\s+(?:(?:all|previous|prior|above|your)\s+)+(instructions?|rules?|guidelines?|constraints?)", re.I),
    re.compile(r"disregard\s+(?:(?:all|previous|prior|above|your)\s+)+(instructions?|rules?|constraints?)", re.I),
    re.compile(r"forget (everything|all|your instructions?|your rules?)", re.I),
    # Role-play attacks
    re.compile(r"\bdeveloper mode\b", re.I),
    re.compile(r"\bdan mode\b", re.I),
    re.compile(r"\bjailbreak\b", re.I),
    re.compile(r"act as (if you (are|were)|an?)\s+(unrestricted|unfiltered|evil|malicious)", re.I),
    re.compile(r"you are now (a|an)\s+\w+\s+(with no|without any)\s+(restrictions?|limits?)", re.I),
    # Delimiter manipulation
    re.compile(r"---\s*system\s*---", re.I),
    re.compile(r"<\s*/?system\s*>", re.I),
    re.compile(r"\[INST\]|\[/INST\]"),
    re.compile(r"<\|im_start\|>|<\|im_end\|>"),
    # Direct override attempts
    re.compile(r"new instructions?:\s", re.I),
    re.compile(r"updated (system )?prompt:\s", re.I),
    re.compile(r"from now on (you (will|must|should)|always)", re.I),
    # Hidden text attacks (whitespace injection)
    re.compile(r"\u200b|\u200c|\u200d|\ufeff"),  # zero-width chars
]

_SCORE_THRESHOLD = 1  # one match is enough to block


class PromptInjectionGuard(Guard):
    name = "PromptInjectionGuard"
    direction = Direction.INBOUND
    priority = 1  # Run first

    def __init__(self, threshold: int = _SCORE_THRESHOLD) -> None:
        self._threshold = threshold

    async def inspect(self, message: Message, context: SessionContext) -> GuardResult:
        text = _extract_text(message.content)
        score = sum(1 for p in _INJECTION_PATTERNS if p.search(text))
        if score >= self._threshold:
            return GuardResult(
                outcome=GuardOutcome.BLOCK,
                guard_name=self.name,
                reason=f"Prompt injection detected (score={score})",
                metadata={"score": score},
            )
        return GuardResult(
            outcome=GuardOutcome.PASS,
            guard_name=self.name,
            metadata={"score": score},
        )


class MerchantCatalogIntegrity(Guard):
    """Applies content-integrity checks to merchant catalog data.

    Detects embedded adversarial instructions in product descriptions
    that could be used for indirect prompt injection.
    """

    name = "MerchantCatalogIntegrity"
    direction = Direction.INBOUND
    priority = 3

    # Patterns that suggest instruction injection via product data
    import re as _re
    _SUSPICIOUS = [
        _re.compile(r"ignore\s+(?:(?:all|previous|prior|above|your)\s+)+(instructions?|rules?)", _re.I),
        _re.compile(r"<script", _re.I),
        _re.compile(r"javascript:", _re.I),
        _re.compile(r"\bsystem:\s*you (are|must|should|will)\b", _re.I),
        _re.compile(r"\bnew goal:\s", _re.I),
    ]

    async def inspect(self, message: Message, context: SessionContext) -> GuardResult:
        content = message.content

        # Only apply to catalog responses
        if not isinstance(content, dict) or "products" not in content:
            return GuardResult(outcome=GuardOutcome.PASS, guard_name=self.name)

        products = content.get("products", [])
        for product in products:
            if not isinstance(product, dict):
                continue
            text = " ".join(str(v) for v in product.values())
            for pattern in self._SUSPICIOUS:
                if pattern.search(text):
                    return GuardResult(
                        outcome=GuardOutcome.BLOCK,
                        guard_name=self.name,
                        reason=f"Adversarial instruction detected in catalog product '{product.get('id', '?')}'",
                        metadata={"product_id": product.get("id")},
                    )

        return GuardResult(outcome=GuardOutcome.PASS, guard_name=self.name)


def _extract_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        return " ".join(str(v) for v in content.values())
    if isinstance(content, (list, tuple)):
        return " ".join(str(v) for v in content)
    return str(content)
