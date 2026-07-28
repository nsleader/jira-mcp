"""Jira issue tools: reading, commenting and creating issues.

These illustrate the pattern for all Jira tools: typed arguments, a docstring
that the LLM sees as the tool description, structured return values, and Jira
errors surfaced as readable messages instead of raw tracebacks.

``create_issue`` is the write here. On Server/DC the agile hierarchy does not
live in plain fields: an Epic needs its "Epic Name" custom field, and a task is
attached to an epic through the "Epic Link" custom field. Both ids differ per
instance (``customfield_10105`` on one Jira, ``customfield_12001`` on the next),
so they are discovered once from ``/field`` by their Greenhopper schema type.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

from mcp.server.fastmcp import FastMCP
from requests import HTTPError

from ..client import get_jira

# Stable Greenhopper schema ids of the two agile custom fields. The field *name*
# is localised and renameable; the schema type is not, so match on it first.
_EPIC_NAME_SCHEMA = "com.pyxis.greenhopper.jira:gh-epic-label"
_EPIC_LINK_SCHEMA = "com.pyxis.greenhopper.jira:gh-epic-link"


@lru_cache(maxsize=1)
def _agile_field_ids() -> dict[str, str]:
    """Return ``{"epic_name": id, "epic_link": id}`` for this Jira instance.

    Cached: the field layout does not change between calls of a session, and
    ``/field`` returns every field on the instance (hundreds of them).
    """
    try:
        fields = get_jira().get_all_fields() or []
    except HTTPError as exc:
        raise _http_error("Field lookup", "rest/api/2/field", exc, {}) from exc

    ids: dict[str, str] = {}
    for field in fields if isinstance(fields, list) else []:
        if not isinstance(field, dict) or not field.get("id"):
            continue
        schema = field.get("schema") if isinstance(field.get("schema"), dict) else {}
        custom = schema.get("custom")
        name = str(field.get("name") or "").strip().lower()
        if custom == _EPIC_NAME_SCHEMA or (custom is None and name == "epic name"):
            ids.setdefault("epic_name", field["id"])
        elif custom == _EPIC_LINK_SCHEMA or (custom is None and name == "epic link"):
            ids.setdefault("epic_link", field["id"])
    return ids


@lru_cache(maxsize=1)
def _issue_types() -> list[dict[str, Any]]:
    """All issue types on the instance (name, id, ``subtask`` flag), cached."""
    try:
        types = get_jira().get_issue_types() or []
    except HTTPError as exc:
        raise _http_error("Issue type lookup", "rest/api/2/issuetype", exc, {}) from exc
    return [t for t in types if isinstance(t, dict)] if isinstance(types, list) else []


def _resolve_issue_type(issue_type: str) -> dict[str, Any]:
    """Find the issue type by name (case-insensitive) or id.

    Resolving up front turns a Jira 400 ("issue type is required") into a
    message that lists what the instance actually offers.
    """
    wanted = issue_type.strip().lower()
    for candidate in _issue_types():
        if str(candidate.get("name", "")).strip().lower() == wanted:
            return candidate
        if str(candidate.get("id", "")) == wanted:
            return candidate
    names = sorted({str(t.get("name")) for t in _issue_types() if t.get("name")})
    raise ValueError(
        f"issue_type '{issue_type}' does not exist on this Jira. "
        f"Available: {', '.join(names) or '(none returned)'}"
    )


def _is_epic(issue_type: dict[str, Any]) -> bool:
    """Whether the resolved issue type is the agile Epic type."""
    return str(issue_type.get("name", "")).strip().lower() == "epic"


def _http_error(
    what: str, endpoint: str, exc: HTTPError, payload: dict[str, Any]
) -> RuntimeError:
    """Wrap an HTTPError with Jira's own error body, which explains the 400."""
    response = exc.response
    status = response.status_code if response is not None else "?"
    body = response.text.strip() if response is not None else str(exc)
    detail = f" Payload: {payload}." if payload else ""
    return RuntimeError(
        f"{what} failed (HTTP {status}) at '{endpoint}'.{detail} "
        f"Server response: {body[:800]}"
    )


def _validate_summary(summary: str) -> str:
    """Jira summaries are single-line; a newline is rejected server-side."""
    value = summary.strip()
    if not value:
        raise ValueError("summary must not be empty")
    if "\n" in value or "\r" in value:
        raise ValueError(
            "summary must be a single line — put the details in `description` instead"
        )
    return value


def _build_fields(
    project_key: str,
    summary: str,
    issue_type: dict[str, Any],
    description: str | None,
    parent: str | None,
    epic_name: str | None,
    assignee: str | None,
    priority: str | None,
    labels: list[str] | None,
    components: list[str] | None,
    due_date: str | None,
    extra_fields: dict[str, Any] | None,
) -> dict[str, Any]:
    """Assemble the ``fields`` object for ``POST rest/api/2/issue``."""
    fields: dict[str, Any] = {
        "project": {"key": project_key.strip().upper()},
        "summary": summary,
        "issuetype": {"name": issue_type["name"]},
    }
    if description:
        fields["description"] = description
    if assignee:
        # Server/DC identifies users by username, not accountId.
        fields["assignee"] = {"name": assignee}
    if priority:
        fields["priority"] = {"name": priority}
    if labels:
        fields["labels"] = labels
    if components:
        fields["components"] = [{"name": c} for c in components]
    if due_date:
        fields["duedate"] = due_date

    agile = _agile_field_ids()

    if _is_epic(issue_type):
        # "Epic Name" is mandatory for epics on Server/DC — default it to the
        # summary so callers do not have to know the field exists.
        field_id = agile.get("epic_name")
        if not field_id:
            raise RuntimeError(
                "This Jira has no 'Epic Name' field (Jira Software / Greenhopper "
                "may not be installed). Pass the field explicitly via extra_fields, "
                "e.g. {'customfield_10105': 'My epic'}."
            )
        fields[field_id] = (epic_name or summary).strip()
    elif epic_name:
        raise ValueError("epic_name only applies when issue_type is 'Epic'")

    if parent:
        parent_key = parent.strip().upper()
        if issue_type.get("subtask"):
            fields["parent"] = {"key": parent_key}
        else:
            # A non-subtask's "parent" is its epic, and that is the Epic Link.
            field_id = agile.get("epic_link")
            if not field_id:
                raise RuntimeError(
                    f"Cannot attach {issue_type['name']} to '{parent_key}': this Jira "
                    f"has no 'Epic Link' field. Only sub-tasks can have a parent here."
                )
            fields[field_id] = parent_key
    elif issue_type.get("subtask"):
        raise ValueError(
            f"issue_type '{issue_type['name']}' is a sub-task — pass `parent` with "
            f"the key of the issue it belongs to"
        )

    if extra_fields:
        fields.update(extra_fields)
    return fields


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

    @mcp.tool()
    def get_issue_types(project_key: str | None = None) -> list[dict[str, Any]]:
        """List the issue types available for ``create_issue``.

        Call this first when unsure what ``issue_type`` a project accepts —
        instances rename and translate types (``Task``/``Задача``, ``Story``,
        ``Sub-task``), so guessing produces a 400.

        Args:
            project_key: Optional project key to restrict to the types actually
                enabled in that project's scheme. Without it, every type on the
                instance is returned.
        """
        if not project_key:
            return [
                {
                    "id": t.get("id"),
                    "name": t.get("name"),
                    "subtask": bool(t.get("subtask")),
                    "description": t.get("description"),
                }
                for t in _issue_types()
            ]

        project = project_key.strip().upper()
        try:
            meta = get_jira().issue_createmeta(project, expand="projects.issuetypes")
        except HTTPError as exc:
            endpoint = f"rest/api/2/issue/createmeta?projectKeys={project}"
            raise _http_error("Create-metadata lookup", endpoint, exc, {}) from exc

        projects = (meta or {}).get("projects") or []
        types = projects[0].get("issuetypes", []) if projects else []
        return [
            {
                "id": t.get("id"),
                "name": t.get("name"),
                "subtask": bool(t.get("subtask")),
                "description": t.get("description"),
            }
            for t in types
            if isinstance(t, dict)
        ]

    @mcp.tool()
    def create_issue(
        project_key: str,
        summary: str,
        issue_type: str = "Task",
        description: str | None = None,
        parent: str | None = None,
        epic_name: str | None = None,
        assignee: str | None = None,
        priority: str | None = None,
        labels: list[str] | None = None,
        components: list[str] | None = None,
        due_date: str | None = None,
        extra_fields: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Create a Jira issue — epic, task, story or sub-task.

        CALL THIS TOOL STRICTLY ON THE USER'S EXPLICIT REQUEST. It writes a real
        issue into the project under the token owner's name and this server
        cannot delete it afterwards. Never create issues on your own initiative
        (not to "track" work you are doing, not as an inferred step of another
        request). When several issues are described at once, confirm the list
        with the user before creating them.

        Args:
            project_key: Target project key, e.g. ``PROJ``.
            summary: Single-line title of the issue.
            issue_type: Type name as it appears in Jira — ``Task`` (default),
                ``Epic``, ``Story``, ``Bug``, ``Sub-task``… Call
                ``get_issue_types`` when unsure; names are instance-specific and
                may be localised.
            description: Body text (plain text / Jira wiki markup).
            parent: Key of the parent issue. For a sub-task this is its parent
                task (required). For any other type this is the **epic** it
                belongs to — it is written to the Epic Link field.
            epic_name: Short board label for an epic. Epics only; defaults to
                ``summary`` when omitted.
            assignee: Username (Server/DC login) to assign the issue to.
            priority: Priority name, e.g. ``Major``.
            labels: Labels to set.
            components: Component names to set.
            due_date: Due date, ``YYYY-MM-DD``.
            extra_fields: Raw field overrides merged in last, for anything this
                signature does not cover — e.g. story points or a required
                custom field: ``{"customfield_10004": 3}``.

        Returns the created issue: ``key``, ``id``, ``url`` and the resolved
        type/parent, so the key can be reused directly (e.g. as ``parent`` for
        the epic's children).
        """
        summary = _validate_summary(summary)
        resolved_type = _resolve_issue_type(issue_type)
        fields = _build_fields(
            project_key=project_key,
            summary=summary,
            issue_type=resolved_type,
            description=description,
            parent=parent,
            epic_name=epic_name,
            assignee=assignee,
            priority=priority,
            labels=labels,
            components=components,
            due_date=due_date,
            extra_fields=extra_fields,
        )

        jira = get_jira()
        try:
            resp = jira.issue_create(fields)
        except HTTPError as exc:
            raise _http_error("Issue creation", "rest/api/2/issue", exc, fields) from exc

        resp = resp if isinstance(resp, dict) else {}
        key = resp.get("key")
        return {
            "key": key,
            "id": resp.get("id"),
            "project": fields["project"]["key"],
            "issue_type": resolved_type["name"],
            "summary": summary,
            "parent": parent,
            "url": f"{jira.url.rstrip('/')}/browse/{key}" if key else None,
        }
