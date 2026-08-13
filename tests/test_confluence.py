"""Tests for the pure helpers behind the Confluence tools.

Everything here is offline: the Confluence client is only constructed to read
its base URL back (``atlassian-python-api`` does no I/O at construction time).
"""

from __future__ import annotations

import pytest

from jira_mcp import client, config
from jira_mcp.tools import confluence


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the (lazily built, cached) clients at a fake instance."""
    monkeypatch.setenv("JIRA_URL", "https://jira.example.com")
    monkeypatch.setenv("JIRA_PERSONAL_TOKEN", "jira-token")
    monkeypatch.setenv("CONFLUENCE_URL", "https://wiki.example.com/")
    monkeypatch.delenv("CONFLUENCE_PERSONAL_TOKEN", raising=False)
    for cached in (config.get_settings, config.get_confluence_settings, client.get_confluence):
        cached.cache_clear()


def test_confluence_token_falls_back_to_the_jira_one() -> None:
    url, token, ssl_verify, timeout = config.resolved_confluence_settings()
    assert (url, token, ssl_verify, timeout) == (
        "https://wiki.example.com/",
        "jira-token",
        True,
        75,
    )


def test_missing_confluence_url_is_a_readable_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CONFLUENCE_URL", raising=False)
    config.get_confluence_settings.cache_clear()
    with pytest.raises(RuntimeError, match="CONFLUENCE_URL"):
        config.resolved_confluence_settings()


def test_build_cql_combines_the_filters() -> None:
    assert (
        confluence._build_cql("release notes", "ENG", None, "page", False)
        == 'type = "page" AND space = "ENG" AND text ~ "release notes"'
    )


def test_build_cql_escapes_quotes_and_orders_on_request() -> None:
    assert (
        confluence._build_cql('say "hi"', None, "howto", None, True)
        == 'label = "howto" AND text ~ "say \\"hi\\"" ORDER BY lastmodified DESC'
    )


def test_build_cql_rejects_an_empty_search() -> None:
    with pytest.raises(ValueError, match="at least one"):
        confluence._build_cql(None, None, None, None, False)


@pytest.mark.parametrize(
    ("reference", "expected"),
    [
        ("98765", ("98765", None)),
        ("https://wiki.example.com/pages/viewpage.action?pageId=42", ("42", None)),
        ("https://wiki.example.com/spaces/ENG/pages/7/Some+Title", ("7", None)),
        (
            "https://wiki.example.com/display/ENG/Release+Checklist",
            (None, ("ENG", "Release Checklist")),
        ),
        (
            "https://wiki.example.com/display/ENG/%D0%A0%D0%B5%D0%BB%D0%B8%D0%B7",
            (None, ("ENG", "Релиз")),
        ),
    ],
)
def test_parse_page_reference(reference: str, expected: tuple) -> None:
    assert confluence._parse_page_reference(reference) == expected


def test_parse_page_reference_rejects_gibberish() -> None:
    with pytest.raises(ValueError, match="Cannot tell which page"):
        confluence._parse_page_reference("Release Checklist")


def test_to_markdown_keeps_structure_and_absolutises_links() -> None:
    html = (
        "<h2>Steps</h2><p>See <a href='/display/ENG/Other'>other</a>.</p>"
        "<table><tr><th>A</th></tr><tr><td>1</td></tr></table>"
        "<ul><li>one</li></ul>"
    )
    markdown = confluence._to_markdown(html)
    assert "## Steps" in markdown
    assert "[other](https://wiki.example.com/display/ENG/Other)" in markdown
    assert "| A |" in markdown
    assert "* one" in markdown


def test_render_falls_back_to_storage_when_the_view_body_is_absent() -> None:
    page = {"body": {"storage": {"value": "<p>plain</p>"}}}
    assert confluence._render(page, "markdown") == "plain"


def test_search_hit_strips_highlight_markers() -> None:
    hit = {
        "title": "@@@hl@@@Release@@@endhl@@@ checklist",
        "excerpt": "before @@@hl@@@release@@@endhl@@@ after",
        "url": "/display/ENG/Release+Checklist",
        "lastModified": "2026-08-01T10:00:00.000+03:00",
        "content": {"id": "42", "type": "page", "space": {"key": "ENG", "name": "Engineering"}},
    }
    assert confluence._search_hit(hit) == {
        "id": "42",
        "title": "Release checklist",
        "type": "page",
        "space_key": "ENG",
        "space_name": "Engineering",
        "url": "https://wiki.example.com/display/ENG/Release+Checklist",
        "excerpt": "before release after",
        "last_modified": "2026-08-01T10:00:00.000+03:00",
    }


def test_page_meta_flattens_version_and_history() -> None:
    page = {
        "id": "42",
        "title": "Release checklist",
        "type": "page",
        "space": {"key": "ENG", "name": "Engineering"},
        "version": {
            "number": 7,
            "when": "2026-08-01T10:00:00.000+03:00",
            "by": {"displayName": "Ann"},
        },
        "history": {
            "createdDate": "2025-01-01T09:00:00.000+03:00",
            "createdBy": {"username": "bob"},
        },
        "_links": {"webui": "/display/ENG/Release+Checklist"},
    }
    assert confluence._page_meta(page) == {
        "id": "42",
        "title": "Release checklist",
        "type": "page",
        "space_key": "ENG",
        "space_name": "Engineering",
        "url": "https://wiki.example.com/display/ENG/Release+Checklist",
        "version": 7,
        "updated": "2026-08-01T10:00:00.000+03:00",
        "updated_by": "Ann",
        "created": "2025-01-01T09:00:00.000+03:00",
        "created_by": "bob",
    }
