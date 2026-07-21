"""FastMCP server entry point for the Jira MCP server."""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from .tools import register_all

mcp = FastMCP("jira-mcp")
register_all(mcp)


def main() -> None:
    """Run the MCP server over stdio."""
    mcp.run()


if __name__ == "__main__":
    main()
