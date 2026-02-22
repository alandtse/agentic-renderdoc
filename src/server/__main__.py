"""Entry point for the agentic-renderdoc MCP server."""

from server.app import mcp


def main():
    mcp.run()


if __name__ == "__main__":
    main()
