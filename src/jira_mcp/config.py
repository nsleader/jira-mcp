"""Runtime configuration loaded from environment / .env."""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Settings for connecting to a Jira Server / Data Center instance.

    Values are read from environment variables (prefix ``JIRA_``), which the MCP
    client passes in the server's connection config (the ``env`` block).
    ``url`` and ``personal_token`` are required.
    """

    model_config = SettingsConfigDict(
        env_prefix="JIRA_",
        extra="ignore",
    )

    url: str
    personal_token: str
    ssl_verify: bool = True
    timeout: int = 75


class ConfluenceSettings(BaseSettings):
    """Settings for connecting to a Confluence Server / Data Center instance.

    Read from environment variables with the ``CONFLUENCE_`` prefix. Everything
    is optional here: Confluence is a separate application from Jira (its own
    host and its own Personal Access Token), but many installs share a hostname
    or a reverse proxy, so unset values fall back to the ``JIRA_`` ones in
    :func:`resolved_confluence_settings`. Nothing is required at import time —
    a server configured for Jira only must still start.
    """

    model_config = SettingsConfigDict(
        env_prefix="CONFLUENCE_",
        extra="ignore",
    )

    url: str | None = None
    personal_token: str | None = None
    ssl_verify: bool | None = None
    timeout: int | None = None


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return cached settings, validating required env vars on first access."""
    return Settings()  # type: ignore[call-arg]


@lru_cache(maxsize=1)
def get_confluence_settings() -> ConfluenceSettings:
    """Return cached raw Confluence settings (no Jira fallbacks applied)."""
    return ConfluenceSettings()


def resolved_confluence_settings() -> tuple[str, str, bool, int]:
    """Return ``(url, token, ssl_verify, timeout)`` for the Confluence client.

    ``CONFLUENCE_URL`` is mandatory — guessing it from ``JIRA_URL`` would point
    at Jira and produce confusing 404s. The token may be inherited from
    ``JIRA_PERSONAL_TOKEN``, which is what a shared-SSO install needs, while a
    normal install sets its own ``CONFLUENCE_PERSONAL_TOKEN``.
    """
    confluence = get_confluence_settings()
    if not confluence.url:
        raise RuntimeError(
            "Confluence is not configured: set CONFLUENCE_URL (e.g. "
            "https://confluence.example.com) in the MCP server's env block. "
            "Set CONFLUENCE_PERSONAL_TOKEN too unless the Jira token also "
            "works there."
        )

    token = confluence.personal_token
    ssl_verify = confluence.ssl_verify
    timeout = confluence.timeout
    if token is None or ssl_verify is None or timeout is None:
        jira = get_settings()
        token = token if token is not None else jira.personal_token
        ssl_verify = ssl_verify if ssl_verify is not None else jira.ssl_verify
        timeout = timeout if timeout is not None else jira.timeout
    return confluence.url, token, ssl_verify, timeout
