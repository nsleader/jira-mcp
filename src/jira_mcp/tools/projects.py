"""Jira project tools."""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from ..client import get_jira


def register(mcp: FastMCP) -> None:
    """Attach project tools to the FastMCP instance."""

    @mcp.tool()
    def get_all_projects(
        include_archived: bool = False,
        expand: str | None = None,
    ) -> list[dict[str, Any]]:
        """List all Jira projects visible to the authenticated user.

        Args:
            include_archived: Include archived projects in the result.
            expand: Optional comma-separated list of fields to expand
                (e.g. ``description,lead,url``).
        """
        return get_jira().get_all_projects(
            included_archived=include_archived,
            expand=expand,
        )

    @mcp.tool()
    def get_project(project_key: str, expand: str | None = None) -> dict[str, Any]:
        """Get a single Jira project by its key or id.

        Args:
            project_key: The project key or id, e.g. ``PROJ`` or ``10001``.
            expand: Optional comma-separated list of fields to expand
                (e.g. ``description,lead,url``).
        """
        return get_jira().get_project(project_key, expand=expand)
