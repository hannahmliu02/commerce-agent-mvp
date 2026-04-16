# TrustX Agent Framework

A domain-agnostic Python framework for building **governed, auditable AI agents**. TrustX enforces authority boundaries, runs every message through a composable governance pipeline, and records an immutable audit trail — before any action executes.

---

## Table of Contents

1. [What is TrustX?](#what-is-trustx)
2. [Quick Start](#quick-start)
3. [Interactive Demo](#interactive-demo)
4. [Named Agents](#named-agents)
5. [Architecture](#architecture)
6. [Core Modules](#core-modules)
7. [Commerce Agent](#commerce-agent)
8. [Guards](#guards)
9. [CLI Reference](#cli-reference)
10. [MCP Server](#mcp-server)
11. [Running Tests](#running-tests)
12. [Adding a New Domain Agent](#adding-a-new-domain-agent)

---

## What is TrustX?

TrustX solves the hardest problem in agentic AI: **how do you let an AI agent take real-world actions — like making purchases — while guaranteeing it stays within bounds?**

It does this through four interlocking layers:

| Layer | What it does |
|---|---|
| **State Machine** | Enforces a strict, ordered step sequence. No skipping, no rewinding without rollback. |
| **Authority Boundary** | Per-session, immutable spend caps and scope allow/block lists. Locked at session start. |
| **Governance Pipeline** | Every inbound and outbound message passes through a chain of guards. One BLOCK stops everything. |
| **Audit Logger** | Append-only, immutable event log. Every decision is recorded before the next step runs. |

---

## Quick Start

```bash
# Install
pip install -e ".[dev]"

# List available agents
trustx agents

# Run an interactive guided session
trustx interactive

# Run with a specific agent
trustx interactive --agent dina
trustx interactive --agent susan

# Start the MCP server (for Claude Code / VS Code integration)
trustx serve --agent dina
```

---

## Interactive Demo

The `interactive` command walks you through a full five-step commerce session in your terminal. It shows guards running, authority checks, approval gates, and the final audit trail — all in real time.

```
trustx interactive
```

You'll be prompted to:
1. Choose an agent (Dina or Susan)
2. Search for a product
3. Select an item
4. Approve the purchase
5. Watch the payment execute and audit record close

To skip the agent selection prompt:

```bash
trustx interactive --agent dina
trustx interactive --agent susan
```

---

## Named Agents

TrustX ships with two named commerce agent personas. Each has its own spend profile and approval threshold.

| Agent | Tagline | Per-action limit | Session cap | Approval above |
|---|---|---|---|---|
| 🛍️ **Dina** | Your everyday smart shopper | $500 | $1,000 | $50 |
| 💼 **Susan** | Your premium high-value purchasing agent | $2,000 | $5,000 | $200 |

**Dina** is the default. She's optimized for routine everyday purchases with conservative spend limits and frequent approval prompts.

**Susan** handles high-value and bulk purchases. She applies the same governance rules but at a higher spend threshold, suitable for B2B or premium consumer flows.

```bash
# See all agents
trustx agents

# Use a specific agent in the MCP server
trustx serve --agent susan
```

Personas are defined in [`agents/commerce/personas.py`](agents/commerce/personas.py). Add a new entry to the `PERSONAS` dict to create your own.

---

## Architecture

```
trustx-agent/
├── core/                        # Domain-agnostic engine
│   ├── types.py                 # Shared enums and Pydantic models
│   ├── state_machine.py         # Ordered step execution, rollback, approval gates
│   ├── authority.py             # Immutable per-session resource limits and scope rules
│   ├── governance.py            # Guard pipeline (PASS / MODIFY / BLOCK)
│   ├── audit.py                 # Append-only immutable event log
│   ├── protocol_adapter.py      # Abstract adapter interface + registry
│   ├── session.py               # SessionManager — ties everything together
│   └── mcp_server.py            # MCP server exposing 8 agent tools
│
├── agents/
│   └── commerce/                # Commerce domain agent
│       ├── flow.py              # Five-step commerce flow graph
│       ├── config.py            # Authority boundary factory
│       ├── personas.py          # Named agent personas (Dina, Susan)
│       ├── adapters/
│       │   ├── acp_client.py    # Agentic Commerce Protocol (browse, checkout, pay)
│       │   ├── stripe.py        # Stripe PaymentIntent adapter
│       │   ├── tap_signer.py    # Visa TAP — RFC 9421 request signing
│       │   └── map_token.py     # Mastercard MAP — scoped Agentic Token lifecycle
│       └── guards/
│           ├── injection.py     # PromptInjectionGuard + MerchantCatalogIntegrity
│           ├── pii_shield.py    # PIIShield — redacts PII from outbound responses
│           └── mandate.py       # MandateEnforcer + TAPSignatureGuard + MAPTokenValidator
│
├── cli/
│   └── main.py                  # CLI commands including `interactive` and `agents`
│
└── tests/
    ├── test_state_machine.py
    ├── test_authority.py
    ├── test_governance.py
    ├── test_audit.py
    └── test_commerce_flow.py
```

---

## Core Modules

### `core/state_machine.py` — Step Sequencer

Defines `FlowGraph` and `Step`. Steps execute in order. No step can be skipped. Each step can declare:

- **entry/exit conditions** — callables that must return `True` before the step is entered or exited
- **rollback handler** — called on cancel or kill to undo the step's effects
- **requires_approval** — pauses the session until a human provides an approval token
- **timeout_seconds** — cancels the step if it runs too long

```python
from core.state_machine import FlowGraph, Step

flow = FlowGraph([
    Step(id="step_a", name="Step A", handler=my_handler, requires_approval=True),
    Step(id="step_b", name="Step B", handler=next_handler),
])
```

### `core/authority.py` — Authority Boundary

Defines what a session is allowed to do. Set once, locked at session start, and immutable thereafter.

```python
from core.authority import AuthorityBoundary, ResourceLimit

boundary = AuthorityBoundary(
    resource_limits={
        "spend": ResourceLimit(name="spend", max_per_action=500.0, max_cumulative=1000.0)
    },
    allowed_scopes=["electronics", "clothing"],
    blocked_scopes=["gambling"],
    requires_approval_above=50.0,
)
```

Key behaviors:
- Raises `BoundaryViolation` if an action exceeds any limit
- Emits proximity alerts when cumulative spend crosses 80% of the cap
- `revoke()` immediately blocks all further actions (used by the kill switch)

### `core/governance.py` — Guard Pipeline

Guards are middleware that inspect every message flowing in or out of the agent. Each guard returns one of:

| Outcome | Effect |
|---|---|
| `PASS` | Message is clean, continue to next guard |
| `MODIFY` | Message was changed (e.g., PII redacted), continue with modified version |
| `BLOCK` | Message rejected — pipeline halts, error raised, escalation logged |

Guards run in `priority` order (lower number = runs first). Each guard declares a `direction`: `INBOUND`, `OUTBOUND`, or `BOTH`.

```python
from core.governance import GuardPipeline
from agents.commerce.guards import PromptInjectionGuard, PIIShield, MandateEnforcer

pipeline = GuardPipeline(
    [PromptInjectionGuard(), PIIShield(), MandateEnforcer(authority)],
    mandatory_guard_names={"PromptInjectionGuard", "PIIShield"},
)
```

### `core/audit.py` — Audit Logger

Every governance decision, step transition, approval, boundary check, and error is written as a frozen `AuditEvent` before the next operation proceeds. If the write fails, the session stops.

```python
from core.audit import AuditLogger, InMemoryAuditBackend, FileAuditBackend

# In-memory for tests
audit = AuditLogger(InMemoryAuditBackend())

# Append-only JSONL file for production
audit = AuditLogger(FileAuditBackend("audit.jsonl"))

# Query
events = audit.query(session_id="abc", event_type=EventType.ESCALATION)
```

### `core/protocol_adapter.py` — Adapter Layer

All external systems (payment processors, merchant APIs, signing services) implement `ProtocolAdapter`. The `AdapterRegistry` routes calls by protocol name and performs health checks before session start.

```python
from core.protocol_adapter import ProtocolAdapter, AdapterRegistry

class MyAdapter(ProtocolAdapter):
    name = "my_adapter"
    protocol = "my_protocol"

    async def execute(self, action): ...
    async def validate(self, action): ...
    async def rollback(self, action_id): ...
    async def health_check(self): ...
```

### `core/session.py` — Session Manager

`SessionManager` ties all five layers together for a single session lifetime:

```
start() → execute_step() → [approve()] → execute_step() → ... → COMPLETED
                                                               ↓
                                                            kill() → KILLED
```

Each `execute_step` call:
1. Runs the inbound message through the guard pipeline
2. Validates the action against the authority boundary
3. Executes the step handler
4. Consumes resources in the authority boundary
5. Runs the outbound result through the guard pipeline
6. Logs proximity alerts if spend is nearing the cap
7. Advances the state machine

---

## Commerce Agent

The commerce agent implements a five-step purchasing flow:

```
Product Discovery → Product Selection → Consumer Approval → Payment Execution → Audit Finalization
```

| Step | Protocol | Approval required |
|---|---|---|
| `product_discovery` | commerce (ACP browse) | No |
| `product_selection` | commerce (ACP checkout) | No |
| `consumer_approval` | internal | **Yes** — human gate |
| `payment_execution` | payment (Stripe + MAP) | No (pre-approved at step 3) |
| `audit_finalization` | audit | No |

### Adapters

| Adapter | File | Purpose |
|---|---|---|
| `ACPClient` | `adapters/acp_client.py` | Browse catalog, create checkout, process payment via Agentic Commerce Protocol |
| `StripeAdapter` | `adapters/stripe.py` | Create, confirm, and cancel Stripe PaymentIntents |
| `TAPSigner` | `adapters/tap_signer.py` | Sign and verify outbound requests using RFC 9421 (Ed25519 in production, HMAC in mock) |
| `MAPToken` | `adapters/map_token.py` | Issue, revoke, and validate Mastercard Agentic Tokens with governance metadata |

All adapters default to **mock mode** — the full flow runs without any real API credentials.

---

## Guards

Guards live in `agents/commerce/guards/` and are split across three files:

### `guards/injection.py`

| Guard | Direction | Priority | What it does |
|---|---|---|---|
| `PromptInjectionGuard` | INBOUND | 1 | Detects instruction overrides, role-play attacks, delimiter manipulation, and hidden-text injection. One match blocks. |
| `MerchantCatalogIntegrity` | INBOUND | 3 | Scans product descriptions for embedded adversarial instructions before they reach the agent context. |

### `guards/pii_shield.py`

| Guard | Direction | Priority | What it does |
|---|---|---|---|
| `PIIShield` | OUTBOUND | 10 | Detects and redacts credit card numbers (Luhn-validated), SSNs, email addresses, US phone numbers, and IP addresses before any response leaves the agent. Returns `MODIFY` with redacted content. |

### `guards/mandate.py`

| Guard | Direction | Priority | What it does |
|---|---|---|---|
| `MandateEnforcer` | BOTH | 5 | Validates message amount and scope against the `AuthorityBoundary`. Blocks if any limit would be exceeded. |
| `TAPSignatureGuard` | OUTBOUND | 15 | Requires a valid `x-signature` or `signature-input` header on all outbound merchant-bound requests. |
| `MAPTokenValidator` | OUTBOUND | 16 | Requires a MAP token with valid `governance_metadata` on all payment operations. |

### Guard execution order (by priority)

```
1  PromptInjectionGuard   (INBOUND)
3  MerchantCatalogIntegrity (INBOUND)
5  MandateEnforcer        (BOTH)
10 PIIShield              (OUTBOUND)
15 TAPSignatureGuard      (OUTBOUND)
16 MAPTokenValidator      (OUTBOUND)
```

---

## CLI Reference

```
trustx [COMMAND] [OPTIONS]
```

| Command | Description |
|---|---|
| `trustx interactive` | Launch a guided interactive commerce session in the terminal |
| `trustx agents` | List all available named agent personas |
| `trustx serve` | Start the MCP server for Claude Code / IDE integration |
| `trustx init --domain NAME` | Scaffold a new domain agent from template |
| `trustx configure --domain NAME` | Generate a session configuration JSON file |
| `trustx start --domain NAME` | Request a session start (MCP mode) |
| `trustx kill --session-id ID --operator OP` | Emergency stop a running session |
| `trustx audit --session-id ID` | Print the audit trail for a session |

### `trustx interactive`

```bash
trustx interactive                   # prompts for agent choice
trustx interactive --agent dina      # skip selection, use Dina
trustx interactive --agent susan     # skip selection, use Susan
```

### `trustx serve`

```bash
trustx serve                         # stdio transport, Dina, commerce domain
trustx serve --agent susan           # use Susan
trustx serve --transport sse --port 8080
```

### `trustx audit`

```bash
trustx audit --session-id my-session-001
trustx audit --session-id my-session-001 --format csv
trustx audit --session-id my-session-001 --file /path/to/audit.jsonl
```

---

## MCP Server

TrustX exposes eight tools over the Model Context Protocol, making it directly accessible from Claude Code, VS Code, and JetBrains:

| Tool | Description |
|---|---|
| `agent.start_session` | Initialize a new session for a given domain |
| `agent.execute_step` | Execute the current step with inputs |
| `agent.approve` | Provide a human approval token to resume a paused session |
| `agent.get_status` | Return current state and step history |
| `agent.cancel` | Cancel the session with rollback |
| `agent.kill` | Emergency stop — halt, rollback, revoke all tokens |
| `agent.list_domains` | List registered domain agents |
| `agent.get_audit_trail` | Retrieve filtered audit events for a session |

Start the server:

```bash
trustx serve --agent dina
```

Then connect from Claude Code via MCP configuration.

---

## Running Tests

```bash
# All tests
pytest

# Specific file
pytest tests/test_commerce_flow.py -v

# With coverage
pytest --cov=. --cov-report=term-missing
```

All 53 tests run in under 0.2 seconds (all mock mode, no external calls).

---

## Adding a New Domain Agent

1. **Scaffold** the new agent:
   ```bash
   trustx init --domain healthcare
   ```
   This creates `agents/healthcare/` with `flow.py`, `config.py`, `guards.py`, and `adapters/`.

2. **Define your flow** in `agents/healthcare/flow.py` — add `Step` objects with handlers and rollback logic.

3. **Set authority limits** in `agents/healthcare/config.py` — define resource limits, allowed scopes, and approval thresholds.

4. **Write domain guards** in `agents/healthcare/guards.py` — subclass `Guard` from `core.governance`.

5. **Register adapters** — implement `ProtocolAdapter` for each external system your domain connects to.

6. **Add a persona** — add an entry to a new `personas.py` file so the agent can be referenced by name from the CLI.

7. **Wire it up** in the session factory inside `cli/main.py` under `_serve_stdio`.

---

## Key Design Decisions

**Why is the authority boundary immutable after session start?**
An agent that can modify its own constraints is not a constrained agent. The boundary is locked before any adapter is called, so the agent cannot widen its own permissions mid-session.

**Why does audit write block the session on failure?**
If we can't record what happened, we don't know what happened. A silent audit failure could allow a bad action to go undetected. Stopping the session is the safe default.

**Why are mandatory guards enforced at pipeline construction?**
`PromptInjectionGuard` and `PIIShield` cannot be removed by domain agents. A configuration that omits them logs a warning but does not silently degrade — the pipeline still runs, and the missing guards are flagged.

**Why do agents have names (Dina, Susan)?**
Named personas give customers a consistent, understandable point of contact. Each name maps to a concrete set of spend limits and approval thresholds, so "ask Dina to buy this" has a well-defined, auditable meaning.
