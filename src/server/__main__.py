"""Entry point for the agentic-renderdoc MCP server."""

from __future__ import annotations

from server.app import mcp


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
