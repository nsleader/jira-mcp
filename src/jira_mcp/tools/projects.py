"""Jira project tools."""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs, urlparse

from mcp.server.fastmcp import FastMCP
from requests import HTTPError

from ..client import get_jira

_ACTOR_TYPES = {
    "atlassian-user-role-actor": "user",
    "atlassian-group-role-actor": "group",
}


def _role_ids(project_key: str) -> dict[str, int]:
    """Map role name → role id for a project (``project/{key}/role``).

    Jira returns ``{"Developers": ".../role/10001", ...}``, so the id has to be
    pulled off the tail of each URL.
    """
    jira = get_jira()
    try:
        resp = jira.get_project_roles(project_key)
    except HTTPError as exc:
        response = exc.response
        status = response.status_code if response is not None else "?"
        body = response.text.strip() if response is not None else str(exc)
        raise RuntimeError(
            f"Project role listing failed (HTTP {status}) at "
            f"'rest/api/2/project/{project_key}/role'. Server response: {body[:800]}"
        ) from exc

    if not isinstance(resp, dict):
        return {}

    roles: dict[str, int] = {}
    for name, url in resp.items():
        tail = str(url).rstrip("/").rsplit("/", 1)[-1]
        if tail.isdigit():
            roles[name] = int(tail)
    return roles


def _actor_key(actor: dict[str, Any]) -> str | None:
    """Best-effort stable user key (``JIRAUSER…``) for a role actor.

    Worklog and plan tools match on this key, not on the login, because a login
    changes (e.g. after a surname change). Server/DC role actors don't carry an
    ``actorUser`` block at all — the key only leaks through as the ``ownerId``
    query param of the avatar URL, so fall back to parsing that.
    """
    actor_user = actor.get("actorUser")
    if isinstance(actor_user, dict):
        key = actor_user.get("key") or actor_user.get("accountId")
        if key:
            return str(key)
    avatar = actor.get("avatarUrl")
    if isinstance(avatar, str):
        owner = parse_qs(urlparse(avatar).query).get("ownerId")
        if owner:
            return owner[0]
    return None


def _normalize_actor(actor: dict[str, Any]) -> dict[str, Any]:
    """Flatten a role actor to who it is and whether it's a user or a group."""
    return {
        "type": _ACTOR_TYPES.get(str(actor.get("type")), actor.get("type")),
        # ``name`` is the login for users and the group name for groups.
        "name": actor.get("name"),
        "display_name": actor.get("displayName"),
        "key": _actor_key(actor),
    }


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

    @mcp.tool()
    def get_project_roles(
        project_key: str,
        role: str | None = None,
        include_empty: bool = False,
        raw: bool = False,
    ) -> list[dict[str, Any]]:
        """List a project's roles and who is in them (users and groups).

        Use this to find the people actually running a project — the project's
        ``lead`` field is often a service account, while roles such as
        ``Tempo Project Managers``, ``Project budget manager`` or
        ``Administrators`` name the real owners.

        Costs one request to list the roles plus one per role, so narrow it with
        ``role`` when scanning many projects. A role whose members can't be read
        (403/404) is returned with an ``error`` field instead of failing the call.

        Args:
            project_key: The project key or id, e.g. ``PROJ`` or ``10001``.
            role: Only return roles whose name contains this substring
                (case-insensitive), e.g. ``manager``.
            include_empty: Also return roles that have no members.
            raw: Return Jira's untouched actor objects instead of the flattened
                ``type`` / ``name`` / ``display_name`` / ``key`` shape.
        """
        jira = get_jira()
        needle = role.strip().lower() if role else None

        result: list[dict[str, Any]] = []
        for name, role_id in _role_ids(project_key).items():
            if needle and needle not in name.lower():
                continue

            entry: dict[str, Any] = {"id": role_id, "name": name}
            try:
                actors = jira.get_project_actors_for_role_project(project_key, role_id)
            except HTTPError as exc:
                response = exc.response
                status = response.status_code if response is not None else "?"
                entry["actors"] = []
                entry["error"] = f"HTTP {status} reading role members"
                result.append(entry)
                continue

            actors = [a for a in (actors or []) if isinstance(a, dict)]
            if not actors and not include_empty:
                continue

            entry["actors"] = actors if raw else [_normalize_actor(a) for a in actors]
            result.append(entry)

        return result
