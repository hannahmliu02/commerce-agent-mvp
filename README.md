# TrustX Agent Framework

A domain-agnostic Python framework for building **governed, auditable AI agents**. TrustX enforces authority boundaries, runs every message through a composable governance pipeline, and records an immutable audit trail — before any action executes.

---

## Table of Contents

1. [What is TrustX?](#what-is-trustx)
2. [Quick Start](#quick-start)
3. [Project Structure](#project-structure)
4. [Core Concepts](#core-concepts)
5. [Named Agents](#named-agents)
6. [Custom Agents](#custom-agents)
7. [Interactive CLI Demo](#interactive-cli-demo)
8. [Web UI](#web-ui)
9. [Commerce Agent](#commerce-agent)
10. [Guards](#guards)
11. [MCP Server](#mcp-server)
12. [CLI Reference](#cli-reference)
13. [Running Tests](#running-tests)
14. [Adding a New Domain](#adding-a-new-domain)
15. [Key Design Decisions](#key-design-decisions)

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

# Run an interactive guided session in the terminal
trustx interactive

# Launch the web UI
trustx web
# → opens at http://localhost:8000

# Start the MCP server (for Claude Code / VS Code integration)
trustx serve --agent dina
```

---

## Project Structure

```
agent-blueprint/
│
├── core/                          # Domain-agnostic engine
│   ├── types.py                   # Shared enums and Pydantic models
│   ├── state_machine.py           # Ordered step execution, rollback, approval gates
│   ├── authority.py               # Immutable per-session resource limits and scope rules
│   ├── governance.py              # Guard pipeline (PASS / MODIFY / BLOCK)
│   ├── audit.py                   # Append-only immutable event log
│   ├── protocol_adapter.py        # Abstract adapter interface + registry
│   ├── session.py                 # SessionManager — ties everything together
│   └── mcp_server.py              # MCP server exposing 8 agent tools
│
├── agents/
│   └── commerce/                  # Commerce domain implementation
│       ├── flow.py                # Five-step commerce flow graph
│       ├── config.py              # Authority boundary factory
│       ├── personas.py            # Named agent personas (Dina, Susan + custom)
│       ├── adapters/
│       │   ├── acp_client.py      # Agentic Commerce Protocol (browse, checkout, pay)
│       │   ├── stripe.py          # Stripe PaymentIntent adapter
│       │   ├── tap_signer.py      # Visa TAP — RFC 9421 request signing
│       │   └── map_token.py       # Mastercard MAP — scoped Agentic Token lifecycle
│       └── guards/
│           ├── injection.py       # PromptInjectionGuard + MerchantCatalogIntegrity
│           ├── pii_shield.py      # PIIShield — redacts PII from outbound responses
│           └── mandate.py         # MandateEnforcer + TAPSignatureGuard + MAPTokenValidator
│
├── cli/
│   └── main.py                    # All CLI commands
│
├── static/
│   └── index.html                 # Web UI (Tailwind CSS, served by FastAPI)
│
├── app.py                         # FastAPI REST backend for the web UI
├── pyproject.toml                 # Package metadata and dependencies
└── tests/
    ├── test_state_machine.py
    ├── test_authority.py
    ├── test_governance.py
    ├── test_audit.py
    └── test_commerce_flow.py
```

---

## Core Concepts

### State Machine (`core/state_machine.py`)

Steps execute in a fixed order. No step can be skipped. Each step can declare:

- **entry/exit conditions** — callables that must return `True` before the step is entered or exited
- **rollback handler** — called on cancel or kill to undo the step's side effects
- **requires_approval** — pauses the session until a human provides an approval token
- **timeout_seconds** — cancels the step if it runs too long

```python
from core.state_machine import FlowGraph, Step

flow = FlowGraph([
    Step(id="step_a", name="Step A", handler=my_handler, requires_approval=True),
    Step(id="step_b", name="Step B", handler=next_handler),
])
```

### Authority Boundary (`core/authority.py`)

Defines what a session is allowed to do. Set once at session start and **immutable** thereafter — the agent cannot widen its own permissions mid-session.

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

### Governance Pipeline (`core/governance.py`)

Guards are middleware that inspect every message flowing in or out of the agent. Each guard returns one of:

| Outcome | Effect |
|---|---|
| `PASS` | Message is clean, continue to next guard |
| `MODIFY` | Message was changed (e.g. PII redacted), continue with modified version |
| `BLOCK` | Message rejected — pipeline halts, error raised, escalation logged |

Guards run in `priority` order (lower = runs first). Each guard declares a `direction`: `INBOUND`, `OUTBOUND`, or `BOTH`.

```python
from core.governance import GuardPipeline
from agents.commerce.guards import PromptInjectionGuard, PIIShield, MandateEnforcer

pipeline = GuardPipeline(
    [PromptInjectionGuard(), PIIShield(), MandateEnforcer(authority)],
    mandatory_guard_names={"PromptInjectionGuard", "PIIShield"},
)
```

### Audit Logger (`core/audit.py`)

Every governance decision, step transition, approval, boundary check, and error is written as a frozen `AuditEvent` before the next operation proceeds. If the write fails, the session stops.

```python
from core.audit import AuditLogger, InMemoryAuditBackend, FileAuditBackend

# In-memory (tests)
audit = AuditLogger(InMemoryAuditBackend())

# Append-only JSONL file (production)
audit = AuditLogger(FileAuditBackend("audit.jsonl"))

# Query
events = audit.query(session_id="abc", event_type=EventType.ESCALATION)
```

### Protocol Adapter (`core/protocol_adapter.py`)

All external systems implement `ProtocolAdapter`. The `AdapterRegistry` routes calls by protocol name and performs health checks before session start. All adapters default to **mock mode** — the full flow runs without any real API credentials.

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

### Session Manager (`core/session.py`)

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

## Named Agents

TrustX ships with two built-in commerce agent personas. Each has its own spend profile and approval threshold.

| Agent | Tagline | Per-action limit | Session cap | Approval above |
|---|---|---|---|---|
| 🛍️ **Dina** | Your everyday smart shopper | $500 | $1,000 | $50 |
| 💼 **Susan** | Your premium high-value purchasing agent | $2,000 | $5,000 | $200 |

**Dina** is the default — optimized for routine everyday purchases with conservative limits and frequent approval prompts.

**Susan** handles high-value and bulk purchases with the same governance rules at a higher spend threshold, suitable for B2B or premium consumer flows.

```bash
trustx agents                        # see all agents
trustx interactive --agent dina
trustx interactive --agent susan
trustx serve --agent susan           # MCP server using Susan
trustx web                           # web UI — pick any agent from the browser
```

---

## Custom Agents

Anyone can create a named agent with their own spend limits:

```bash
# Create a custom agent
trustx create-agent \
  --name Alex \
  --tagline "Budget-conscious everyday shopper" \
  --emoji 🎯 \
  --spend-limit 200 \
  --session-cap 400 \
  --approval-above 25

# Use the new agent
trustx interactive --agent alex

# List all agents (built-in + custom)
trustx agents

# Delete a custom agent
trustx delete-agent --name alex
```

Custom personas are saved to `~/.trustx/personas.json` and persist across sessions. Built-in agents (Dina, Susan) cannot be overwritten or deleted.

---

## Interactive CLI Demo

The `interactive` command walks you through a full five-step commerce session in your terminal — guards running, authority checks, approval gates, and the final audit trail, all in real time.

```bash
trustx interactive              # prompts for agent choice
trustx interactive --agent dina
trustx interactive --agent susan
```

You'll be guided through:
1. **Product Discovery** — search the catalog (prompt injection blocked here)
2. **Product Selection** — pick an item by ID
3. **Consumer Approval** — review order summary; approve or decline
4. **Payment Execution** — Stripe + MAP token + TAP signature all run
5. **Audit Finalization** — full event log printed in the terminal

---

## Web UI

TrustX includes a browser-based UI — a five-step purchase wizard in a popup modal, built with Tailwind CSS and a FastAPI REST backend.

```bash
trustx web                         # http://localhost:8000
trustx web --port 3000 --reload    # dev mode with auto-reload
```

The page loads all available agents as clickable cards. Clicking one opens a modal that walks through the same five steps as the CLI — search, select, approve, pay, done. The payment step shows an animated governance checklist (all six guards running in sequence). The completion screen displays the payment-level governance checks as a clean receipt confirming every check passed.

**API docs** auto-generate at `http://localhost:8000/docs`.

The frontend (`static/index.html`) uses zero build tooling — plain HTML, vanilla JS, and Tailwind via CDN — making it straightforward to hand off to a React generator like [Lovable](https://lovable.dev).

### REST API

| Method | Path | Description |
|---|---|---|
| `GET` | `/agents` | List all personas |
| `POST` | `/sessions` | Create a governed session |
| `POST` | `/sessions/{id}/search` | Search the catalog |
| `POST` | `/sessions/{id}/steps/product_discovery` | Step 1 |
| `POST` | `/sessions/{id}/steps/product_selection` | Step 2 |
| `POST` | `/sessions/{id}/steps/consumer_approval` | Step 3 + approval gate |
| `POST` | `/sessions/{id}/steps/payment_execution` | Step 4 |
| `POST` | `/sessions/{id}/steps/audit_finalization` | Step 5, returns audit trail |
| `GET` | `/sessions/{id}/audit` | Fetch raw audit events |
| `POST` | `/sessions/{id}/kill` | Emergency stop |

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
| `ACPClient` | `agents/commerce/adapters/acp_client.py` | Browse catalog, create checkout, process payment via Agentic Commerce Protocol |
| `StripeAdapter` | `agents/commerce/adapters/stripe.py` | Create, confirm, and cancel Stripe PaymentIntents |
| `TAPSigner` | `agents/commerce/adapters/tap_signer.py` | Sign and verify outbound requests using RFC 9421 (Ed25519 in production, HMAC in mock) |
| `MAPToken` | `agents/commerce/adapters/map_token.py` | Issue, revoke, and validate Mastercard Agentic Tokens with governance metadata |

---

## Guards

Guards live in `agents/commerce/guards/` and are split across three files.

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
 1  PromptInjectionGuard      (INBOUND)
 3  MerchantCatalogIntegrity  (INBOUND)
 5  MandateEnforcer           (BOTH)
10  PIIShield                 (OUTBOUND)
15  TAPSignatureGuard         (OUTBOUND)
16  MAPTokenValidator         (OUTBOUND)
```

---

## MCP Server

TrustX exposes eight tools over the Model Context Protocol, making it directly usable from Claude Code, VS Code, and JetBrains.

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

```bash
trustx serve --agent dina         # stdio transport (default)
trustx serve --agent susan --transport sse --port 8080
```

---

## CLI Reference

```
trustx [COMMAND] [OPTIONS]
```

| Command | Description |
|---|---|
| `trustx interactive` | Guided interactive commerce session in the terminal |
| `trustx web` | Launch the browser-based UI and REST API |
| `trustx agents` | List all available named agent personas |
| `trustx create-agent` | Create a new custom named agent |
| `trustx delete-agent` | Delete a custom agent |
| `trustx serve` | Start the MCP server |
| `trustx init --domain NAME` | Scaffold a new domain agent from template |
| `trustx configure --domain NAME` | Generate a session configuration JSON file |
| `trustx start --domain NAME` | Request a session start (MCP mode) |
| `trustx kill --session-id ID --operator OP` | Emergency stop a running session |
| `trustx audit --session-id ID` | Print the audit trail for a session |

### `trustx interactive`

```bash
trustx interactive                   # prompts for agent choice
trustx interactive --agent dina
trustx interactive --agent susan
trustx interactive --agent alex      # any custom agent
```

### `trustx web`

```bash
trustx web                           # http://localhost:8000
trustx web --port 3000
trustx web --host 0.0.0.0 --reload   # expose to network, dev mode
```

### `trustx create-agent`

```bash
trustx create-agent \
  --name Jordan \
  --tagline "Mid-range general shopper" \
  --emoji 🛒 \
  --spend-limit 750 \
  --session-cap 1500 \
  --approval-above 100 \
  --color blue
```

### `trustx audit`

```bash
trustx audit --session-id my-session-001
trustx audit --session-id my-session-001 --format csv
trustx audit --session-id my-session-001 --file /path/to/audit.jsonl
```

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

All 53 tests run in under 0.2 seconds — all mock mode, no external API calls required.

---

## Adding a New Domain

1. **Scaffold** the new agent:
   ```bash
   trustx init --domain healthcare
   ```
   Creates `agents/healthcare/` with `flow.py`, `config.py`, `guards.py`, and `adapters/`.

2. **Define your flow** in `agents/healthcare/flow.py` — add `Step` objects with handlers and rollback logic.

3. **Set authority limits** in `agents/healthcare/config.py` — define resource limits, allowed scopes, and approval thresholds.

4. **Write domain guards** in `agents/healthcare/guards.py` — subclass `Guard` from `core.governance`.

5. **Register adapters** — implement `ProtocolAdapter` for each external system your domain connects to.

6. **Add personas** — create `agents/healthcare/personas.py` so agents can be referenced by name from the CLI.

7. **Wire it up** — add the domain to the session factory in `cli/main.py` under `_serve_stdio`.

---

## Key Design Decisions

**Why is the authority boundary immutable after session start?**
An agent that can modify its own constraints is not a constrained agent. The boundary is locked before any adapter is called, so the agent cannot widen its own permissions mid-session.

**Why does audit write block the session on failure?**
If we can't record what happened, we don't know what happened. A silent audit failure could allow a bad action to go undetected. Stopping the session is the safe default.

**Why are mandatory guards enforced at pipeline construction?**
`PromptInjectionGuard` and `PIIShield` cannot be removed by domain agents. A configuration that omits them still runs, but the missing guards are flagged — there is no silent degradation.

**Why do agents have names (Dina, Susan)?**
Named personas give users a consistent, understandable point of contact. Each name maps to a concrete set of spend limits and approval thresholds, so "ask Dina to buy this" has a well-defined, auditable meaning.

**Why are custom personas persisted to `~/.trustx/personas.json` and not the project directory?**
Custom agents belong to the user, not the codebase. Storing them in the home directory means they survive repo clones, branch switches, and upgrades without ever being accidentally committed or overwritten.
