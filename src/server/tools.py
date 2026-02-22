"""MCP tool definitions for eval, search_api, and instance."""

from server.app import mcp
from server.client import RenderDocClient

_client = RenderDocClient()


# --- eval ---

@mcp.tool()
def eval(code: str) -> dict:
    """Execute Python code in a live RenderDoc replay session.

    TODO: Rich description covering access model, cursor model, object graph,
    key enums, available utilities, and return convention. This description is
    the critical piece of the design — see docs/DESIGN.md.
    """
    return _client.send("eval", {"code": code})


# --- search_api ---

@mcp.tool()
def search_api(query: str) -> dict:
    """Search the RenderDoc Python API reference for classes, methods, enums,
    or concepts. Use when you need to discover what API exists for a task,
    find exact method signatures, or understand parameter types.

    Returns matching entries with their official documentation extracted from
    the live RenderDoc module's docstrings.
    """
    return _client.send("api_index", {"query": query})


# --- instance ---

@mcp.tool()
def instance(action: str, port: int | None = None) -> dict:
    """Manage connections to running RenderDoc instances.

    Lists available instances, connects to a specific one, or disconnects.
    On first use, automatically connects to the first available instance.

    action: One of "list", "connect", "disconnect".
    port: Port to connect to. Required for "connect".
    """
    if action == "list":
        return {"instances": _client.discover_instances()}
    elif action == "connect":
        if port is None:
            return {"error": "port is required for connect"}
        _client.connect(port)
        return _client.send("instance_info", {})
    elif action == "disconnect":
        _client.disconnect()
        return {"status": "disconnected"}
    else:
        return {"error": f"unknown action: {action}"}
