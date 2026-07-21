"""Jira issue tools (sample implementation).

These illustrate the pattern for all Jira tools: typed arguments, a docstring
that the LLM sees as the tool description, structured return values, and Jira
errors surfaced as readable messages instead of raw tracebacks.
"""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from ..client import get_jira


def register(mcp: FastMCP) -> None:
    """Attach issue tools to the FastMCP instance."""

    @mcp.tool()
    def get_issue(issue_key: str, fields: str | None = None) -> dict[str, Any]:
        """Get a single Jira issue by key (e.g. ``PROJ-123``).

        Args:
            issue_key: The issue key, e.g. ``PROJ-123``.
            fields: Optional comma-separated list of fields to return
                (e.g. ``summary,status,assignee``). Defaults to all fields.
        """
        return get_jira().issue(issue_key, fields=fields)

    @mcp.tool()
    def search_issues(jql: str, limit: int = 50) -> dict[str, Any]:
        """Search issues with a JQL query.

        Args:
            jql: A JQL query, e.g. ``project = PROJ AND status = "In Progress"``.
            limit: Maximum number of issues to return (default 50).
        """
        return get_jira().jql(jql, limit=limit)

    @mcp.tool()
    def add_comment(issue_key: str, comment: str) -> dict[str, Any]:
        """Add a comment to a Jira issue.

        Args:
            issue_key: The issue key, e.g. ``PROJ-123``.
            comment: The comment body (plain text / Jira wiki markup).
        """
        return get_jira().issue_add_comment(issue_key, comment)
