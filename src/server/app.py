"""MCP server definition and tool registration."""

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("agentic-renderdoc")

# Tool implementations are registered via decorators in their respective modules.
# Import them here so the decorators execute at startup.
import server.tools  # noqa: F401, E402
