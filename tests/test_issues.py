"""Tests for the pure helpers behind the Jira issue tools.

Offline: no client is constructed here, the helpers work on the shapes Jira's
REST API returns.
"""

from __future__ import annotations

import pytest

from jira_mcp.tools import issues

_PAYLOAD = {
    "transitions": [
        {
            "id": "11",
            "name": "Start Progress",
            "to": {"name": "In Progress", "statusCategory": {"name": "In Progress"}},
        },
        {
            "id": "41",
            "name": "Ready for test",
            "to": {"name": "Ready For Test", "statusCategory": {"name": "In Progress"}},
            "fields": {
                "resolution": {"required": True},
                "assignee": {"required": False},
            },
        },
    ]
}


def test_parse_transitions_flattens_target_and_required_fields() -> None:
    assert issues._parse_transitions(_PAYLOAD) == [
        {
            "id": "11",
            "name": "Start Progress",
            "to_status": "In Progress",
            "to_status_category": "In Progress",
            "required_fields": [],
        },
        {
            "id": "41",
            "name": "Ready for test",
            "to_status": "Ready For Test",
            "to_status_category": "In Progress",
            "required_fields": ["resolution"],
        },
    ]


def test_parse_transitions_survives_a_junk_response() -> None:
    assert issues._parse_transitions(None) == []
    assert issues._parse_transitions({"transitions": [{"name": "no id"}, "nonsense"]}) == []


def test_match_transition_prefers_the_target_status() -> None:
    """A workflow whose button and outcome disagree must resolve by outcome."""
    transitions = issues._parse_transitions(
        {
            "transitions": [
                {"id": "2", "name": "Ready For Test", "to": {"name": "Closed"}},
                {"id": "3", "name": "Send to QA", "to": {"name": "Ready For Test"}},
            ]
        }
    )
    assert issues._match_transition(transitions, "ready for test")["id"] == "3"


def test_match_transition_falls_back_to_name_then_id() -> None:
    transitions = issues._parse_transitions(_PAYLOAD)
    assert issues._match_transition(transitions, "Start Progress")["id"] == "11"
    assert issues._match_transition(transitions, "41")["to_status"] == "Ready For Test"


def test_match_transition_returns_none_for_an_unreachable_status() -> None:
    assert issues._match_transition(issues._parse_transitions(_PAYLOAD), "Closed") is None


def test_match_transition_rejects_an_empty_target() -> None:
    with pytest.raises(ValueError, match="must not be empty"):
        issues._match_transition(issues._parse_transitions(_PAYLOAD), "   ")


def test_describe_transitions_lists_status_and_transition() -> None:
    described = issues._describe_transitions(issues._parse_transitions(_PAYLOAD))
    assert described == (
        "'In Progress' (transition 'Start Progress', id 11), "
        "'Ready For Test' (transition 'Ready for test', id 41)"
    )
    assert "none" in issues._describe_transitions([])


def test_transition_payload_puts_the_comment_in_update() -> None:
    assert issues._transition_payload("41", {"resolution": {"name": "Fixed"}}, "done") == {
        "transition": {"id": "41"},
        "fields": {"resolution": {"name": "Fixed"}},
        "update": {"comment": [{"add": {"body": "done"}}]},
    }


def test_transition_payload_omits_empty_parts() -> None:
    assert issues._transition_payload("11", {}, None) == {"transition": {"id": "11"}}


_FIELD_NAMES = {
    "customfield_12001": "Описание пресейла",
    "customfield_11704": "Story Points",
}


def test_name_custom_fields_names_the_filled_ones_and_drops_the_rest() -> None:
    """Jira returns every custom field on the instance — почти все пустые."""
    assert issues._name_custom_fields(
        {
            "summary": "Ретраинг",
            "description": None,
            "customfield_12001": "Текст пресейла",
            "customfield_11704": None,
            "customfield_10000": [],
        },
        _FIELD_NAMES,
    ) == {
        "summary": "Ретраинг",
        "description": None,
        "Описание пресейла (customfield_12001)": "Текст пресейла",
    }


def test_name_custom_fields_keeps_an_unknown_field_under_its_id() -> None:
    # Поле завели между вызовами — лучше отдать его как есть, чем потерять.
    assert issues._name_custom_fields({"customfield_99999": "x"}, _FIELD_NAMES) == {
        "customfield_99999": "x"
    }


def test_name_custom_fields_keeps_falsy_but_real_values() -> None:
    # Ноль и False — это ответ поля, а не его отсутствие.
    assert issues._name_custom_fields(
        {"customfield_11704": 0, "customfield_12001": False}, _FIELD_NAMES
    ) == {"Story Points (customfield_11704)": 0, "Описание пресейла (customfield_12001)": False}


def test_name_custom_fields_survives_a_junk_response() -> None:
    assert issues._name_custom_fields(None, _FIELD_NAMES) is None
    assert issues._name_custom_fields("nonsense", _FIELD_NAMES) == "nonsense"


def test_assignee_payload_uses_the_login_and_null_to_unassign() -> None:
    assert issues._assignee_payload("acutina") == {"name": "acutina"}
    assert issues._assignee_payload(None) == {"name": None}


def test_resolve_assignee_turns_a_key_into_the_login(monkeypatch: pytest.MonkeyPatch) -> None:
    known = issues.identity.Identity(login="acutina", key="JIRAUSER18613", display="Кутина")
    monkeypatch.setattr(issues.identity, "resolve", lambda u: known)
    assert issues._resolve_assignee(" JIRAUSER18613 ") == known


def test_resolve_assignee_treats_empty_as_unassign() -> None:
    assert issues._resolve_assignee(None) is None
    assert issues._resolve_assignee("  ") is None


def test_resolve_assignee_refuses_an_unknown_handle(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(issues.identity, "resolve", lambda u: None)
    with pytest.raises(ValueError, match="search_users"):
        issues._resolve_assignee("Кутина Анастасия")
