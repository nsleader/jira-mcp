"""Confluence tools: finding pages and reading them.

Read-only counterpart to the Jira tools. Confluence Server/DC is a separate
application from Jira — its own base URL and (usually) its own Personal Access
Token — so these tools use their own client and stay silent about Jira config;
see ``config.resolved_confluence_settings``.

Two things make Confluence awkward for an LLM and are handled here:

- **Search is CQL**, not free text. ``search_confluence`` builds the usual
  ``type = page AND text ~ "…"`` query from plain arguments and leaves ``cql``
  as an escape hatch. Server returns match highlights wrapped in ``@@@hl@@@``
  markers, which are stripped.
- **Page bodies are XHTML**, either the storage format (littered with
  ``<ac:structured-macro>`` elements) or the rendered ``view`` HTML. Reading a
  page returns the ``view`` body converted to Markdown, which keeps headings,
  tables, lists and code blocks — the parts that carry the meaning — instead of
  dumping markup into the context. Long pages are truncated to ``max_chars``
  and can be paged through with ``offset``.
"""

from __future__ import annotations

import html
import re
from collections.abc import Iterator
from typing import Any
from urllib.parse import unquote, urlparse

from atlassian.errors import ApiError, ApiValueError
from bs4 import BeautifulSoup
from markdownify import markdownify
from mcp.server.fastmcp import FastMCP
from requests import HTTPError

from ..client import get_confluence

# Confluence Server wraps search hits in these markers inside `title`/`excerpt`.
_HIGHLIGHT_RE = re.compile(r"@@@(?:end)?hl@@@")
_BLANK_LINES_RE = re.compile(r"\n{3,}")

_FORMATS = ("markdown", "text", "html", "storage")

# Page size and hard stop for walking `rest/api/space`.
_SPACE_PAGE = 100
_SPACE_MAX = 2000

# `body.view` is the rendered page (macros resolved); `body.storage` is the raw
# XHTML source. `history` carries the author/creation date, `metadata.labels`
# the labels, `ancestors` the breadcrumb up to the space home page.
_PAGE_EXPAND = "version,space,ancestors,history,metadata.labels"


def _http_error(what: str, endpoint: str, exc: HTTPError) -> RuntimeError:
    """Wrap an HTTPError with Confluence's own error body."""
    response = exc.response
    status = response.status_code if response is not None else "?"
    body = response.text.strip() if response is not None else str(exc)
    return RuntimeError(
        f"Confluence {what} failed (HTTP {status}) at '{endpoint}'. "
        f"Server response: {body[:800]}"
    )


def _base_url() -> str:
    """Base URL of the Confluence instance, without a trailing slash."""
    return str(get_confluence().url).rstrip("/")


def _absolute(link: str | None) -> str | None:
    """Turn a Confluence-relative link (``/display/…``) into a full URL.

    Only site-relative paths are rewritten. Anything already addressable —
    another host, a ``mailto:``/``tel:`` link, a protocol-relative URL, an
    in-page anchor — is left alone; prefixing those produces dead links.
    """
    if not link:
        return None
    if urlparse(link).scheme or link.startswith(("//", "#")):
        return link
    return f"{_base_url()}/{link.lstrip('/')}"


def _clean_highlight(value: Any) -> str:
    """Clean a search hit's title or excerpt.

    Server marks the matched words with ``@@@hl@@@`` and returns the snippet
    HTML-escaped, so a quoted phrase arrives as ``&quot;…&quot;``.
    """
    return html.unescape(_HIGHLIGHT_RE.sub("", str(value or ""))).strip()


def _escape_cql(value: str) -> str:
    """Escape a value for use inside a double-quoted CQL string literal."""
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _build_cql(
    query: str | None,
    space: str | None,
    label: str | None,
    content_type: str | None,
    newest_first: bool,
) -> str:
    """Assemble a CQL query from the plain search arguments.

    ``text ~ "…"`` is the full-text operator (title, body and attachments);
    without an explicit order Confluence ranks by relevance, so ordering is
    only added when the caller asks for it.
    """
    clauses: list[str] = []
    if content_type:
        clauses.append(f'type = "{_escape_cql(content_type)}"')
    if space:
        clauses.append(f'space = "{_escape_cql(space.strip())}"')
    if label:
        clauses.append(f'label = "{_escape_cql(label.strip())}"')
    if query and query.strip():
        clauses.append(f'text ~ "{_escape_cql(query.strip())}"')
    if not clauses:
        raise ValueError("pass at least one of query / space / label, or a raw cql")
    cql = " AND ".join(clauses)
    return f"{cql} ORDER BY lastmodified DESC" if newest_first else cql


def _parse_page_reference(reference: str) -> tuple[str | None, tuple[str, str] | None]:
    """Resolve a page id, or a page URL, to ``(page_id, (space, title))``.

    Accepts what a user actually pastes: a bare id, a ``pageId=…`` link, a
    ``/pages/123/Some+Title`` link, or a human ``/display/SPACE/Some+Title``
    link — the last of which carries no id, so it has to be looked up by title.
    """
    value = reference.strip()
    if value.isdigit():
        return value, None

    parsed = urlparse(value)
    path = parsed.path

    page_id = re.search(r"[?&]pageId=(\d+)", value)
    if page_id:
        return page_id.group(1), None

    in_path = re.search(r"/pages/(?:viewpage\.action.*?)?(\d+)", path)
    if in_path:
        return in_path.group(1), None

    display = re.search(r"/display/([^/]+)/([^/?#]+)", path)
    if display:
        space = unquote(display.group(1))
        title = unquote(display.group(2).replace("+", " "))
        return None, (space, title)

    raise ValueError(
        f"Cannot tell which page '{reference}' is. Pass a numeric page id, a page "
        f"URL, or the `space` + `title` arguments."
    )


def _to_markdown(markup: str) -> str:
    """Convert a rendered Confluence body to Markdown.

    Links are absolutised first: Confluence emits ``/display/…`` hrefs, which
    are useless once they leave the browser. Markdown escaping of ``*`` and
    ``_`` is switched off — wiki pages are full of both as ordinary
    punctuation, and backslashes in front of them only obscure the text.
    """
    soup = BeautifulSoup(markup, "html.parser")
    for tag, attr in (("a", "href"), ("img", "src")):
        for node in soup.find_all(tag):
            value = node.get(attr)
            if value:
                node[attr] = _absolute(str(value))
    text = markdownify(
        str(soup),
        heading_style="ATX",
        escape_asterisks=False,
        escape_underscores=False,
    )
    return _BLANK_LINES_RE.sub("\n\n", text).strip()


def _to_text(markup: str) -> str:
    """Flatten a body to plain text, keeping paragraph breaks."""
    text = BeautifulSoup(markup, "html.parser").get_text("\n")
    return _BLANK_LINES_RE.sub("\n\n", text).strip()


def _body(page: dict[str, Any], key: str) -> str:
    """Pull ``body.<key>.value`` out of a content object."""
    body = page.get("body")
    body = body if isinstance(body, dict) else {}
    section = body.get(key)
    section = section if isinstance(section, dict) else {}
    return str(section.get("value") or "")


def _render(page: dict[str, Any], fmt: str) -> str:
    """Render the page body in the requested format."""
    if fmt == "storage":
        return _body(page, "storage")
    html = _body(page, "view") or _body(page, "storage")
    if fmt == "html":
        return html
    return _to_text(html) if fmt == "text" else _to_markdown(html)


def _labels(page: dict[str, Any]) -> list[str]:
    """Label names attached to the page (from ``metadata.labels``)."""
    metadata = page.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    labels = metadata.get("labels")
    labels = labels if isinstance(labels, dict) else {}
    return [
        str(item.get("name"))
        for item in labels.get("results", [])
        if isinstance(item, dict) and item.get("name")
    ]


def _person(container: dict[str, Any] | None, key: str) -> str | None:
    """Display name of a user nested under ``container[key]``."""
    container = container if isinstance(container, dict) else {}
    person = container.get(key)
    person = person if isinstance(person, dict) else {}
    return person.get("displayName") or person.get("username") or None


def _page_url(page: dict[str, Any]) -> str | None:
    """Browser URL of a page from its ``_links``."""
    links = page.get("_links")
    links = links if isinstance(links, dict) else {}
    return _absolute(links.get("webui") or links.get("tinyui"))


def _page_meta(page: dict[str, Any]) -> dict[str, Any]:
    """Flatten the metadata every page-shaped result carries."""
    space = page.get("space") if isinstance(page.get("space"), dict) else {}
    version = page.get("version") if isinstance(page.get("version"), dict) else {}
    history = page.get("history") if isinstance(page.get("history"), dict) else {}
    return {
        "id": page.get("id"),
        "title": page.get("title"),
        "type": page.get("type"),
        "space_key": space.get("key"),
        "space_name": space.get("name"),
        "url": _page_url(page),
        "version": version.get("number"),
        "updated": version.get("when"),
        "updated_by": _person(version, "by"),
        "created": history.get("createdDate"),
        "created_by": _person(history, "createdBy"),
    }


def _search(cql: str, limit: int, start: int, include_archived: bool) -> dict[str, Any]:
    """Run a CQL search, turning Confluence's two failure modes into messages."""
    try:
        return get_confluence().cql(
            cql,
            start=start,
            limit=limit,
            expand="content.space",
            include_archived_spaces=include_archived or None,
        ) or {}
    except ApiValueError as exc:
        raise ValueError(f"Confluence rejected the CQL query: {cql}") from exc
    except HTTPError as exc:
        raise _http_error(f"search (cql: {cql})", "rest/api/search", exc) from exc


def _search_hit(hit: dict[str, Any]) -> dict[str, Any]:
    """Flatten one ``rest/api/search`` result into id / title / space / excerpt."""
    content = hit.get("content") if isinstance(hit.get("content"), dict) else {}
    container = hit.get("resultGlobalContainer")
    container = container if isinstance(container, dict) else {}
    space = content.get("space") if isinstance(content.get("space"), dict) else {}
    return {
        "id": content.get("id"),
        "title": _clean_highlight(hit.get("title")) or content.get("title"),
        "type": content.get("type") or hit.get("entityType"),
        "space_key": space.get("key"),
        "space_name": space.get("name") or container.get("title"),
        "url": _absolute(hit.get("url")) or _page_url(content),
        "excerpt": _clean_highlight(hit.get("excerpt")) or None,
        "last_modified": hit.get("lastModified") or hit.get("friendlyLastModified"),
    }


def _identify(
    page: str | None, space: str | None, title: str | None
) -> tuple[str | None, str | None, str | None]:
    """Normalise the three addressing arguments to ``(page_id, space, title)``.

    A ``/display/SPACE/Title`` URL carries no id, so it degrades to a title
    lookup; explicit ``space``/``title`` arguments win over the URL's.
    """
    if not page:
        return None, space, title
    page_id, by_title = _parse_page_reference(page)
    if by_title and not (space and title):
        space, title = by_title
    return page_id, space, title


def _fetch_page(
    page: str | None, space: str | None, title: str | None, expand: str
) -> dict[str, Any]:
    """Load a page by id/URL, or by space + title."""
    confluence = get_confluence()
    page_id, space, title = _identify(page, space, title)

    if page_id:
        try:
            result = confluence.get_page_by_id(page_id, expand=expand)
        except HTTPError as exc:
            endpoint = f"rest/api/content/{page_id}"
            raise _http_error(f"page {page_id}", endpoint, exc) from exc
        except ApiError as exc:
            # The client turns a 404 here into its own (id-less) message.
            raise RuntimeError(f"Confluence page {page_id}: {exc}") from exc
        if not isinstance(result, dict) or not result.get("id"):
            raise RuntimeError(f"Confluence returned no page for id {page_id}")
        return result

    if not (space and title):
        raise ValueError("pass `page` (id or URL), or both `space` and `title`")

    try:
        result = confluence.get_page_by_title(space=space, title=title, expand=expand)
    except HTTPError as exc:
        endpoint = f"rest/api/content?spaceKey={space}&title={title}"
        raise _http_error(f"page '{title}' in space '{space}'", endpoint, exc) from exc
    if not isinstance(result, dict) or not result.get("id"):
        raise RuntimeError(
            f"No page titled '{title}' in space '{space}'. The whole title has to "
            f"match — use search_confluence to find the page, then pass its id."
        )
    return result


def _iter_spaces() -> Iterator[dict[str, Any]]:
    """Yield every space on the instance, fetching one page at a time.

    ``rest/api/space`` returns far fewer spaces than a real instance has, so
    filtering a single response by name silently misses most of them. Lazily
    paging keeps ``query`` honest while stopping as soon as the caller has
    enough matches; ``_SPACE_MAX`` is only a runaway guard.
    """
    confluence = get_confluence()
    start = 0
    while start < _SPACE_MAX:
        try:
            response = confluence.get_all_spaces(start=start, limit=_SPACE_PAGE)
        except HTTPError as exc:
            raise _http_error("space listing", "rest/api/space", exc) from exc

        batch = response.get("results", []) if isinstance(response, dict) else response
        batch = [s for s in (batch or []) if isinstance(s, dict)]
        yield from batch
        if len(batch) < _SPACE_PAGE:
            return
        start += _SPACE_PAGE


def register(mcp: FastMCP) -> None:
    """Attach Confluence tools to the FastMCP instance."""

    @mcp.tool()
    def search_confluence(
        query: str | None = None,
        space: str | None = None,
        label: str | None = None,
        content_type: str | None = "page",
        newest_first: bool = False,
        limit: int = 25,
        start: int = 0,
        include_archived: bool = False,
        cql: str | None = None,
    ) -> dict[str, Any]:
        """Search Confluence pages by text, space or label.

        Full-text search over titles, bodies and attachments, ranked by
        relevance. Returns matches with an excerpt around the hit — read the
        page itself with ``get_confluence_page`` using the returned ``id``.

        Args:
            query: Words to look for, e.g. ``release checklist``. Matched with
                CQL's ``text ~`` operator over the whole page.
            space: Restrict to one space key, e.g. ``ENG``. Use
                ``get_confluence_spaces`` when the key is unknown.
            label: Restrict to pages carrying this label.
            content_type: Content type to search — ``page`` (default),
                ``blogpost``, ``comment``, ``attachment``. Pass ``None`` to
                search every type.
            newest_first: Order by last modification instead of relevance.
                Use it for "what changed lately" questions; it discards the
                relevance ranking, so leave it off for topic searches.
            limit: Maximum results (default 25).
            start: Offset into the result set, for paging.
            include_archived: Also search archived spaces.
            cql: Raw CQL, used verbatim and ignoring every argument above
                except ``limit``/``start``. For queries this signature cannot
                express, e.g. ``creator = jsmith AND lastmodified > -7d``.

        Returns ``results`` (id, title, space, url, excerpt, last_modified),
        the ``cql`` that was run, and ``total`` — the full match count, which
        is usually larger than the page of results returned.
        """
        effective_cql = cql or _build_cql(query, space, label, content_type, newest_first)
        response = _search(effective_cql, limit=limit, start=start,
                           include_archived=include_archived)

        hits = response.get("results") or []
        results = [_search_hit(h) for h in hits if isinstance(h, dict)]
        return {
            "cql": effective_cql,
            "total": response.get("totalSize", response.get("size", len(results))),
            "start": start,
            "count": len(results),
            "results": results,
        }

    @mcp.tool()
    def get_confluence_page(
        page: str | None = None,
        space: str | None = None,
        title: str | None = None,
        format: str = "markdown",
        max_chars: int = 20000,
        offset: int = 0,
    ) -> dict[str, Any]:
        """Read a Confluence page: its content plus who changed it and when.

        The body is returned as Markdown by default — headings, lists, tables
        and code blocks survive, the macro markup does not. Long pages come
        back truncated at ``max_chars``; the response says so and gives the
        ``next_offset`` to continue from.

        Args:
            page: The page id (``123456``) or any page URL — ``…?pageId=123``,
                ``…/pages/123/Title`` and ``…/display/SPACE/Title`` all work.
            space: Space key, when identifying the page by title.
            title: Exact page title, used together with ``space``. The whole
                title has to match — prefer ``search_confluence`` to find the
                page, then pass its id.
            format: ``markdown`` (default), ``text`` (no markup at all),
                ``html`` (the rendered body) or ``storage`` (Confluence's raw
                XHTML source — needed only to edit a page elsewhere).
            max_chars: Cap on the returned content length (default 20000).
                Raise it to read a long page in one go.
            offset: Character offset to start from, for reading a long page in
                successive chunks.

        Returns the page metadata (``id``, ``title``, ``space_key``, ``url``,
        ``version``, ``created``/``updated`` with authors, ``labels``,
        ``ancestors`` breadcrumb) and ``content``.
        """
        if format not in _FORMATS:
            raise ValueError(f"format must be one of {', '.join(_FORMATS)}")
        if max_chars <= 0:
            raise ValueError("max_chars must be positive")
        if offset < 0:
            raise ValueError("offset must not be negative")

        body_expand = "body.storage" if format == "storage" else "body.view"
        result = _fetch_page(page, space, title, f"{_PAGE_EXPAND},{body_expand}")

        content = _render(result, format)
        chunk = content[offset : offset + max_chars]
        end = offset + len(chunk)

        ancestors = result.get("ancestors")
        ancestors = ancestors if isinstance(ancestors, list) else []

        return {
            **_page_meta(result),
            "labels": _labels(result),
            "ancestors": [
                {"id": a.get("id"), "title": a.get("title")}
                for a in ancestors
                if isinstance(a, dict)
            ],
            "format": format,
            "content": chunk,
            "content_chars": len(content),
            "offset": offset,
            "truncated": end < len(content),
            "next_offset": end if end < len(content) else None,
        }

    @mcp.tool()
    def get_confluence_page_children(
        page: str | None = None,
        space: str | None = None,
        title: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        """List the direct child pages of a page.

        Confluence spaces are trees, and a page is often just a table of
        contents for the pages under it. Walk down with this instead of
        guessing titles; the ``ancestors`` of ``get_confluence_page`` walk up.

        Args:
            page: Parent page id or URL (same forms as ``get_confluence_page``).
            space: Space key, when identifying the parent by title.
            title: Exact parent title, used together with ``space``.
            limit: Maximum children to return (default 50).

        Returns ``children``: id, title, space and url for each child page.
        """
        # An id (or a URL carrying one) needs no lookup — only a title does.
        page_id, space, title = _identify(page, space, title)
        if page_id:
            parent = {"id": page_id, "title": title} if title else {"id": page_id}
        else:
            resolved = _fetch_page(None, space, title, "version")
            parent = {"id": str(resolved.get("id")), "title": resolved.get("title")}
        parent_id = str(parent["id"])

        try:
            response = get_confluence().get_page_child_by_type(
                parent_id, type="page", limit=limit, expand="space,version"
            )
        except HTTPError as exc:
            endpoint = f"rest/api/content/{parent_id}/child/page"
            raise _http_error(f"children of page {parent_id}", endpoint, exc) from exc

        # The client returns a list on Server and a paged dict on Cloud.
        items = response.get("results", []) if isinstance(response, dict) else response
        children = [
            {k: v for k, v in _page_meta(child).items() if v is not None}
            for child in (items or [])
            if isinstance(child, dict)
        ]
        return {"parent": parent, "count": len(children), "children": children}

    @mcp.tool()
    def get_confluence_spaces(
        query: str | None = None,
        limit: int = 100,
        include_personal: bool = False,
    ) -> list[dict[str, Any]]:
        """List Confluence spaces — how to find the ``space`` key to search in.

        The whole space directory is walked, so ``query`` matches across the
        instance and not just the first page of spaces.

        Args:
            query: Only return spaces whose key or name contains this substring
                (case-insensitive).
            limit: Maximum spaces to return (default 100). Searching stops as
                soon as this many match.
            include_personal: Include personal spaces (``~username``), which are
                excluded by default because they crowd out team spaces.

        Returns each space's ``key``, ``name``, ``type`` and ``url``.
        """
        needle = query.strip().lower() if query else None

        spaces = []
        for space in _iter_spaces():
            if not include_personal and space.get("type") == "personal":
                continue
            key = str(space.get("key") or "")
            name = str(space.get("name") or "")
            if needle and needle not in key.lower() and needle not in name.lower():
                continue
            spaces.append(
                {
                    "key": key,
                    "name": name,
                    "type": space.get("type"),
                    "url": _page_url(space),
                }
            )
            if len(spaces) >= limit:
                break
        return spaces
