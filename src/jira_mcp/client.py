"""Factories for the atlassian-python-api Jira and Confluence clients."""

from __future__ import annotations

from functools import lru_cache

from atlassian import Confluence, Jira

from .config import get_settings, resolved_confluence_settings


@lru_cache(maxsize=1)
def get_jira() -> Jira:
    """Return a cached Jira client for a Server/Data Center instance.

    Uses Personal Access Token (Bearer) authentication. The client is created
    lazily on first use so importing this module never touches the network or
    requires credentials to be present.
    """
    settings = get_settings()
    return Jira(
        url=settings.url,
        token=settings.personal_token,
        cloud=False,
        verify_ssl=settings.ssl_verify,
        timeout=settings.timeout,
    )


@lru_cache(maxsize=1)
def get_confluence() -> Confluence:
    """Return a cached Confluence client for a Server/Data Center instance.

    Same Bearer-PAT scheme as :func:`get_jira`, but a separate base URL and
    (usually) a separate token — Confluence is its own application. Created
    lazily, so a deployment that only configures Jira never pays for it.
    """
    url, token, ssl_verify, timeout = resolved_confluence_settings()
    return Confluence(
        url=url,
        token=token,
        cloud=False,
        verify_ssl=ssl_verify,
        timeout=timeout,
    )
