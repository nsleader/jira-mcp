"""Jira user tools.

Jira Server / Data Center has no single "list every user" endpoint (that exists
only in Cloud). A user "exists" in Jira by having application access, which is
granted through groups. There is usually no single group holding everyone, so to
enumerate all users we page through every group that grants Jira Software access
and de-duplicate the members.
"""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from ..client import get_jira

# Safety cap so a huge instance can't produce an unbounded response.
_MAX_USERS = 5000
_PAGE_SIZE = 50

# Groups that grant Jira Software access on this instance. Their union is the set
# of all Jira users. Override via the ``groups`` argument if this ever changes.
_DEFAULT_LICENSE_GROUPS = (
    "jira-users",
    "65apps-employees",
    "65apps-managers",
    "administrators",
    "system-administrators",
    "site-admins",
    "confluence-administrators",
)


def _user_key(user: dict[str, Any]) -> str:
    """Stable identity for de-duplicating a user across groups."""
    return user.get("accountId") or user.get("key") or user.get("name") or ""


def register(mcp: FastMCP) -> None:
    """Attach user tools to the FastMCP instance."""

    @mcp.tool()
    def get_all_users(
        groups: list[str] | None = None,
        status: str = "active",
    ) -> list[dict[str, Any]]:
        """List all Jira users (union of the groups that grant Jira access).

        Jira Server/DC has no single "all users" endpoint — users are enumerated
        through group membership. By default this reads every group that grants
        Jira Software access on this instance and de-duplicates the members.
        Pass ``groups`` to target specific group(s) instead.

        Args:
            groups: Group names to read. Defaults to the instance's Jira Software
                license groups.
            status: Which users to return by account state — ``active`` (default,
                enabled users only), ``inactive`` (deactivated / blocked / former
                employees, ``active=false``), or ``all``.
        """
        if status not in ("active", "inactive", "all"):
            raise ValueError("status must be one of: active, inactive, all")

        jira = get_jira()
        target_groups = groups or list(_DEFAULT_LICENSE_GROUPS)
        # Need inactive accounts from the API for both "inactive" and "all".
        fetch_inactive = status != "active"
        seen: dict[str, dict[str, Any]] = {}

        for group in target_groups:
            start = 0
            while start < _MAX_USERS:
                page = jira.get_all_users_from_group(
                    group,
                    include_inactive_users=fetch_inactive,
                    start=start,
                    limit=_PAGE_SIZE,
                )
                values = page.get("values", [])
                for user in values:
                    seen.setdefault(_user_key(user), user)
                if page.get("isLast") or len(values) < _PAGE_SIZE:
                    break
                start += _PAGE_SIZE

        users = list(seen.values())
        if status == "active":
            return [u for u in users if u.get("active", True)]
        if status == "inactive":
            return [u for u in users if not u.get("active", True)]
        return users

    @mcp.tool()
    def find_groups(query: str = "users", limit: int = 20) -> dict[str, Any]:
        """Find Jira groups by name — use it to discover the license group name.

        Handy when you don't know which group holds all users. Search e.g.
        ``users`` to find candidates like ``jira-users`` / ``jira-software-users``,
        then pass the right one to ``get_all_users``.

        Args:
            query: Substring to match against group names (default ``users``).
            limit: Maximum number of groups to return (default 20).
        """
        return get_jira().get_groups(query=query, limit=limit)

    @mcp.tool()
    def search_users(
        query: str,
        limit: int = 50,
        include_inactive: bool = False,
    ) -> list[dict[str, Any]]:
        """Search Jira users by name, username or email fragment.

        Args:
            query: Substring to match against username, display name or email.
            limit: Maximum number of users to return (default 50).
            include_inactive: Include deactivated users in the result.
        """
        # Jira Server/DC expects ``username``; ``query`` is Cloud-only.
        return get_jira().user_find_by_user_string(
            username=query,
            start=0,
            limit=limit,
            include_inactive_users=include_inactive,
        )
