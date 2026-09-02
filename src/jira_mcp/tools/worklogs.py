"""Worklog tools: reporting (Tempo Timesheets) and logging time.

Aggregates how much time users logged over a date range using the Tempo
Timesheets API: a single search request returns every matching worklog for the
period (filtered server-side by worker and project), so there is no per-issue
fan-out. The ``worker`` filter accepts several users, so a whole team is one
request too.

Breakdowns compose. When ``day`` is combined with ``project`` and/or ``issue``,
the project/issue totals are nested *inside each day* (hours per project/issue
per day). Without ``day`` they are returned flat for the whole period.

``add_worklog`` is the one write here — it posts to Jira's core worklog API
(``rest/api/2/issue/{key}/worklog``) as the token's owner; Tempo on Server/DC
reads the same worklogs, so logged time shows up in the reports above.
"""

from __future__ import annotations

import re
from collections import defaultdict
from datetime import date, datetime, time
from typing import Any

from mcp.server.fastmcp import FastMCP
from requests import HTTPError

from ..client import get_jira
from ..identity import display_name, unknown_users_warning, worker_key

_VALID_DIMS = {"day", "project", "issue", "account"}

# Tempo account key/category marking non-working time (vacation, sick leave).
# The English key is stable; the display name is localised ("Отсутствие").
_ABSENCE_MARKERS = {"absence"}

# Jira duration syntax accepted by the worklog API: "2h", "90m", "1d 4h".
_TIME_SPENT_RE = re.compile(r"^\d+(\.\d+)?[wdhm](\s*\d+(\.\d+)?[wdhm])*$")
_JIRA_DATETIME_FMT = "%Y-%m-%dT%H:%M:%S.000%z"
# Hour of the day a date-only ``started`` is anchored at (see _parse_started).
_DEFAULT_START_HOUR = 9


def _seconds_to_hours(seconds: int) -> float:
    """Convert seconds to hours rounded to 2 decimals."""
    return round(seconds / 3600, 2)


def _issue_of(worklog: dict[str, Any]) -> dict[str, Any]:
    """Return the worklog's issue object, tolerating Tempo shape differences."""
    issue = worklog.get("issue")
    return issue if isinstance(issue, dict) else {}


def _worklog_date(worklog: dict[str, Any]) -> str:
    """Return the worklog's date as ``YYYY-MM-DD`` (Tempo field name varies)."""
    raw = worklog.get("started") or worklog.get("dateStarted") or ""
    return str(raw)[:10]


def _worklog_worker(worklog: dict[str, Any]) -> str:
    """Return the worklog's worker key (Tempo's ``worker`` — often ``JIRAUSER…``)."""
    return worklog.get("worker") or "UNKNOWN"


def _worklog_worker_display(worklog: dict[str, Any]) -> str | None:
    """Best-effort readable name for the worker, if Tempo attached one.

    Tempo's ``worker`` is an opaque account key; a display name may live on an
    ``author`` object or a flat field depending on the instance/version.
    """
    author = worklog.get("author")
    if isinstance(author, dict):
        name = author.get("displayName") or author.get("displayname")
        if name:
            return name
    return worklog.get("workerDisplayName") or worklog.get("workerFullName")


def _resolve_workers(users: list[str]) -> tuple[list[str], dict[str, str]]:
    """Turn caller-supplied logins/keys into Tempo worker keys, de-duplicated.

    Tempo's ``worker`` filter matches the user *key*, so a login silently
    matches nothing (see :mod:`jira_mcp.identity`). De-duplication matters
    because the same person can arrive twice under both handles — as a worker
    key twice over they would seed two rows for one human.

    Also returns the readable names Jira knows for those keys: worklogs
    themselves often carry no display name, and a report keyed by ``JIRAUSER…``
    is unreadable.
    """
    keys: list[str] = []
    displays: dict[str, str] = {}
    for user in users:
        key = worker_key(user)
        if key not in keys:
            keys.append(key)
        name = display_name(user)
        if name:
            displays[key] = name
    return keys, displays


def _worklog_account(worklog: dict[str, Any]) -> tuple[str, str | None]:
    """Return ``(account_key, account_name)`` for a worklog.

    Tempo exposes the account (the billing bucket behind the CSV export's
    "Ключ аккаунта" / "Имя аккаунта" columns) in different places depending on
    version, so probe the known shapes in order and fall back to ``UNKNOWN``.
    """
    candidates: list[Any] = [
        worklog.get("account"),
        _issue_of(worklog).get("account"),
    ]
    for account in candidates:
        if isinstance(account, dict):
            key = account.get("key") or account.get("id")
            if key:
                return str(key), account.get("name")

    issue = _issue_of(worklog)
    flat_key = issue.get("accountKey") or worklog.get("accountKey")
    if flat_key:
        return str(flat_key), issue.get("accountName") or worklog.get("accountName")

    # Tempo work-attributes carry it as a custom attribute on some instances.
    attributes = worklog.get("attributes")
    if isinstance(attributes, dict):
        attr = attributes.get("_Account_") or attributes.get("Account")
        if isinstance(attr, dict) and attr.get("value"):
            return str(attr["value"]), None
    return "UNKNOWN", None


def _is_absence(worklog: dict[str, Any]) -> bool:
    """Whether the worklog is non-working time (vacation / sick leave).

    Matched on the Tempo account key, not the display name, since the name is
    localised. Absence is logged against a dedicated account, so a worklog's
    8 hours of vacation are indistinguishable from 8 hours of work without it.
    """
    key, _ = _worklog_account(worklog)
    if key.lower() in _ABSENCE_MARKERS:
        return True
    account = worklog.get("account")
    if isinstance(account, dict):
        category = account.get("category")
        if isinstance(category, dict):
            category = category.get("key") or category.get("name")
        if str(category or "").lower() in _ABSENCE_MARKERS:
            return True
    return False


def _parse_breakdown(breakdown: str | None) -> set[str]:
    """Parse the ``breakdown`` argument into a set of grouping dimensions.

    Accepts ``day``, ``project``, ``issue``, the alias ``both`` (=project+issue),
    ``all`` (=day+project+issue), or a comma-separated combination
    (e.g. ``day,project``).
    """
    if not breakdown:
        return set()
    dims: set[str] = set()
    for token in breakdown.split(","):
        token = token.strip().lower()
        if token == "both":
            dims |= {"project", "issue"}
        elif token == "all":
            dims |= set(_VALID_DIMS)
        elif token in _VALID_DIMS:
            dims.add(token)
        else:
            raise ValueError(
                "breakdown parts must be one of: day, project, issue, account, both, all"
            )
    return dims


def _fetch_worklogs(
    workers: list[str] | None,
    date_from: str,
    date_to: str,
    project: str | None,
    team_id: int | None = None,
    include_subtasks: bool | None = None,
) -> list[dict[str, Any]]:
    """Fetch all Tempo worklogs for the given filters/period.

    Filters compose server-side: ``workers`` restricts to specific Tempo
    workers, ``team_id`` restricts to a Tempo team's worklogs (the ``teamId``
    filter behind the "Logged Time" report), ``project`` restricts to a project.

    Tempo Timesheets v4 ``/worklogs/search`` (``SearchParamsBean``) has no
    pagination fields — it returns every matching worklog in a single response —
    so we must not send ``offset``/``maxResults`` (they raise HTTP 400
    "Unrecognized field").
    """
    jira = get_jira()
    search_params: dict[str, Any] = {}
    if workers:
        search_params["worker"] = workers
    if team_id is not None:
        search_params["teamId"] = [team_id]
    if project:
        search_params["projectKey"] = [project]
    if include_subtasks is not None:
        search_params["includeSubtasks"] = include_subtasks

    try:
        resp = jira.tempo_4_timesheets_find_worklogs(
            date_from=date_from,
            date_to=date_to,
            **search_params,
        )
    except HTTPError as exc:
        # Surface Tempo's actual error body instead of a bare "400".
        response = exc.response
        status = response.status_code if response is not None else "?"
        body = response.text.strip() if response is not None else str(exc)
        raise RuntimeError(
            f"Tempo Timesheets worklog search failed (HTTP {status}) at "
            f"'rest/tempo-timesheets/4/worklogs/search'. "
            f"Params: worker={search_params.get('worker')}, "
            f"teamId={search_params.get('teamId')}, "
            f"from={date_from}, to={date_to}, "
            f"projectKey={search_params.get('projectKey')}. "
            f"Server response: {body[:800]}"
        ) from exc

    # Tempo may return a bare list or wrap it in an object.
    if isinstance(resp, list):
        return resp
    if isinstance(resp, dict):
        return resp.get("results") or resp.get("worklogs") or []
    return []


class _Aggregation:
    """Accumulates worklog seconds across every dimension in one pass."""

    def __init__(self) -> None:
        self.total_seconds = 0
        self.by_worker: dict[str, int] = defaultdict(int)
        self.by_day: dict[str, int] = defaultdict(int)
        self.by_project: dict[str, int] = defaultdict(int)
        self.by_issue: dict[str, int] = defaultdict(int)
        self.issue_summary: dict[str, str | None] = {}
        # Readable name per worker key, when Tempo provides one on the worklog.
        self.worker_display: dict[str, str] = {}
        # Nested: day -> project/issue -> seconds.
        self.day_project: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        self.day_issue: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        # Nested: worker -> day/project/issue -> seconds (per-person breakdowns,
        # built in the same single pass so no extra API calls are needed).
        self.worker_day: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        self.worker_project: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        self.worker_issue: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        self.by_account: dict[str, int] = defaultdict(int)
        self.worker_account: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        self.account_name: dict[str, str | None] = {}

    def add(self, worklog: dict[str, Any]) -> None:
        seconds = worklog.get("timeSpentSeconds", 0)
        self.total_seconds += seconds

        worker = _worklog_worker(worklog)
        self.by_worker[worker] += seconds
        display = _worklog_worker_display(worklog)
        if display:
            self.worker_display.setdefault(worker, display)

        day = _worklog_date(worklog)
        self.by_day[day] += seconds

        issue = _issue_of(worklog)
        key = issue.get("key", "UNKNOWN")
        project_key = issue.get("projectKey") or key.split("-", 1)[0]
        self.issue_summary.setdefault(key, issue.get("summary"))

        self.by_project[project_key] += seconds
        self.by_issue[key] += seconds
        self.day_project[day][project_key] += seconds
        self.day_issue[day][key] += seconds

        self.worker_day[worker][day] += seconds
        self.worker_project[worker][project_key] += seconds
        self.worker_issue[worker][key] += seconds

        account_key, account_name = _worklog_account(worklog)
        if account_name:
            self.account_name.setdefault(account_key, account_name)
        self.by_account[account_key] += seconds
        self.worker_account[worker][account_key] += seconds


def _fetch_account_names() -> dict[str, str]:
    """Return ``{account_key: account_name}`` from the Tempo Accounts API.

    Worklogs carry only the account key (``FINHELP``), not the readable name
    ("Банк России_Фикс"). One extra request buys names for every account, so it
    is issued only when an account breakdown was actually asked for. A failure
    here must not sink the report — names degrade to ``None``.
    """
    try:
        accounts = get_jira().tempo_account_get_accounts()
    except Exception:  # noqa: BLE001 - names are cosmetic, never fail the report
        return {}
    names: dict[str, str] = {}
    for account in accounts if isinstance(accounts, list) else []:
        if isinstance(account, dict) and (key := account.get("key")):
            if name := account.get("name"):
                names[str(key)] = name
    return names


def _split_absence(
    worklogs: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], int]:
    """Split off absence worklogs, returning ``(working, absence_seconds)``."""
    working: list[dict[str, Any]] = []
    absence_seconds = 0
    for worklog in worklogs:
        if _is_absence(worklog):
            absence_seconds += worklog.get("timeSpentSeconds", 0)
        else:
            working.append(worklog)
    return working, absence_seconds


def _aggregate(worklogs: list[dict[str, Any]]) -> _Aggregation:
    agg = _Aggregation()
    for wl in worklogs:
        agg.add(wl)
    return agg


def _seconds_map(totals: dict[str, int]) -> dict[str, dict[str, Any]]:
    """Render a {key: seconds} map as a sorted (desc) {key: {hours, seconds}} map."""
    return {
        k: {"hours": _seconds_to_hours(s), "seconds": s}
        for k, s in sorted(totals.items(), key=lambda kv: kv[1], reverse=True)
    }


def _issue_map(
    totals: dict[str, int], summaries: dict[str, str | None]
) -> dict[str, dict[str, Any]]:
    """Render a {issue_key: seconds} map, attaching each issue's summary."""
    return {
        k: {
            "summary": summaries.get(k),
            "hours": _seconds_to_hours(s),
            "seconds": s,
        }
        for k, s in sorted(totals.items(), key=lambda kv: kv[1], reverse=True)
    }


def _account_map(
    totals: dict[str, int], names: dict[str, str | None]
) -> dict[str, dict[str, Any]]:
    """Render a {account_key: seconds} map, attaching each account's name."""
    return {
        k: {
            "name": names.get(k),
            "hours": _seconds_to_hours(s),
            "seconds": s,
        }
        for k, s in sorted(totals.items(), key=lambda kv: kv[1], reverse=True)
    }


def _render_days(agg: _Aggregation, dims: set[str]) -> dict[str, dict[str, Any]]:
    """Render the per-day breakdown, nesting project/issue totals under each day."""
    out: dict[str, dict[str, Any]] = {}
    for day in sorted(agg.by_day):  # chronological order
        seconds = agg.by_day[day]
        entry: dict[str, Any] = {
            "hours": _seconds_to_hours(seconds),
            "seconds": seconds,
        }
        if "project" in dims:
            entry["projects"] = _seconds_map(agg.day_project[day])
        if "issue" in dims:
            entry["issues"] = _issue_map(agg.day_issue[day], agg.issue_summary)
        out[day] = entry
    return out


def _render_breakdowns(agg: _Aggregation, dims: set[str]) -> dict[str, Any]:
    """Build the breakdown section of a report from the requested dimensions.

    With ``day`` present, project/issue totals are nested inside each day.
    Without ``day`` they are flat over the whole period.
    """
    result: dict[str, Any] = {}
    if "day" in dims:
        result["by_day"] = _render_days(agg, dims)
        return result
    if "project" in dims:
        result["by_project"] = _seconds_map(agg.by_project)
    if "issue" in dims:
        result["by_issue"] = _issue_map(agg.by_issue, agg.issue_summary)
    if "account" in dims:
        result["by_account"] = _account_map(agg.by_account, agg.account_name)
    return result


def _render_users(
    agg: _Aggregation,
    dims: set[str],
    display_by_worker: dict[str, str],
) -> dict[str, dict[str, Any]]:
    """Render the per-worker section, sorted by hours desc.

    Each worker carries a readable ``display`` name (from the team roster or the
    worklog author, falling back to ``None`` → caller shows the key) and, when
    ``dims`` are requested, sibling ``by_day`` / ``by_project`` / ``by_issue``
    breakdowns scoped to that person. These come from the same single worklog
    fetch — no per-user API calls.
    """
    out: dict[str, dict[str, Any]] = {}
    for worker, seconds in sorted(agg.by_worker.items(), key=lambda kv: kv[1], reverse=True):
        entry: dict[str, Any] = {
            "display": display_by_worker.get(worker),
            "hours": _seconds_to_hours(seconds),
            "seconds": seconds,
        }
        if "day" in dims:
            # Chronological, and includes every day the worker logged time.
            worker_days = agg.worker_day.get(worker, {})
            entry["by_day"] = {
                day: {"hours": _seconds_to_hours(s), "seconds": s}
                for day, s in sorted(worker_days.items())
            }
        if "project" in dims:
            entry["by_project"] = _seconds_map(agg.worker_project.get(worker, {}))
        if "issue" in dims:
            entry["by_issue"] = _issue_map(agg.worker_issue.get(worker, {}), agg.issue_summary)
        if "account" in dims:
            entry["by_account"] = _account_map(
                agg.worker_account.get(worker, {}), agg.account_name
            )
        out[worker] = entry
    return out


def _member_key(entry: dict[str, Any]) -> str | None:
    """Extract the worker key from a Tempo team-member entry.

    CRITICAL: match on ``member.key``, never ``member.name``. Tempo's worklog
    ``worker`` field carries the *key* — a stable internal id, usually
    ``JIRAUSER…``. ``name`` is the human login and it *changes* (e.g. after a
    surname change), so matching on it silently fails and the same person shows
    up twice: once with hours under the key, once with zero under the login.
    """
    member = entry.get("member")
    if not isinstance(member, dict):
        return None
    if member.get("type") not in (None, "USER"):
        return None
    return member.get("key") or member.get("name")


def _member_display(entry: dict[str, Any]) -> str | None:
    """Readable name of a Tempo team-member entry (``displayname``)."""
    member = entry.get("member")
    if not isinstance(member, dict):
        return None
    return member.get("displayname") or member.get("displayName")


def _member_is_active(entry: dict[str, Any]) -> bool:
    """Whether the member is a current employee on a current membership.

    Two independent conditions, both from the roster itself (no extra request):
    the Jira account is not deactivated (``member.activeInJira``), and the
    team membership window is still open (``membership.dateTo`` empty, or in
    the future). A closed membership means they moved off the team.
    """
    member = entry.get("member") if isinstance(entry.get("member"), dict) else {}
    membership = entry.get("membership") if isinstance(entry.get("membership"), dict) else {}

    if member.get("activeInJira") is False:
        return False
    if str(membership.get("status") or "").lower() == "past":
        return False

    date_to = membership.get("dateToANSI") or membership.get("dateTo")
    if date_to:  # empty string / None means "open ended"
        try:
            return date.fromisoformat(str(date_to)[:10]) >= date.today()
        except ValueError:
            return True  # unparseable → don't silently drop the person
    return True


def _fetch_team_members(team_id: int) -> dict[str, dict[str, Any]]:
    """Return ``{worker_key: {display, login, active}}`` for every team member.

    Keyed by ``member.key`` so it lines up with the worklog ``worker`` field.
    Order is preserved and keys de-duplicated. Fetched anyway to include
    zero-hours members, so the display names and active flags are free.
    """
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
        entries = resp
    elif isinstance(resp, dict):
        entries = resp.get("results", [])
    else:
        entries = []
    members: dict[str, dict[str, Any]] = {}  # preserve order, de-duplicate
    for entry in entries:
        if isinstance(entry, dict) and (key := _member_key(entry)):
            member = entry.get("member") if isinstance(entry.get("member"), dict) else {}
            members.setdefault(
                key,
                {
                    "display": _member_display(entry),
                    "login": member.get("name"),
                    "active": _member_is_active(entry),
                },
            )
    return members


def _validate_time_spent(time_spent: str) -> str:
    """Normalise a Jira duration (``2h 30m``), rejecting anything Jira can't parse."""
    value = " ".join(time_spent.split()).lower()
    if not _TIME_SPENT_RE.match(value):
        raise ValueError(
            f"time_spent '{time_spent}' is not a Jira duration: use a number plus "
            f"w/d/h/m, space-separated — '2h', '90m', '2h 30m', '1d 4h'"
        )
    return value


def _parse_started(started: str | None) -> str:
    """Build Jira's ``started`` timestamp from a date, datetime or ``None``.

    Jira wants ``yyyy-MM-dd'T'HH:mm:ss.SSSZ`` — an offset is mandatory, and it
    decides which calendar day the worklog lands on. A bare ``YYYY-MM-DD`` is
    therefore anchored at 09:00 *local* time rather than midnight UTC, which in
    an eastern timezone would silently move the worklog to the previous day.
    """
    local_tz = datetime.now().astimezone().tzinfo
    if not started:
        moment = datetime.now(tz=local_tz)
    else:
        text = started.strip().replace(" ", "T", 1)
        try:
            if len(text) == 10:
                moment = datetime.combine(
                    date.fromisoformat(text), time(_DEFAULT_START_HOUR), tzinfo=local_tz
                )
            else:
                moment = datetime.fromisoformat(text)
        except ValueError as exc:
            raise ValueError(
                f"started '{started}' is not a date or datetime: use 'YYYY-MM-DD', "
                f"'YYYY-MM-DDTHH:MM', or a timestamp with an offset"
            ) from exc
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=local_tz)
    return moment.strftime(_JIRA_DATETIME_FMT)


def register(mcp: FastMCP) -> None:
    """Attach worklog tools to the FastMCP instance."""

    @mcp.tool()
    def get_user_worklog(
        username: str,
        date_from: str,
        date_to: str,
        project: str | None = None,
        breakdown: str | None = None,
        exclude_absence: bool = False,
    ) -> dict[str, Any]:
        """Total hours a user logged in a date range, optionally broken down.

        Uses the Tempo Timesheets API: one search returns all of the user's
        worklogs in ``[date_from, date_to]`` (both inclusive), filtered
        server-side.

        Args:
            username: The person — either their Jira login (``acutina``) or
                their user key (``JIRAUSER18613``). Tempo itself only matches
                the key; the login is resolved to it here, so both work.
            date_from: Start date, inclusive, ``YYYY-MM-DD``.
            date_to: End date, inclusive, ``YYYY-MM-DD``.
            project: Optional project key to restrict to (e.g. ``PROJ``).
            breakdown: Optional grouping — ``day``, ``project``, ``issue``,
                ``account``, ``both`` (=project+issue), ``all``, or a
                comma-separated combo like ``day,project``. When ``day`` is
                combined with ``project``/``issue`` the project/issue hours are
                nested inside each day; otherwise they are flat for the period.
            exclude_absence: Drop vacation / sick-leave worklogs (Tempo's
                ``Absence`` account). Off by default so totals match Tempo.
        """
        dims = _parse_breakdown(breakdown)
        worker = worker_key(username)
        worklogs = _fetch_worklogs([worker], date_from, date_to, project)
        absence_seconds = 0
        if exclude_absence:
            worklogs, absence_seconds = _split_absence(worklogs)
        agg = _aggregate(worklogs)

        result: dict[str, Any] = {
            "user": username,
            "worker_key": worker,
            "display": display_name(username),
            "date_from": date_from,
            "date_to": date_to,
            "project": project,
            "exclude_absence": exclude_absence,
            "excluded_absence_hours": _seconds_to_hours(absence_seconds),
            "total_hours": _seconds_to_hours(agg.total_seconds),
            "total_seconds": agg.total_seconds,
            "worklog_count": len(worklogs),
        }
        result.update(_render_breakdowns(agg, dims))
        warning = unknown_users_warning([username])
        if warning:
            result["warnings"] = [warning]
        return result

    @mcp.tool()
    def get_users_worklog(
        usernames: list[str],
        date_from: str,
        date_to: str,
        project: str | None = None,
        breakdown: str | None = None,
        exclude_absence: bool = False,
    ) -> dict[str, Any]:
        """Total hours a group of users logged in a date range, per user.

        Uses the Tempo Timesheets API: one search covers every user in
        ``usernames`` for ``[date_from, date_to]`` (both inclusive) — a single
        request, regardless of team size or breakdown. ``by_user`` always lists
        each worker's total; ``breakdown`` nests that worker's own day/project/
        issue split inside their entry (still one request — never call this once
        per user).

        Args:
            usernames: The people — Jira logins (``acutina``) or user keys
                (``JIRAUSER18613``), mixed freely; logins are resolved to keys
                here, which is what Tempo matches on. ``by_user`` is keyed by
                the resolved key and carries the person's ``display`` name.
            date_from: Start date, inclusive, ``YYYY-MM-DD``.
            date_to: End date, inclusive, ``YYYY-MM-DD``.
            project: Optional project key to restrict to (e.g. ``PROJ``).
            breakdown: Optional per-worker grouping — ``day``, ``project``,
                ``issue``, ``both`` (=project+issue), ``all``, or a comma combo
                like ``day,project``. Each requested dimension appears as
                ``by_day`` / ``by_project`` / ``by_issue`` inside every user's
                entry (e.g. ``day`` → hours-per-day per worker in one call).
        """
        if not usernames:
            raise ValueError("usernames must not be empty")
        dims = _parse_breakdown(breakdown)
        workers, displays = _resolve_workers(usernames)
        worklogs = _fetch_worklogs(workers, date_from, date_to, project)
        absence_seconds = 0
        if exclude_absence:
            worklogs, absence_seconds = _split_absence(worklogs)
        agg = _aggregate(worklogs)

        # Ensure every requested user appears, even with zero logged time.
        for worker in workers:
            agg.by_worker.setdefault(worker, 0)

        result: dict[str, Any] = {
            "users": usernames,
            "worker_keys": workers,
            "date_from": date_from,
            "date_to": date_to,
            "project": project,
            "exclude_absence": exclude_absence,
            "excluded_absence_hours": _seconds_to_hours(absence_seconds),
            "total_hours": _seconds_to_hours(agg.total_seconds),
            "total_seconds": agg.total_seconds,
            "worklog_count": len(worklogs),
        }
        if "account" in dims:
            agg.account_name = {**_fetch_account_names(), **agg.account_name}
        # Tempo's own name wins when it attached one; Jira's fills the rest.
        result["by_user"] = _render_users(agg, dims, {**displays, **agg.worker_display})
        warning = unknown_users_warning(usernames)
        if warning:
            result["warnings"] = [warning]
        if "account" in dims:
            result["by_account"] = _account_map(agg.by_account, agg.account_name)
        return result

    @mcp.tool()
    def get_team_worklog(
        team_id: int,
        date_from: str,
        date_to: str,
        include_subtasks: bool | None = None,
        breakdown: str | None = None,
        only_active: bool = True,
        exclude_absence: bool = False,
    ) -> dict[str, Any]:
        """Hours every member of a Tempo team logged in a date range, per worker.

        Reproduces Tempo's "Logged Time" report grouped by worker (the
        ``Tempo.jspa#/reports/logged-time?teamId=…&groupBy=worker`` view): one
        worklog search filtered by ``teamId`` covers the whole team for
        ``[date_from, date_to]`` (both inclusive). The team roster is fetched
        separately so members who logged nothing still appear with 0 hours.

        Args:
            team_id: Tempo team id (the ``teamId`` from the report URL, e.g. 79).
            date_from: Start date, inclusive, ``YYYY-MM-DD``.
            date_to: End date, inclusive, ``YYYY-MM-DD``.
            include_subtasks: Include worklogs on subtasks (the report's
                ``includeSubtasks`` toggle). Defaults to Tempo's own default.
            breakdown: Optional per-worker grouping — ``day``, ``project``,
                ``issue``, ``both`` (=project+issue), ``all``, or a comma combo
                like ``day,project``. Each requested dimension appears as
                ``by_day`` / ``by_project`` / ``by_issue`` inside every worker's
                entry (e.g. ``day`` → hours-per-day per worker, the whole
                timesheet matrix, in ONE call — never call this once per user).
            only_active: Keep only current employees on an open team membership
                (default). Set False to include people who left the company or
                moved off the team — a team's roster accumulates them for years,
                so the default keeps the report to the people working now.
            exclude_absence: Drop vacation / sick-leave worklogs, which Tempo
                logs against a dedicated ``Absence`` account. Off by default so
                totals match Tempo's own report; turn it on to read the numbers
                as actual workload — otherwise a week of vacation looks
                identical to a week of work (8h/day either way).
        """
        dims = _parse_breakdown(breakdown)
        worklogs = _fetch_worklogs(
            None,
            date_from,
            date_to,
            project=None,
            team_id=team_id,
            include_subtasks=include_subtasks,
        )
        absence_seconds = 0
        if exclude_absence:
            worklogs, absence_seconds = _split_absence(worklogs)
        agg = _aggregate(worklogs)

        # Pull the roster so members with zero logged time still show up, and to
        # label opaque ``JIRAUSER…`` worker keys with readable names.
        roster = _fetch_team_members(team_id)
        if only_active:
            roster = {k: v for k, v in roster.items() if v["active"]}
        for key in roster:
            agg.by_worker.setdefault(key, 0)

        # Drop workers who logged time but are no longer on the team, rather than
        # letting them silently vanish: report what was excluded.
        excluded: dict[str, int] = {}
        if only_active:
            for worker in [w for w in agg.by_worker if w not in roster]:
                excluded[worker] = agg.by_worker.pop(worker)

        # Roster names win over anything Tempo put on the worklogs.
        display = {
            **agg.worker_display,
            **{k: v["display"] for k, v in roster.items() if v["display"]},
        }
        if "account" in dims:
            agg.account_name = {**_fetch_account_names(), **agg.account_name}

        result: dict[str, Any] = {
            "team_id": team_id,
            "date_from": date_from,
            "date_to": date_to,
            "include_subtasks": include_subtasks,
            "only_active": only_active,
            "exclude_absence": exclude_absence,
            "excluded_absence_hours": _seconds_to_hours(absence_seconds),
            "member_count": len(roster),
            "total_hours": _seconds_to_hours(agg.total_seconds),
            "total_seconds": agg.total_seconds,
            "worklog_count": len(worklogs),
            "by_user": _render_users(agg, dims, display),
        }
        if "account" in dims:
            # Team-wide account totals, alongside the per-user nesting: this is
            # the "how much went to absence / to each client" view.
            result["by_account"] = _account_map(agg.by_account, agg.account_name)
        if excluded:
            # Hours here ARE included in total_hours but not in by_user.
            result["excluded_inactive"] = {
                "count": len(excluded),
                "hours": _seconds_to_hours(sum(excluded.values())),
                "workers": sorted(excluded, key=lambda w: excluded[w], reverse=True),
            }
        return result

    @mcp.tool()
    def add_worklog(
        issue_key: str,
        time_spent: str,
        started: str | None = None,
        comment: str | None = None,
    ) -> dict[str, Any]:
        """Log time on a Jira issue, as the user who owns the API token.

        CALL THIS TOOL STRICTLY ON THE USER'S EXPLICIT REQUEST. It is the only
        tool here that writes: it creates a real worklog under the user's name
        and cannot be undone from this server. Never call it on your own
        initiative — not after finishing a task, not to "correct" hours seen in
        a report, not as a step inferred from some other request. If the user
        has not asked to log time, ask them first.

        Args:
            issue_key: The issue to log against, e.g. ``PROJ-123``.
            time_spent: Duration in Jira syntax — ``2h``, ``90m``, ``2h 30m``,
                ``1d 4h`` (w/d/h/m). Jira converts it using the instance's
                working day/week, so ``1d`` is usually 8h, not 24h.
            started: When the work happened. ``YYYY-MM-DD`` (anchored at 09:00
                local time), ``YYYY-MM-DDTHH:MM``, or a timestamp with an
                offset. Defaults to now — pass the date explicitly when logging
                for a past day.
            comment: Optional worklog comment (plain text / Jira wiki markup).

        Returns the created worklog: ``id``, ``time_spent``, ``started``,
        ``author`` and a browse link to the issue.
        """
        payload: dict[str, Any] = {
            "timeSpent": _validate_time_spent(time_spent),
            "started": _parse_started(started),
        }
        if comment:
            payload["comment"] = comment

        jira = get_jira()
        try:
            resp = jira.issue_add_json_worklog(issue_key, payload)
        except HTTPError as exc:
            response = exc.response
            status = response.status_code if response is not None else "?"
            body = response.text.strip() if response is not None else str(exc)
            raise RuntimeError(
                f"Logging work failed (HTTP {status}) at "
                f"'rest/api/2/issue/{issue_key}/worklog'. "
                f"Payload: {payload}. Server response: {body[:800]}"
            ) from exc

        resp = resp if isinstance(resp, dict) else {}
        author = resp.get("author") if isinstance(resp.get("author"), dict) else {}
        seconds = int(resp.get("timeSpentSeconds") or 0)
        return {
            "id": resp.get("id"),
            "issue_key": issue_key,
            "time_spent": resp.get("timeSpent") or payload["timeSpent"],
            "time_spent_seconds": seconds,
            "hours": _seconds_to_hours(seconds),
            "started": resp.get("started") or payload["started"],
            "author": author.get("displayName") or author.get("name"),
            "comment": resp.get("comment"),
            "url": f"{jira.url.rstrip('/')}/browse/{issue_key}",
        }
