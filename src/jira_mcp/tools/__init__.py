"""Tool modules for the Jira MCP server.

Each module exposes a ``register(mcp)`` function that attaches its tools to the
FastMCP instance. ``register_all`` wires them all up from ``server.py``.
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from . import issues, projects, teams, tempo, users, worklogs


def register_all(mcp: FastMCP) -> None:
    """Register every tool module on the given FastMCP instance."""
    issues.register(mcp)
    projects.register(mcp)
    users.register(mcp)
    worklogs.register(mcp)
    teams.register(mcp)
    tempo.register(mcp)
