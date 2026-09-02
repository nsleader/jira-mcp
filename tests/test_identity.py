"""Tests for login ↔ user-key resolution.

Offline: the Jira client is replaced by a stub that answers the way
``rest/api/2/user`` does — by login, by key, and 404 for everything else.

The case worth protecting is the silent one. Both Tempo APIs answer a wrong
handle with an empty result rather than an error, so a regression here does not
break a report, it makes one lie.
"""

from __future__ import annotations

import pytest
from requests import HTTPError

from jira_mcp import identity

# Two accounts of the same instance: one minted before Jira 6.1 (key == login),
# one after (key unrelated to the login). Only the second one ever broke.
_USERS = {
    "JIRAUSER18613": {
        "key": "JIRAUSER18613",
        "name": "acutina",
        "displayName": "Кутина Анастасия",
    },
    "veteran": {"key": "veteran", "name": "veteran", "displayName": "Старожил"},
}
_BY_LOGIN = {u["name"]: u for u in _USERS.values()}


class _Jira:
    """Minimal stand-in for the Jira client, counting requests."""

    def __init__(self) -> None:
        self.calls: list[dict[str, str]] = []

    def user(self, **params: str) -> dict[str, str]:
        self.calls.append(params)
        if "key" in params:
            found = _USERS.get(params["key"])
        else:
            found = _BY_LOGIN.get(params["username"])
        if found is None:
            raise HTTPError("404 Not Found")
        return found


@pytest.fixture(autouse=True)
def _stub_jira(monkeypatch: pytest.MonkeyPatch) -> _Jira:
    identity.resolve.cache_clear()
    jira = _Jira()
    monkeypatch.setattr(identity, "get_jira", lambda: jira)
    return jira


def test_login_resolves_to_the_key_tempo_timesheets_matches() -> None:
    """The reported bug: hours asked for by login came back as zero."""
    assert identity.worker_key("acutina") == "JIRAUSER18613"


def test_key_resolves_to_the_login_tempo_planner_matches() -> None:
    assert identity.planner_login("JIRAUSER18613") == "acutina"


def test_both_handles_survive_the_round_trip() -> None:
    assert identity.worker_key("JIRAUSER18613") == "JIRAUSER18613"
    assert identity.planner_login("acutina") == "acutina"


def test_an_old_account_where_key_equals_login_resolves_either_way() -> None:
    assert identity.worker_key("veteran") == "veteran"
    assert identity.planner_login("veteran") == "veteran"


def test_display_name_comes_from_jira() -> None:
    assert identity.display_name("acutina") == "Кутина Анастасия"


def test_an_unknown_handle_is_passed_through_untouched(_stub_jira: _Jira) -> None:
    """Unresolvable input must not raise: Tempo answers what it always would."""
    assert identity.worker_key("nobody") == "nobody"
    assert identity.planner_login("nobody") == "nobody"
    assert identity.display_name("nobody") is None


def test_a_key_is_probed_by_key_first(_stub_jira: _Jira) -> None:
    """A JIRAUSER… string costs one request, not a miss and then a hit."""
    identity.worker_key("JIRAUSER18613")
    assert _stub_jira.calls == [{"key": "JIRAUSER18613"}]


def test_a_login_is_probed_by_login_first(_stub_jira: _Jira) -> None:
    identity.worker_key("acutina")
    assert _stub_jira.calls == [{"username": "acutina"}]


def test_resolution_is_cached(_stub_jira: _Jira) -> None:
    """A team report resolves each member once, not once per lookup."""
    identity.worker_key("acutina")
    identity.planner_login("acutina")
    identity.display_name("acutina")
    assert len(_stub_jira.calls) == 1


def test_an_unknown_handle_is_called_out_instead_of_reading_as_zero() -> None:
    """A display name resolves to nobody; the report must say so, not answer 0."""
    warning = identity.unknown_users_warning(["acutina", "Кутина Анастасия"])
    assert warning is not None
    assert "Кутина Анастасия" in warning
    assert "acutina" not in warning.split("—")[0]


def test_no_warning_when_everyone_resolves() -> None:
    assert identity.unknown_users_warning(["acutina", "JIRAUSER18613", "veteran"]) is None
