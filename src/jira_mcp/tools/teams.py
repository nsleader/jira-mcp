"""Tempo team tools.

Discovery helpers for Tempo teams — needed to turn a team *name* into the
numeric ``teamId`` that ``get_team_worklog`` (and Tempo's "Logged Time" report)
expects. The Tempo teams API has no server-side name search, so ``find_team``
lists every team once and filters by substring locally.
"""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP
from requests import HTTPError

from ..client import get_jira


def _get_all_teams(expand: str | None = None) -> list[dict[str, Any]]:
    """Fetch every Tempo team (``rest/tempo-teams/2/team``)."""
    jira = get_jira()
    try:
        resp = jira.tempo_teams_get_all_teams(expand=expand)
    except HTTPError as exc:
        response = exc.response
        status = response.status_code if response is not None else "?"
        body = response.text.strip() if response is not None else str(exc)
        raise RuntimeError(
            f"Tempo team listing failed (HTTP {status}) at "
            f"'rest/tempo-teams/2/team'. Server response: {body[:800]}"
        ) from exc

    if isinstance(resp, list):
        return resp
    if isinstance(resp, dict):
        return resp.get("results") or resp.get("teams") or []
    return []


def _get_team_members_raw(team_id: int) -> list[dict[str, Any]]:
    """Fetch raw membership entries for a Tempo team."""
    jira = get_jira()
    try:
        resp = jira.tempo_teams_get_members(team_id)
    except HTTPError as exc:
        response = exc.response
        status = response.status_code if response is not None else "?"
        body = response.text.strip() if response is not None else str(exc)
        raise RuntimeError(
            f"Tempo team member lookup failed (HTTP {status}) at "
            f"'rest/tempo-teams/2/team/{team_id}/member/'. "
            f"Server response: {body[:800]}"
        ) from exc

    if isinstance(resp, list):
        return resp
    if isinstance(resp, dict):
        return resp.get("results") or resp.get("members") or []
    return []


def _normalize_member(entry: dict[str, Any]) -> dict[str, Any]:
    """Flatten a Tempo membership entry to the fields that actually matter.

    A membership nests the person under ``member`` and the assignment details
    (role, active window, availability) under ``membership``. Field names vary
    across Tempo versions, so every lookup is defensive.
    """
    member = entry.get("member") if isinstance(entry.get("member"), dict) else {}
    membership = entry.get("membership") if isinstance(entry.get("membership"), dict) else {}
    role = membership.get("role") if isinstance(membership.get("role"), dict) else {}
    return {
        # ``key`` is what the worklog ``worker`` field carries — match on this,
        # never on ``login``, which changes (e.g. after a surname change).
        "key": member.get("key") or member.get("name"),
        "login": member.get("name"),
        "display_name": member.get("displayname") or member.get("displayName"),
        "active_in_jira": member.get("activeInJira"),
        "membership_status": membership.get("status"),
        "type": member.get("type"),
        "role": role.get("name"),
        "date_from": membership.get("dateFromANSI") or membership.get("dateFrom"),
        "date_to": membership.get("dateToANSI") or membership.get("dateTo"),
        "availability": membership.get("availability"),
    }


def register(mcp: FastMCP) -> None:
    """Attach Tempo team tools to the FastMCP instance."""

    @mcp.tool()
    def get_all_teams(expand: str | None = None) -> list[dict[str, Any]]:
        """List every Tempo team (id, name, lead, …).

        Use this to discover a team's numeric ``teamId`` for
        ``get_team_worklog``. To look one up by name, prefer ``find_team``.

        Args:
            expand: Optional comma-separated Tempo expand fields
                (e.g. ``lead,program``).
        """
        return _get_all_teams(expand)

    @mcp.tool()
    def find_team(query: str, expand: str | None = None) -> list[dict[str, Any]]:
        """Find Tempo teams whose name contains ``query`` (case-insensitive).

        Tempo has no server-side team search, so this lists every team and
        filters locally. Handy to turn a team name into the ``teamId`` that
        ``get_team_worklog`` needs.

        Args:
            query: Substring to match against the team name.
            expand: Optional comma-separated Tempo expand fields
                (e.g. ``lead,program``).
        """
        needle = query.strip().lower()
        if not needle:
            raise ValueError("query must not be empty")
        return [
            team
            for team in _get_all_teams(expand)
            if needle in str(team.get("name", "")).lower()
        ]

    @mcp.tool()
    def get_team_members(team_id: int, raw: bool = False) -> list[dict[str, Any]]:
        """List all members of a Tempo team.

        Reads ``rest/tempo-teams/2/team/{team_id}/member/``. Each member's
        ``key`` is the same value the Tempo worklog ``worker`` field carries — so
        it's what you pass on to per-user worklog tools. Note this is the *Tempo
        team* roster, not a Jira user group (see ``get_all_users`` for groups).

        Args:
            team_id: Tempo team id (e.g. 79).
            raw: Return Tempo's untouched membership objects instead of the
                flattened ``key`` / ``display_name`` / ``role`` / membership-window
                shape. Use when you need fields not surfaced by default.
        """
        entries = _get_team_members_raw(team_id)
        if raw:
            return entries
        return [_normalize_member(e) for e in entries if isinstance(e, dict)]
