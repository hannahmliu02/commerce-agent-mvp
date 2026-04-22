"""TrustX Web API — FastAPI backend for the commerce agent UI.

Run with:
    trustx web
or directly:
    uvicorn app:app --reload --port 8000
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from agents.commerce import (
    ACPClient,
    MAPToken,
    MAPTokenValidator,
    MerchantCatalogIntegrity,
    StripeAdapter,
    TAPSigner,
    TAPSignatureGuard,
    default_commerce_boundary,
)
from agents.commerce.flow import build_commerce_flow
from agents.commerce.guards import MandateEnforcer, PIIShield, PromptInjectionGuard
from agents.commerce.personas import get_persona, list_personas
from core.audit import AuditLogger, InMemoryAuditBackend
from core.governance import GuardPipeline
from core.protocol_adapter import AdapterRegistry
from core.session import SessionManager

app = FastAPI(title="TrustX Agent API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount static files (index.html lives in static/)
import pathlib
_STATIC = pathlib.Path(__file__).parent / "static"
if _STATIC.exists():
    app.mount("/static", StaticFiles(directory=str(_STATIC)), name="static")

# In-memory session store  {session_id -> (SessionManager, InMemoryAuditBackend)}
# session_id -> (SessionManager, InMemoryAuditBackend, cart)
# cart: {product_id: {"product": {...}, "quantity": int}}
_sessions: dict[str, tuple[SessionManager, InMemoryAuditBackend, dict]] = {}

# ---------------------------------------------------------------------------
# Mock catalog (mirrors CLI)
# ---------------------------------------------------------------------------
MOCK_CATALOG = [
    {"id": "p001", "name": "Wireless Headphones", "price": 79.99, "category": "electronics"},
    {"id": "p002", "name": "Running Shoes",        "price": 120.00, "category": "clothing"},
    {"id": "p003", "name": "USB-C Hub",            "price": 49.99,  "category": "electronics"},
    {"id": "p004", "name": "Yoga Mat",             "price": 35.00,  "category": "sports"},
]

PRICE_MAP = {p["id"]: p["price"] for p in MOCK_CATALOG}


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------

class StartSessionRequest(BaseModel):
    agent: str = "dina"


class SearchRequest(BaseModel):
    query: str = ""


class CartItemRequest(BaseModel):
    product_id: str


class ApprovalRequest(BaseModel):
    confirmed: bool


class PaymentRequest(BaseModel):
    amount: float


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/")
async def index():
    html = _STATIC / "index.html"
    if html.exists():
        return FileResponse(str(html))
    return {"message": "TrustX API running. UI not found — check static/index.html"}


@app.get("/agents")
async def list_agents():
    return [
        {
            "key": p.name.lower(),
            "name": p.name,
            "tagline": p.tagline,
            "description": p.description,
            "emoji": p.emoji,
            "color": p.color,
            "max_per_action": p.max_per_action,
            "max_cumulative": p.max_cumulative,
            "requires_approval_above": p.requires_approval_above,
            "custom": p.custom,
        }
        for p in list_personas()
    ]


@app.post("/sessions")
async def create_session(body: StartSessionRequest):
    try:
        persona = get_persona(body.agent)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))

    authority = default_commerce_boundary(
        max_per_action=persona.max_per_action,
        max_cumulative=persona.max_cumulative,
        requires_approval_above=persona.requires_approval_above,
    )
    registry = AdapterRegistry()
    registry.register(ACPClient(mock=True))
    registry.register(StripeAdapter(mock=True))
    registry.register(TAPSigner(mock=True))
    registry.register(MAPToken(mock=True))

    audit_backend = InMemoryAuditBackend()
    pipeline = GuardPipeline(
        [
            PromptInjectionGuard(),
            PIIShield(),
            MandateEnforcer(authority),
            TAPSignatureGuard(),
            MAPTokenValidator(),
            MerchantCatalogIntegrity(),
        ],
        mandatory_guard_names={"PromptInjectionGuard", "PIIShield"},
    )
    session_id = f"web-{body.agent}-{uuid.uuid4().hex[:8]}"
    mgr = SessionManager(
        session_id=session_id,
        domain="commerce",
        flow=build_commerce_flow(),
        adapters=registry,
        guard_pipeline=pipeline,
        authority=authority,
        audit=AuditLogger(audit_backend),
    )
    result = await mgr.start()
    _sessions[session_id] = (mgr, audit_backend, {})
    return {
        "session_id": session_id,
        "agent": persona.name,
        "emoji": persona.emoji,
        "color": persona.color,
        "first_step": result["first_step"],
        "limits": {
            "max_per_action": persona.max_per_action,
            "max_cumulative": persona.max_cumulative,
            "requires_approval_above": persona.requires_approval_above,
        },
    }


def _get_session(session_id: str) -> tuple[SessionManager, InMemoryAuditBackend, Any]:
    if session_id not in _sessions:
        raise HTTPException(status_code=404, detail="Session not found")
    return _sessions[session_id]


@app.get("/sessions/{session_id}")
async def get_session_status(session_id: str):
    mgr, _, _product = _get_session(session_id)
    return {
        "session_id": session_id,
        "status": mgr.status.value,
    }


@app.post("/sessions/{session_id}/search")
async def search_products(session_id: str, body: SearchRequest):
    _get_session(session_id)  # validate session exists
    query = body.query.strip().lower()
    matched = [p for p in MOCK_CATALOG if not query or query in p["name"].lower()]
    return {"products": matched if matched else MOCK_CATALOG, "query": body.query}


@app.post("/sessions/{session_id}/steps/product_discovery")
async def step_product_discovery(session_id: str, body: SearchRequest):
    mgr, _, _product = _get_session(session_id)
    try:
        result = await mgr.execute_step("product_discovery", {"query": body.query})
        return {"ok": True, "next_step": result.get("next_step")}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


# ---------------------------------------------------------------------------
# Cart management
# ---------------------------------------------------------------------------

def _cart_total(cart: dict) -> float:
    return sum(v["product"]["price"] * v["quantity"] for v in cart.values())


@app.get("/sessions/{session_id}/cart")
async def get_cart(session_id: str):
    _, _, cart = _get_session(session_id)
    items = [{"product": v["product"], "quantity": v["quantity"]} for v in cart.values()]
    return {"items": items, "total": _cart_total(cart), "count": sum(v["quantity"] for v in cart.values())}


@app.post("/sessions/{session_id}/cart/items")
async def add_to_cart(session_id: str, body: CartItemRequest):
    mgr, audit_backend, cart = _get_session(session_id)
    product = next((p for p in MOCK_CATALOG if p["id"] == body.product_id), None)
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")
    if body.product_id in cart:
        cart[body.product_id]["quantity"] += 1
    else:
        cart[body.product_id] = {"product": product, "quantity": 1}
    _sessions[session_id] = (mgr, audit_backend, cart)
    return {"items": [{"product": v["product"], "quantity": v["quantity"]} for v in cart.values()],
            "total": _cart_total(cart), "count": sum(v["quantity"] for v in cart.values())}


@app.delete("/sessions/{session_id}/cart/items/{product_id}")
async def remove_from_cart(session_id: str, product_id: str):
    mgr, audit_backend, cart = _get_session(session_id)
    if product_id not in cart:
        raise HTTPException(status_code=404, detail="Item not in cart")
    if cart[product_id]["quantity"] > 1:
        cart[product_id]["quantity"] -= 1
    else:
        del cart[product_id]
    _sessions[session_id] = (mgr, audit_backend, cart)
    return {"items": [{"product": v["product"], "quantity": v["quantity"]} for v in cart.values()],
            "total": _cart_total(cart), "count": sum(v["quantity"] for v in cart.values())}


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------

@app.post("/sessions/{session_id}/steps/product_selection")
async def step_product_selection(session_id: str):
    """Advance the state machine past product_selection using the current cart."""
    mgr, audit_backend, cart = _get_session(session_id)
    if not cart:
        raise HTTPException(status_code=400, detail="Cart is empty")
    # Pass the first product id; the mandate enforcer checks the total at approval
    first_id = next(iter(cart))
    try:
        result = await mgr.execute_step("product_selection", {"product_id": first_id})
        return {"ok": True, "next_step": result.get("next_step"), "cart_total": _cart_total(cart)}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/sessions/{session_id}/steps/consumer_approval")
async def step_consumer_approval(session_id: str, body: ApprovalRequest):
    mgr, audit_backend, cart = _get_session(session_id)
    total = _cart_total(cart) if cart else 79.99
    try:
        result = await mgr.execute_step(
            "consumer_approval",
            {"total": total, "order_summary": {"confirmed": body.confirmed}},
        )
        pending = result.get("pending_approval", False)
        if pending:
            if not body.confirmed:
                await mgr.cancel("Consumer declined")
                return {"ok": False, "cancelled": True}
            approval = await mgr.approve("consumer_approval", f"web_approval_{uuid.uuid4().hex[:8]}")
            return {"ok": True, "next_step": approval.get("next_step"), "approved": True, "total": total}
        return {"ok": True, "next_step": result.get("next_step"), "auto_approved": True, "total": total}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/sessions/{session_id}/steps/payment_execution")
async def step_payment_execution(session_id: str):
    """Execute payment for the full cart total."""
    mgr, _, cart = _get_session(session_id)
    total = _cart_total(cart)
    try:
        result = await mgr.execute_step("payment_execution", {"amount": total})
        return {"ok": True, "next_step": result.get("next_step"), "amount": total}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/sessions/{session_id}/steps/audit_finalization")
async def step_audit_finalization(session_id: str):
    mgr, audit_backend, _ = _get_session(session_id)
    try:
        result = await mgr.execute_step("audit_finalization", {})
        events = audit_backend.query(session_id=session_id)
        return {
            "ok": True,
            "status": result["result"].get("status", "completed"),
            "audit_events": [
                {
                    "event_type": e.event_type.value,
                    "step_id": e.step_id,
                    "actor": e.actor,
                    "disposition": e.disposition.value,
                    "timestamp": e.timestamp.isoformat(),
                }
                for e in events
            ],
        }
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/sessions/{session_id}/kill")
async def kill_session(session_id: str):
    mgr, _, _product = _get_session(session_id)
    result = await mgr.kill("web_operator")
    return {"ok": True, "status": result["status"].value}


@app.get("/sessions/{session_id}/audit")
async def get_audit_trail(session_id: str):
    _, audit_backend, _product = _get_session(session_id)
    events = audit_backend.query(session_id=session_id)
    return {
        "session_id": session_id,
        "events": [
            {
                "event_type": e.event_type.value,
                "step_id": e.step_id,
                "actor": e.actor,
                "disposition": e.disposition.value,
                "timestamp": e.timestamp.isoformat(),
                "metadata": e.metadata,
            }
            for e in events
        ],
    }
