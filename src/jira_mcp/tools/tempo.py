"""Tempo Planner (resource planning) tools.

Reads the plans behind Tempo's "Planning" section: who is allocated to which
project/issue, for how many hours per day, over which period. On Server/DC
Tempo Planner is a Jira plugin, so the same base URL and PAT the rest of this
server uses work here too — no separate Tempo token needed.

Two endpoints are involved:

- ``rest/tempo-planning/1/allocation`` — the allocations (plans) themselves.
  Each spans ``start``..``end`` with a per-day load (``secondsPerDay``).
  NOTE: the assignee filter parameter is ``assigneeKeys`` (plural) and must be
  *repeated* for several users (``assigneeKeys=a&assigneeKeys=b``) — the
  singular ``assigneeKey`` is silently ignored and a comma-separated value
  matches nothing. One request covers any number of users.
- ``rest/tempo-core/1/user/schedule`` — a user's working-day calendar.
  An allocation's total excludes weekends *and* holidays (unless the plan has
  ``includeNonWorkingDays``), so expanding a plan into per-day hours with a
  plain Mon–Fri rule over-counts; the schedule gives the real working days.
  Calendars are per-user (part-time, regional holidays), so this is one
  request per user.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from typing import Any
from urllib.parse import urlencode

from mcp.server.fastmcp import FastMCP
from requests import HTTPError

from ..client import get_jira
from ..identity import planner_login, unknown_users_warning

_ALLOCATION_PATH = "rest/tempo-planning/1/allocation"
_SCHEDULE_PATH = "rest/tempo-core/1/user/schedule"


def _seconds_to_hours(seconds: int) -> float:
    """Convert seconds to hours rounded to 2 decimals."""
    return round(seconds / 3600, 2)


def _get(path: str, params: dict[str, Any], what: str) -> Any:
    """GET a Tempo REST resource, surfacing the server's error body on failure."""
    try:
        return get_jira().get(path, params=params)
    except HTTPError as exc:
        response = exc.response
        status = response.status_code if response is not None else "?"
        body = response.text.strip() if response is not None else str(exc)
        raise RuntimeError(
            f"Tempo {what} request failed (HTTP {status}) at '{path}'. "
            f"Params: {params}. Server response: {body[:800]}"
        ) from exc


def _fetch_allocations(
    usernames: list[str], date_from: str, date_to: str
) -> list[dict[str, Any]]:
    """Fetch plan allocations overlapping ``[date_from, date_to]`` for the users.

    A single request regardless of how many users: ``assigneeKeys`` is passed
    once per user. The query string is built here with ``doseq`` because the
    atlassian client urlencodes params itself *without* it — a list value would
    be stringified into ``assigneeKeys=['a', 'b']`` and match nothing.
    """
    query = urlencode(
        [("startDate", date_from), ("endDate", date_to)]
        + [("assigneeKeys", u) for u in usernames],
        doseq=True,
    )
    resp = _get(f"{_ALLOCATION_PATH}?{query}", {}, "Planner allocation")
    if isinstance(resp, list):
        return [a for a in resp if isinstance(a, dict)]
    return []


def _fetch_working_days(username: str, date_from: str, date_to: str) -> set[str]:
    """Return the user's working days (``YYYY-MM-DD``) in the range.

    A day counts as working when the schedule requires time on it — weekends
    and holidays come back with ``requiredSeconds: 0``.
    """
    resp = _get(
        _SCHEDULE_PATH,
        {"user": username, "from": date_from, "to": date_to},
        "user schedule",
    )
    days = resp.get("days", []) if isinstance(resp, dict) else []
    return {
        str(d["date"])
        for d in days
        if isinstance(d, dict) and d.get("date") and d.get("requiredSeconds", 0) > 0
    }


def _alloc_assignee(alloc: dict[str, Any]) -> str:
    """Assignee login of an allocation (matches the ``assigneeKeys`` filter)."""
    assignee = alloc.get("assignee")
    assignee = assignee if isinstance(assignee, dict) else {}
    return str(assignee.get("key") or "UNKNOWN")


def _plan_project_key(alloc: dict[str, Any]) -> str:
    """Project key of an allocation (plans target a whole PROJECT or an ISSUE)."""
    item = alloc.get("planItem")
    item = item if isinstance(item, dict) else {}
    key = item.get("projectKey") or str(item.get("key") or "UNKNOWN")
    return str(key).split("-", 1)[0] if item.get("type") == "ISSUE" and "-" in key else str(key)


def _iter_plan_days(
    alloc: dict[str, Any],
    range_from: date,
    range_to: date,
    working_days: set[str],
):
    """Yield each date within the range on which the allocation plans time."""
    try:
        start = max(date.fromisoformat(str(alloc.get("start"))[:10]), range_from)
        end = min(date.fromisoformat(str(alloc.get("end"))[:10]), range_to)
    except ValueError:
        return
    include_non_working = bool(alloc.get("includeNonWorkingDays"))
    day = start
    while day <= end:
        if include_non_working or day.isoformat() in working_days:
            yield day
        day += timedelta(days=1)


class _PlanAggregation:
    """Planned time for one user, expanded day-by-day from their allocations."""

    def __init__(self) -> None:
        self.total_seconds = 0
        self.plans: list[dict[str, Any]] = []
        self.by_project: dict[str, int] = defaultdict(int)
        self.project_name: dict[str, str] = {}
        self.day_total: dict[str, int] = defaultdict(int)
        self.day_project: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        self.warnings: list[str] = []

    def add(
        self,
        alloc: dict[str, Any],
        range_from: date,
        range_to: date,
        working_days: set[str],
    ) -> None:
        item = alloc.get("planItem")
        item = item if isinstance(item, dict) else {}
        project_key = _plan_project_key(alloc)
        if item.get("type") == "PROJECT" and item.get("name"):
            self.project_name.setdefault(project_key, item["name"])

        rule = (alloc.get("recurrence") or {}).get("rule")
        if rule not in (None, "NEVER"):
            self.warnings.append(
                f"allocation {alloc.get('id')} has recurrence rule '{rule}' "
                f"which is not expanded; its per-day hours may be off"
            )

        seconds_per_day = int(alloc.get("secondsPerDay") or 0)
        plan_seconds = 0
        for day in _iter_plan_days(alloc, range_from, range_to, working_days):
            plan_seconds += seconds_per_day
            key = day.isoformat()
            self.day_total[key] += seconds_per_day
            self.day_project[key][project_key] += seconds_per_day

        self.total_seconds += plan_seconds
        self.by_project[project_key] += plan_seconds
        self.plans.append(
            {
                "id": alloc.get("id"),
                "type": item.get("type"),
                "key": item.get("key"),
                "project_key": project_key,
                "name": item.get("summary") or item.get("name"),
                "hours_per_day": _seconds_to_hours(seconds_per_day),
                "commitment_percent": alloc.get("commitment"),
                "start": alloc.get("start"),
                "end": alloc.get("end"),
                "include_non_working_days": bool(alloc.get("includeNonWorkingDays")),
                "planned_hours_in_range": _seconds_to_hours(plan_seconds),
                "description": alloc.get("description") or None,
            }
        )


def _project_map(
    totals: dict[str, int], names: dict[str, str]
) -> dict[str, dict[str, Any]]:
    """Render a {project_key: seconds} map as sorted {key: {name, hours, seconds}}."""
    return {
        key: {
            "name": names.get(key),
            "hours": _seconds_to_hours(seconds),
            "seconds": seconds,
        }
        for key, seconds in sorted(totals.items(), key=lambda kv: kv[1], reverse=True)
    }


def _render_days(agg: _PlanAggregation) -> dict[str, dict[str, Any]]:
    """Render per-day planned hours, split by project inside each day."""
    return {
        day: {
            "hours": _seconds_to_hours(agg.day_total[day]),
            "seconds": agg.day_total[day],
            "projects": {
                key: {"hours": _seconds_to_hours(s), "seconds": s}
                for key, s in sorted(
                    agg.day_project[day].items(), key=lambda kv: kv[1], reverse=True
                )
            },
        }
        for day in sorted(agg.day_total)
    }


def _weekdays(range_from: date, range_to: date) -> set[str]:
    """Mon–Fri days in the range — fallback when the Tempo schedule is unavailable."""
    day, out = range_from, set()
    while day <= range_to:
        if day.weekday() < 5:
            out.add(day.isoformat())
        day += timedelta(days=1)
    return out


def _aggregate_user(
    username: str,
    allocations: list[dict[str, Any]],
    date_from: str,
    date_to: str,
    range_from: date,
    range_to: date,
) -> tuple[_PlanAggregation, set[str]]:
    """Expand one user's allocations against their own working-day calendar.

    A failed schedule lookup (unknown login, no Tempo permission) must not sink
    a multi-user report: fall back to plain Mon–Fri and attach a warning —
    per-day hours may then over-count public holidays.
    """
    agg = _PlanAggregation()
    try:
        working_days = _fetch_working_days(username, date_from, date_to)
    except RuntimeError as exc:
        working_days = _weekdays(range_from, range_to)
        agg.warnings.append(
            f"schedule for user '{username}' unavailable ({exc}); "
            f"falling back to Mon-Fri, public holidays may be over-counted"
        )
    for alloc in allocations:
        agg.add(alloc, range_from, range_to, working_days)
    agg.plans.sort(key=lambda p: p["planned_hours_in_range"], reverse=True)
    return agg, working_days


def _resolve_assignees(users: list[str]) -> list[str]:
    """Turn caller-supplied logins/keys into Planner assignee logins.

    ``assigneeKeys`` and the schedule endpoint both match the *login*, not the
    user key — a ``JIRAUSER…`` string matches no allocation and 404s the
    schedule (see :mod:`jira_mcp.identity`). De-duplicated because the same
    person may arrive under both handles.
    """
    logins: list[str] = []
    for user in users:
        login = planner_login(user)
        if login not in logins:
            logins.append(login)
    return logins


def _parse_range(date_from: str, date_to: str) -> tuple[date, date]:
    """Validate and parse the inclusive date range."""
    range_from = date.fromisoformat(date_from)
    range_to = date.fromisoformat(date_to)
    if range_from > range_to:
        raise ValueError("date_from must not be after date_to")
    return range_from, range_to


def register(mcp: FastMCP) -> None:
    """Attach Tempo Planner tools to the FastMCP instance."""

    @mcp.tool()
    def get_user_plan(
        username: str,
        date_from: str,
        date_to: str,
        breakdown: str | None = None,
    ) -> dict[str, Any]:
        """What a user is planned for in Tempo Planner over a date range.

        Answers "which projects is this person allocated to, for how many hours
        per day": fetches the user's plan allocations and expands them into
        planned hours within ``[date_from, date_to]`` (both inclusive), using
        the user's Tempo working-day calendar so weekends and public holidays
        are excluded exactly as Tempo itself does.

        Args:
            username: The person — either their Jira login (``acutina``) or
                their user key (``JIRAUSER18613``). Planner itself only matches
                the login; the key is resolved to it here, so both work.
            date_from: Start date, inclusive, ``YYYY-MM-DD``.
            date_to: End date, inclusive, ``YYYY-MM-DD``.
            breakdown: Pass ``day`` to add ``by_day`` — planned hours for each
                date in the range, split by project inside each day. Omit for
                the compact view (plans + per-project totals only).

        Returns ``plans`` (each allocation with its ``hours_per_day``, period
        and target project/issue), ``by_project`` totals for the range, and
        optionally ``by_day``.
        """
        if breakdown not in (None, "day"):
            raise ValueError("breakdown must be omitted or 'day'")
        range_from, range_to = _parse_range(date_from, date_to)

        login = planner_login(username)
        allocations = _fetch_allocations([login], date_from, date_to)
        agg, working_days = _aggregate_user(
            login, allocations, date_from, date_to, range_from, range_to
        )

        result: dict[str, Any] = {
            "user": username,
            "login": login,
            "date_from": date_from,
            "date_to": date_to,
            "workdays": len(working_days),
            "total_hours": _seconds_to_hours(agg.total_seconds),
            "total_seconds": agg.total_seconds,
            "plan_count": len(agg.plans),
            "plans": agg.plans,
            "by_project": _project_map(agg.by_project, agg.project_name),
        }
        if breakdown == "day":
            result["by_day"] = _render_days(agg)
        warnings = list(agg.warnings)
        unknown = unknown_users_warning([username])
        if unknown:
            warnings.insert(0, unknown)
        if warnings:
            result["warnings"] = warnings
        return result

    @mcp.tool()
    def get_users_plan(
        usernames: list[str],
        date_from: str,
        date_to: str,
        breakdown: str | None = None,
    ) -> dict[str, Any]:
        """What a group of users is planned for in Tempo Planner, per user.

        One allocation request covers every user in ``usernames`` for
        ``[date_from, date_to]`` (both inclusive) — never call this once per
        user. Each user's plans are then expanded against their own
        working-day calendar (calendars differ: part-time, regional holidays),
        so weekends and public holidays are excluded exactly as Tempo does.

        Args:
            usernames: The people — Jira logins (``acutina``) or user keys
                (``JIRAUSER18613``), mixed freely; keys are resolved to logins
                here, which is what Planner matches on.
            date_from: Start date, inclusive, ``YYYY-MM-DD``.
            date_to: End date, inclusive, ``YYYY-MM-DD``.
            breakdown: Pass ``day`` to add ``by_day`` inside every user's
                entry — planned hours for each date, split by project within
                each day. Omit for the compact view.

        Returns ``by_user`` (each user's ``plans``, ``by_project`` totals and
        optionally ``by_day``, users sorted by planned hours desc; users with
        no plans appear with 0) and cross-user ``by_project`` totals.
        """
        if not usernames:
            raise ValueError("usernames must not be empty")
        if breakdown not in (None, "day"):
            raise ValueError("breakdown must be omitted or 'day'")
        range_from, range_to = _parse_range(date_from, date_to)

        logins = _resolve_assignees(usernames)
        allocations = _fetch_allocations(logins, date_from, date_to)
        per_user: dict[str, list[dict[str, Any]]] = {u: [] for u in logins}
        for alloc in allocations:
            per_user.setdefault(_alloc_assignee(alloc), []).append(alloc)

        total_seconds = 0
        all_projects: dict[str, int] = defaultdict(int)
        project_name: dict[str, str] = {}
        warnings: list[str] = []
        unknown = unknown_users_warning(usernames)
        if unknown:
            warnings.append(unknown)
        by_user: dict[str, dict[str, Any]] = {}

        for username, user_allocs in per_user.items():
            agg, working_days = _aggregate_user(
                username, user_allocs, date_from, date_to, range_from, range_to
            )
            total_seconds += agg.total_seconds
            for key, seconds in agg.by_project.items():
                all_projects[key] += seconds
            project_name.update(agg.project_name)
            warnings.extend(agg.warnings)

            entry: dict[str, Any] = {
                "workdays": len(working_days),
                "total_hours": _seconds_to_hours(agg.total_seconds),
                "total_seconds": agg.total_seconds,
                "plan_count": len(agg.plans),
                "plans": agg.plans,
                "by_project": _project_map(agg.by_project, agg.project_name),
            }
            if breakdown == "day":
                entry["by_day"] = _render_days(agg)
            by_user[username] = entry

        by_user = dict(
            sorted(by_user.items(), key=lambda kv: kv[1]["total_seconds"], reverse=True)
        )
        result: dict[str, Any] = {
            "users": usernames,
            "logins": logins,
            "date_from": date_from,
            "date_to": date_to,
            "total_hours": _seconds_to_hours(total_seconds),
            "total_seconds": total_seconds,
            "by_user": by_user,
            "by_project": _project_map(all_projects, project_name),
        }
        if warnings:
            result["warnings"] = warnings
        return result
