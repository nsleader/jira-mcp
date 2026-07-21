"""Factory for the atlassian-python-api Jira client."""

from __future__ import annotations

from functools import lru_cache

from atlassian import Jira

from .config import get_settings


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
