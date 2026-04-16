"""Named agent personas for the Commerce domain.

Built-in personas (Dina, Susan) are defined here.
Custom personas are stored in ~/.trustx/personas.json and merged at runtime,
so anyone can create their own named agent with `trustx create-agent`.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

# Where user-created personas are persisted
_CUSTOM_PERSONAS_FILE = Path.home() / ".trustx" / "personas.json"

# Rich color names cycled for auto-assigned colors
_COLOR_CYCLE = ["green", "yellow", "blue", "red", "bright_cyan", "bright_magenta", "bright_green"]


@dataclass
class AgentPersona:
    """Metadata describing a named commerce agent."""

    name: str
    tagline: str
    description: str
    emoji: str
    max_per_action: float
    max_cumulative: float
    requires_approval_above: float
    color: str  # rich color name used in the TUI
    custom: bool = False  # True for user-created personas


# ---------------------------------------------------------------------------
# Built-in personas
# ---------------------------------------------------------------------------

_BUILTIN_PERSONAS: dict[str, AgentPersona] = {
    "dina": AgentPersona(
        name="Dina",
        tagline="Your everyday smart shopper",
        description=(
            "Dina is your go-to commerce agent for everyday purchases. "
            "She's efficient, thorough, and keeps spending in check. "
            "Perfect for routine shopping under $500."
        ),
        emoji="🛍️",
        max_per_action=500.0,
        max_cumulative=1000.0,
        requires_approval_above=50.0,
        color="cyan",
    ),
    "susan": AgentPersona(
        name="Susan",
        tagline="Your premium high-value purchasing agent",
        description=(
            "Susan handles high-value and bulk purchases with precision. "
            "She applies stricter governance checks and always confirms "
            "before committing to large transactions."
        ),
        emoji="💼",
        max_per_action=2000.0,
        max_cumulative=5000.0,
        requires_approval_above=200.0,
        color="magenta",
    ),
}


# ---------------------------------------------------------------------------
# Custom persona persistence
# ---------------------------------------------------------------------------

def _load_custom_personas() -> dict[str, AgentPersona]:
    """Load user-created personas from ~/.trustx/personas.json."""
    if not _CUSTOM_PERSONAS_FILE.exists():
        return {}
    try:
        raw = json.loads(_CUSTOM_PERSONAS_FILE.read_text())
        return {
            k: AgentPersona(**{**v, "custom": True})
            for k, v in raw.items()
        }
    except Exception:
        return {}


def _save_custom_personas(custom: dict[str, AgentPersona]) -> None:
    """Persist user-created personas to ~/.trustx/personas.json."""
    _CUSTOM_PERSONAS_FILE.parent.mkdir(parents=True, exist_ok=True)
    data = {
        k: {f: getattr(v, f) for f in (
            "name", "tagline", "description", "emoji",
            "max_per_action", "max_cumulative", "requires_approval_above", "color",
        )}
        for k, v in custom.items()
    }
    _CUSTOM_PERSONAS_FILE.write_text(json.dumps(data, indent=2))


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def all_personas() -> dict[str, AgentPersona]:
    """Return built-in personas merged with any user-created ones."""
    merged = dict(_BUILTIN_PERSONAS)
    merged.update(_load_custom_personas())
    return merged


def get_persona(name: str) -> AgentPersona:
    personas = all_personas()
    key = name.lower()
    if key not in personas:
        available = ", ".join(personas.keys())
        raise ValueError(f"Unknown agent '{name}'. Available: {available}")
    return personas[key]


def list_personas() -> list[AgentPersona]:
    return list(all_personas().values())


def create_persona(
    name: str,
    tagline: str = "",
    description: str = "",
    emoji: str = "🤖",
    max_per_action: float = 500.0,
    max_cumulative: float = 1000.0,
    requires_approval_above: float = 50.0,
    color: Optional[str] = None,
) -> AgentPersona:
    """Create and persist a new custom persona. Raises ValueError if name is taken."""
    key = name.lower()
    if key in _BUILTIN_PERSONAS:
        raise ValueError(f"'{name}' is a built-in agent and cannot be overwritten.")

    custom = _load_custom_personas()
    if key in custom:
        raise ValueError(f"An agent named '{name}' already exists. Delete it first with `trustx delete-agent --name {name}`.")

    if color is None:
        color = _COLOR_CYCLE[len(custom) % len(_COLOR_CYCLE)]

    persona = AgentPersona(
        name=name.capitalize(),
        tagline=tagline or f"{name.capitalize()}'s commerce agent",
        description=description or f"{name.capitalize()} is a custom commerce agent.",
        emoji=emoji,
        max_per_action=max_per_action,
        max_cumulative=max_cumulative,
        requires_approval_above=requires_approval_above,
        color=color,
        custom=True,
    )
    custom[key] = persona
    _save_custom_personas(custom)
    return persona


def delete_persona(name: str) -> None:
    """Remove a user-created persona. Raises ValueError if built-in or not found."""
    key = name.lower()
    if key in _BUILTIN_PERSONAS:
        raise ValueError(f"'{name}' is a built-in agent and cannot be deleted.")
    custom = _load_custom_personas()
    if key not in custom:
        raise ValueError(f"No custom agent named '{name}' found.")
    del custom[key]
    _save_custom_personas(custom)
