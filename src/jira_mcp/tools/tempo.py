"""Tempo Planning API tools (placeholder).

Extension point for the Tempo Planning REST API. Tempo has its own base URL and
API token (separate from Jira's PAT), so this module will grow its own thin HTTP
client and settings when the integration is implemented.

For now ``register`` is a no-op — no tools are exposed.
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP


def register(mcp: FastMCP) -> None:
    """Register Tempo Planning tools. No tools yet — placeholder."""
    # TODO: implement Tempo Planning integration.
    #   - Add TEMPO_API_TOKEN / TEMPO_BASE_URL to config.Settings.
    #   - Build a small httpx-based client for the Tempo Planning REST API.
    #   - Register tools here, e.g. list_plans, create_allocation, get_workload.
    return None
