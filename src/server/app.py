"""MCP server definition and tool registration."""

from importlib.metadata import PackageNotFoundError, version

from mcp.server import MCPServer


try:
    _VERSION = version("agentic-renderdoc")
except PackageNotFoundError:
    _VERSION = "0+unknown"


mcp = MCPServer("agentic-renderdoc", version=_VERSION)

# Tool implementations are registered via decorators in their respective modules.
# Import them here so the decorators execute at startup.
import server.tools  # noqa: F401, E402