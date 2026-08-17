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
