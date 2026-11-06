"""Jira issue tools: reading, commenting, creating and transitioning issues.

These illustrate the pattern for all Jira tools: typed arguments, a docstring
that the LLM sees as the tool description, structured return values, and Jira
errors surfaced as readable messages instead of raw tracebacks.

``create_issue`` is a write here. On Server/DC the agile hierarchy does not
live in plain fields: an Epic needs its "Epic Name" custom field, and a task is
attached to an epic through the "Epic Link" custom field. Both ids differ per
instance (``customfield_10105`` on one Jira, ``customfield_12001`` on the next),
so they are discovered once from ``/field`` by their Greenhopper schema type.

``transition_issue`` is the other write. A status cannot be assigned directly on
Server/DC — it is the *result* of a workflow transition, and which transitions
exist depends on the issue's current status, its workflow and the token owner's
permissions. So the available transitions are always read first, which is also
what turns "no such status" into a message listing the reachable ones.

``assign_issue`` changes the assignee through the dedicated
``PUT /issue/{key}/assignee`` endpoint, which needs only the "Assign Issues"
permission — an edit of the ``assignee`` field would also need "Edit Issues"
and the field on the edit screen. Server/DC identifies the person by *login*
(``{"name": ...}``), so a user key is resolved to a login first; an unknown
handle is refused up front rather than passed on for a bare HTTP 400.
"""

from __future__ import annotations

from collections.abc import Mapping
from functools import lru_cache
from typing import Any

from mcp.server.fastmcp import FastMCP
from requests import HTTPError

from .. import identity
from ..client import get_jira

# Stable Greenhopper schema ids of the two agile custom fields. The field *name*
# is localised and renameable; the schema type is not, so match on it first.
_EPIC_NAME_SCHEMA = "com.pyxis.greenhopper.jira:gh-epic-label"
_EPIC_LINK_SCHEMA = "com.pyxis.greenhopper.jira:gh-epic-link"


@lru_cache(maxsize=1)
def _all_fields() -> list[dict[str, Any]]:
    """Every field defined on the instance, cached.

    ``/field`` returns hundreds of entries and the layout does not change
    between calls of a session, so both readers below share one request.
    """
    try:
        fields = get_jira().get_all_fields() or []
    except HTTPError as exc:
        raise _http_error("Field lookup", "rest/api/2/field", exc, {}) from exc
    return [f for f in fields if isinstance(f, dict)] if isinstance(fields, list) else []


def _agile_field_ids() -> dict[str, str]:
    """Return ``{"epic_name": id, "epic_link": id}`` for this Jira instance."""
    ids: dict[str, str] = {}
    for field in _all_fields():
        if not field.get("id"):
            continue
        schema = field.get("schema") if isinstance(field.get("schema"), dict) else {}
        custom = schema.get("custom")
        name = str(field.get("name") or "").strip().lower()
        if custom == _EPIC_NAME_SCHEMA or (custom is None and name == "epic name"):
            ids.setdefault("epic_name", field["id"])
        elif custom == _EPIC_LINK_SCHEMA or (custom is None and name == "epic link"):
            ids.setdefault("epic_link", field["id"])
    return ids


def _custom_field_names() -> dict[str, str]:
    """``{"customfield_12001": "Описание пресейла"}`` for this instance."""
    return {
        field["id"]: str(field.get("name")).strip()
        for field in _all_fields()
        if str(field.get("id", "")).startswith("customfield_") and field.get("name")
    }


def _name_custom_fields(fields: Any, names: Mapping[str, str]) -> Any:
    """Give ``customfield_*`` keys their human names and drop the empty ones.

    Jira returns every custom field defined on the instance, so one issue
    carries a hundred ``customfield_11704: null`` entries and the two that are
    actually filled in look exactly like them. A model reading that sees an
    empty ``description`` and reports the issue has none — while the text sits
    in something like "Описание пресейла", which on many instances is where
    descriptions really live.

    The id stays inside the key: writes address fields by id, not by name
    (``extra_fields`` of ``create_issue``, transition screens).
    """
    if not isinstance(fields, dict):
        return fields
    named: dict[str, Any] = {}
    for key, value in fields.items():
        if not key.startswith("customfield_"):
            named[key] = value
            continue
        if value is None or value == "" or value == [] or value == {}:
            continue
        name = names.get(key)
        named[f"{name} ({key})" if name else key] = value
    return named


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


def _parse_transitions(payload: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Flatten ``GET /issue/{key}/transitions`` into id / name / target triples.

    ``required_fields`` is only populated when the request expanded
    ``transitions.fields``. It matters because a transition behind a screen
    ("Resolve Issue" asking for a resolution) rejects the POST with a 400 unless
    that field comes along, and the field ids are instance-specific.
    """
    raw = (payload or {}).get("transitions")
    transitions: list[dict[str, Any]] = []
    for transition in raw if isinstance(raw, list) else []:
        if not isinstance(transition, dict) or not transition.get("id"):
            continue
        to = transition.get("to") if isinstance(transition.get("to"), dict) else {}
        category = to.get("statusCategory") if isinstance(to.get("statusCategory"), dict) else {}
        fields = transition.get("fields") if isinstance(transition.get("fields"), dict) else {}
        transitions.append(
            {
                "id": str(transition["id"]),
                "name": transition.get("name"),
                "to_status": to.get("name"),
                "to_status_category": category.get("name"),
                "required_fields": sorted(
                    field_id
                    for field_id, spec in fields.items()
                    if isinstance(spec, dict) and spec.get("required")
                ),
            }
        )
    return transitions


def _match_transition(
    transitions: list[dict[str, Any]], target: str
) -> dict[str, Any] | None:
    """Find the transition meant by ``target``, or ``None``.

    Callers name a status ("Ready For Test"), a transition ("Start Progress") or
    an id, and the three namespaces overlap — a transition called "Done" may lead
    to a status called "Closed". Target status wins, because that is what a
    request to "change the status" is about; the transition name is the fallback
    for workflows whose buttons are named differently from their outcomes.
    """
    wanted = target.strip().lower()
    if not wanted:
        raise ValueError("target must not be empty")
    for key in ("to_status", "name", "id"):
        for transition in transitions:
            if str(transition.get(key) or "").strip().lower() == wanted:
                return transition
    return None


def _describe_transitions(transitions: list[dict[str, Any]]) -> str:
    """Render the reachable statuses for an error message."""
    return (
        ", ".join(
            f"'{t['to_status']}' (transition '{t['name']}', id {t['id']})" for t in transitions
        )
        or "(none — the workflow offers this user no transition from here)"
    )


def _transition_payload(
    transition_id: str, fields: dict[str, Any] | None, comment: str | None
) -> dict[str, Any]:
    """Build the body of ``POST /issue/{key}/transitions``.

    A comment goes into ``update``, not ``fields``: on a transition screen the
    comment is an *add* operation on the comment list, and passing it as a field
    is rejected.
    """
    payload: dict[str, Any] = {"transition": {"id": transition_id}}
    if fields:
        payload["fields"] = fields
    if comment:
        payload["update"] = {"comment": [{"add": {"body": comment}}]}
    return payload


def _current_status(issue_key: str) -> str | None:
    """Return the issue's status name, or ``None`` if Jira did not report one."""
    try:
        issue = get_jira().issue(issue_key, fields="status")
    except HTTPError as exc:
        raise _http_error("Status lookup", f"rest/api/2/issue/{issue_key}", exc, {}) from exc
    fields = issue.get("fields") if isinstance(issue, dict) else None
    status = fields.get("status") if isinstance(fields, dict) else None
    return status.get("name") if isinstance(status, dict) else None


def _read_transitions(issue_key: str) -> list[dict[str, Any]]:
    """Read the transitions available to the token owner from the current status."""
    try:
        payload = get_jira().get_issue_transitions_full(
            issue_key, expand="transitions.fields"
        )
    except HTTPError as exc:
        endpoint = f"rest/api/2/issue/{issue_key}/transitions"
        raise _http_error("Transition lookup", endpoint, exc, {}) from exc
    return _parse_transitions(payload if isinstance(payload, dict) else {})


def _assignee_payload(login: str | None) -> dict[str, Any]:
    """Body of ``PUT /issue/{key}/assignee``: ``name: null`` unassigns."""
    return {"name": login}


def _resolve_assignee(user: str | None) -> identity.Identity | None:
    """Turn a login or user key into the account to assign; ``None`` = unassign.

    Raises for a handle Jira knows no account for — most often a display name
    ("Кутина Анастасия") passed where a login belongs.
    """
    value = (user or "").strip()
    if not value:
        return None
    found = identity.resolve(value)
    if found is None:
        raise ValueError(
            f"no Jira account matches '{value}'. assign_issue takes a login "
            "('acutina') or a user key ('JIRAUSER18613'); find the person with "
            "search_users first."
        )
    return found


def _current_assignee(issue_key: str) -> dict[str, Any] | None:
    """Return ``{login, display}`` of the current assignee, ``None`` if unassigned."""
    try:
        issue = get_jira().issue(issue_key, fields="assignee")
    except HTTPError as exc:
        raise _http_error("Assignee lookup", f"rest/api/2/issue/{issue_key}", exc, {}) from exc
    fields = issue.get("fields") if isinstance(issue, dict) else None
    assignee = fields.get("assignee") if isinstance(fields, dict) else None
    if not isinstance(assignee, dict):
        return None
    return {
        "login": assignee.get("name"),
        "display": assignee.get("displayName"),
    }


def register(mcp: FastMCP) -> None:
    """Attach issue tools to the FastMCP instance."""

    @mcp.tool()
    def get_issue(issue_key: str, fields: str | None = None) -> dict[str, Any]:
        """Get a single Jira issue by key (e.g. ``PROJ-123``).

        Custom fields come back under their human names with the id kept in
        the key ("Описание пресейла (customfield_12001)"), and the empty ones
        are dropped. Do read them: instances put real content — descriptions
        above all — into custom fields, and as bare ids they are
        indistinguishable from the hundred nulls around them.

        Args:
            issue_key: The issue key, e.g. ``PROJ-123``.
            fields: Optional comma-separated list of fields to return
                (e.g. ``summary,status,assignee``). Defaults to all fields.
        """
        issue = get_jira().issue(issue_key, fields=fields)
        if isinstance(issue, dict) and "fields" in issue:
            names = _custom_field_names()
            issue["fields"] = _name_custom_fields(issue["fields"], names)
        return issue

    @mcp.tool()
    def search_issues(jql: str, limit: int = 50) -> dict[str, Any]:
        """Search issues with a JQL query.

        Custom fields are named and stripped of empties the same way as in
        ``get_issue`` — over a page of results that is most of the payload.

        Args:
            jql: A JQL query, e.g. ``project = PROJ AND status = "In Progress"``.
            limit: Maximum number of issues to return (default 50).
        """
        result = get_jira().jql(jql, limit=limit)
        if isinstance(result, dict) and isinstance(result.get("issues"), list):
            names = _custom_field_names()
            for issue in result["issues"]:
                if isinstance(issue, dict) and "fields" in issue:
                    issue["fields"] = _name_custom_fields(issue["fields"], names)
        return result

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
    def get_transitions(issue_key: str) -> dict[str, Any]:
        """List the statuses an issue can be moved to right now.

        Call this before ``transition_issue`` when unsure what the workflow
        allows: the set depends on the current status, the project's workflow and
        the token owner's permissions, and status names are instance-specific and
        often localised. ``required_fields`` on a transition lists field ids that
        must be passed to ``transition_issue`` (a resolution screen, typically).

        Args:
            issue_key: The issue key, e.g. ``PROJ-123``.
        """
        key = issue_key.strip().upper()
        return {
            "issue_key": key,
            "current_status": _current_status(key),
            "transitions": _read_transitions(key),
        }

    @mcp.tool()
    def transition_issue(
        issue_key: str,
        target: str,
        comment: str | None = None,
        resolution: str | None = None,
        fields: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Change an issue's status by running a workflow transition.

        CALL THIS TOOL STRICTLY ON THE USER'S EXPLICIT REQUEST. It changes a real
        issue under the token owner's name, the change is visible to the whole
        team (boards, notifications, automation), and this server cannot undo it —
        the reverse transition may not even exist in the workflow. Never move an
        issue on your own initiative: not because the work looks finished, not to
        "tidy up" a board, not as an inferred step of another request. When
        several issues are named at once, confirm the list with the user first.

        Args:
            issue_key: The issue key, e.g. ``PROJ-123``.
            target: Where to move it. Preferably the target status name
                (``Ready For Test``); a transition name (``Start Progress``) or a
                numeric transition id also work. Matched case-insensitively,
                target status first. Call ``get_transitions`` when unsure.
            comment: Optional comment to add as part of the transition.
            resolution: Resolution name (``Fixed``, ``Done``…) for transitions
                that close an issue and require one.
            fields: Raw field values for a transition screen that demands more,
                keyed by field id — e.g. ``{"customfield_10200": "1.4.0"}``. See
                ``required_fields`` from ``get_transitions``.

        Returns the status before and after the change, plus the transition used.
        A no-op (the issue is already in ``target``) returns ``changed: false``
        instead of failing.
        """
        key = issue_key.strip().upper()
        before = _current_status(key)
        if before and before.strip().lower() == target.strip().lower():
            return {
                "issue_key": key,
                "changed": False,
                "status": before,
                "detail": f"'{key}' is already in status '{before}' — nothing to do.",
            }

        transitions = _read_transitions(key)
        transition = _match_transition(transitions, target)
        if transition is None:
            raise ValueError(
                f"'{target}' is not reachable from status '{before}' on {key}. "
                f"Available: {_describe_transitions(transitions)}"
            )

        payload_fields = dict(fields) if fields else {}
        if resolution:
            payload_fields["resolution"] = {"name": resolution}
        payload = _transition_payload(transition["id"], payload_fields, comment)

        jira = get_jira()
        try:
            jira.post(f"{jira.resource_url('issue')}/{key}/transitions", data=payload)
        except HTTPError as exc:
            endpoint = f"rest/api/2/issue/{key}/transitions"
            raise _http_error("Transition", endpoint, exc, payload) from exc

        return {
            "issue_key": key,
            "changed": True,
            "from_status": before,
            "status": _current_status(key),
            "transition": {"id": transition["id"], "name": transition["name"]},
            "url": f"{jira.url.rstrip('/')}/browse/{key}",
        }

    @mcp.tool()
    def assign_issue(issue_key: str, assignee: str | None) -> dict[str, Any]:
        """Assign an existing issue to someone, reassign it, or unassign it.

        CALL THIS TOOL STRICTLY ON THE USER'S EXPLICIT REQUEST. It changes a real
        issue under the token owner's name and notifies the people involved.
        Never (re)assign an issue on your own initiative: not because someone
        seems to be working on it, not as an inferred step of another request.
        When several issues are named at once, confirm the list with the user
        first.

        Args:
            issue_key: The issue key, e.g. ``PROJ-123``.
            assignee: The new assignee's login (``acutina``) or user key
                (``JIRAUSER18613``). Not a display name — resolve one with
                ``search_users`` first. ``None`` or an empty string removes the
                assignee (only if the project allows unassigned issues).

        Returns the assignee before and after. Assigning the person who already
        holds the issue returns ``changed: false`` without a write.
        """
        key = issue_key.strip().upper()
        target = _resolve_assignee(assignee)
        before = _current_assignee(key)
        before_login = before.get("login") if before else None
        target_login = target.login if target else None

        jira = get_jira()
        url = f"{jira.url.rstrip('/')}/browse/{key}"
        after = (
            {"login": target.login, "display": target.display} if target else None
        )
        # Server/DC logins are case-insensitive.
        if (before_login or "").lower() == (target_login or "").lower():
            return {
                "issue_key": key,
                "changed": False,
                "assignee": before,
                "url": url,
            }

        payload = _assignee_payload(target_login)
        endpoint = f"rest/api/2/issue/{key}/assignee"
        try:
            jira.put(endpoint, data=payload)
        except HTTPError as exc:
            raise _http_error("Assignment", endpoint, exc, payload) from exc

        return {
            "issue_key": key,
            "changed": True,
            "from_assignee": before,
            "assignee": after,
            "url": url,
        }

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
