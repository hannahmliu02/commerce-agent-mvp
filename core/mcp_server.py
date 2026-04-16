"""MCP Server Interface — exposes the Mother Agent as an MCP server.

Developers connect from Claude Code, VS Code, JetBrains, or any MCP client.
Each session is isolated; no shared mutable state between sessions.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from .session import SessionManager
from .types import EventType

logger = logging.getLogger(__name__)


class AgentMCPServer:
    """Wraps a session factory and exposes all agent tools over MCP."""

    def __init__(self, session_factory: "SessionFactory") -> None:
        self._factory = session_factory
        self._sessions: dict[str, SessionManager] = {}
        self._server = Server("trustx-agent")
        self._register_tools()

    # ------------------------------------------------------------------
    # Tool registration
    # ------------------------------------------------------------------

    def _register_tools(self) -> None:
        server = self._server

        @server.list_tools()
        async def list_tools() -> list[Tool]:
            return [
                Tool(
                    name="agent.start_session",
                    description="Initialize a new agent session with the specified domain flow and authority constraints.",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "domain": {"type": "string", "description": "Domain identifier (e.g., 'commerce')"},
                            "config": {"type": "object", "description": "Session configuration overrides"},
                            "authority_boundary": {"type": "object", "description": "Authority boundary overrides"},
                        },
                        "required": ["domain"],
                    },
                ),
                Tool(
                    name="agent.execute_step",
                    description="Execute the current step. Returns the result and next available step.",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "session_id": {"type": "string"},
                            "step_id": {"type": "string"},
                            "inputs": {"type": "object"},
                        },
                        "required": ["session_id", "step_id"],
                    },
                ),
                Tool(
                    name="agent.approve",
                    description="Provide human approval for a paused step (e.g., payment confirmation).",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "session_id": {"type": "string"},
                            "step_id": {"type": "string"},
                            "approval_token": {"type": "string"},
                        },
                        "required": ["session_id", "step_id", "approval_token"],
                    },
                ),
                Tool(
                    name="agent.get_status",
                    description="Return the current state of a session including full step history.",
                    inputSchema={
                        "type": "object",
                        "properties": {"session_id": {"type": "string"}},
                        "required": ["session_id"],
                    },
                ),
                Tool(
                    name="agent.cancel",
                    description="Cancel an active session. Triggers rollback of the current step.",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "session_id": {"type": "string"},
                            "reason": {"type": "string"},
                        },
                        "required": ["session_id", "reason"],
                    },
                ),
                Tool(
                    name="agent.kill",
                    description="Emergency stop. Halts all activity, rolls back current step, revokes tokens.",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "session_id": {"type": "string"},
                            "operator_id": {"type": "string"},
                        },
                        "required": ["session_id", "operator_id"],
                    },
                ),
                Tool(
                    name="agent.list_domains",
                    description="List all registered domain agents and their flow descriptions.",
                    inputSchema={"type": "object", "properties": {}},
                ),
                Tool(
                    name="agent.get_audit_trail",
                    description="Retrieve the audit trail for a session, optionally filtered by event type.",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "session_id": {"type": "string"},
                            "event_type": {"type": "string"},
                            "step_id": {"type": "string"},
                        },
                        "required": ["session_id"],
                    },
                ),
            ]

        @server.call_tool()
        async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
            try:
                result = await self._dispatch(name, arguments)
                import json
                return [TextContent(type="text", text=json.dumps(result, default=str))]
            except Exception as exc:
                logger.exception("Tool '%s' failed: %s", name, exc)
                import json
                return [TextContent(type="text", text=json.dumps({"error": str(exc)}))]

    # ------------------------------------------------------------------
    # Dispatcher
    # ------------------------------------------------------------------

    async def _dispatch(self, name: str, args: dict[str, Any]) -> Any:
        if name == "agent.start_session":
            return await self._start_session(args)
        if name == "agent.execute_step":
            return await self._execute_step(args)
        if name == "agent.approve":
            return await self._approve(args)
        if name == "agent.get_status":
            return self._get_status(args)
        if name == "agent.cancel":
            return await self._cancel(args)
        if name == "agent.kill":
            return await self._kill(args)
        if name == "agent.list_domains":
            return self._list_domains()
        if name == "agent.get_audit_trail":
            return self._get_audit_trail(args)
        raise ValueError(f"Unknown tool: {name}")

    # ------------------------------------------------------------------
    # Tool implementations
    # ------------------------------------------------------------------

    async def _start_session(self, args: dict) -> dict:
        domain = args["domain"]
        config = args.get("config", {})
        authority_override = args.get("authority_boundary", {})
        session = await self._factory.create(domain, config, authority_override)
        self._sessions[session.session_id] = session
        return await session.start()

    async def _execute_step(self, args: dict) -> dict:
        session = self._get_session(args["session_id"])
        return await session.execute_step(args["step_id"], args.get("inputs", {}))

    async def _approve(self, args: dict) -> dict:
        session = self._get_session(args["session_id"])
        return await session.approve(args["step_id"], args["approval_token"])

    def _get_status(self, args: dict) -> dict:
        return self._get_session(args["session_id"]).get_status()

    async def _cancel(self, args: dict) -> dict:
        session = self._get_session(args["session_id"])
        result = await session.cancel(args.get("reason", "cancelled by consumer"))
        del self._sessions[args["session_id"]]
        return result

    async def _kill(self, args: dict) -> dict:
        session = self._get_session(args["session_id"])
        result = await session.kill(args["operator_id"])
        del self._sessions[args["session_id"]]
        return result

    def _list_domains(self) -> dict:
        return {"domains": self._factory.list_domains()}

    def _get_audit_trail(self, args: dict) -> dict:
        session = self._get_session(args["session_id"])
        event_type = args.get("event_type")
        et = EventType(event_type) if event_type else None
        events = session.get_audit_trail(event_type=et, step_id=args.get("step_id"))
        return {"session_id": args["session_id"], "events": events}

    def _get_session(self, session_id: str) -> SessionManager:
        if session_id not in self._sessions:
            raise SessionNotFoundError(f"Session '{session_id}' not found")
        return self._sessions[session_id]

    # ------------------------------------------------------------------
    # Server start
    # ------------------------------------------------------------------

    async def run_stdio(self) -> None:
        """Start MCP server over stdio (local development)."""
        async with stdio_server() as (read_stream, write_stream):
            await self._server.run(
                read_stream,
                write_stream,
                self._server.create_initialization_options(),
            )


# ---------------------------------------------------------------------------
# Session factory interface
# ---------------------------------------------------------------------------


class SessionFactory:
    """Override create() to wire up domain-specific session managers."""

    async def create(
        self, domain: str, config: dict, authority_override: dict
    ) -> SessionManager:
        raise NotImplementedError

    def list_domains(self) -> list[dict]:
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class SessionNotFoundError(Exception):
    pass
