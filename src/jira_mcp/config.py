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


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return cached settings, validating required env vars on first access."""
    return Settings()  # type: ignore[call-arg]
