# orb-renderdoc v2 — Design Plan

## Motivation

The v1 integration exposed 70 tools to the agent. In practice:
- The agent ignored dedicated tools and went straight to the Python `eval` handler.
- 20+ tool calls were needed before the agent understood the problem space.
- The agent fumbled the RenderDoc Python API for ~6 iterations per session before
  becoming productive, re-discovering the same patterns every time.

Research (Zhang et al., "One Tool Is Enough", arXiv 2512.20957) confirms that fewer,
semantically grounded tools outperform large tool suites — even across model size gaps.
Tool design is prompt engineering: naming, description quality, and semantic granularity
directly impact agent capability.

## Design Principles

1. **Minimal tool surface.** Three tools. One primary (execute Python), one reference
   (search API docs), one management (instance connection). Everything else is a utility
   function, not a tool.
2. **Rich descriptions over many tools.** The tool description encodes the RenderDoc
   access model, object graph, cursor semantics, and key patterns. The agent should
   write correct Python on the first call.
3. **Structured data.** Return JSON, not formatted text. Less fragile, easier to
   maintain, lets the agent decide what to focus on.
4. **Runtime-accurate reference.** The API index is built by introspecting the live
   `renderdoc` module at startup. Docstrings are RST-formatted and rich (SWIG
   `DOCUMENT()` macro, validated by RenderDoc's CI). No static dataset to maintain
   across RenderDoc versions.
5. **Python end-to-end.** MCP server in Python (FastMCP) to keep both sides of the
   socket in the same language. Eliminates cross-language translation bugs.

## Architecture

```
Claude (MCP client)
    |  stdio (JSON-RPC)
    v
MCP Server (Python, FastMCP)
    |  TCP socket (JSON-lines, localhost)
    v
RenderDoc Extension (Python, inside RenderDoc)
    |  renderdoc / qrenderdoc modules
    v
RenderDoc replay engine
```

Two processes, connected by TCP over loopback. The MCP server is spawned by the MCP
client as a subprocess (stdio transport). The extension runs inside RenderDoc's embedded
Python interpreter and hosts a TCP server.

## MCP Tools

### 1. `eval` (primary)

Execute Python code in the RenderDoc environment. This is the agent's primary interface
for all state inspection, analysis, and UI interaction.

**Description** (embedded in tool definition — this is the critical piece):

The description must cover:

- **Access model**: Code executes inside RenderDoc's Python environment. The
  `pyrenderdoc` global provides access to the capture context. Use
  `pyrenderdoc.Replay().BlockInvoke(callback)` where the callback receives a
  `ReplayController` instance.

- **Cursor model**: `controller.SetFrameEvent(eventId, True)` moves the replay to a
  specific event. All state queries return state *at the current event*. Must be called
  before querying pipeline state.

- **Object graph**:
  - `ReplayController` is the hub.
  - `GetRootActions()` -> action tree (draw calls, dispatches, markers).
  - `GetPipelineState()` -> `PipeState` (API-agnostic pipeline state).
  - `PipeState.GetShaderReflection(stage)` -> shader metadata.
  - `PipeState.GetOutputTargets()` / `GetDepthTarget()` -> render targets.
  - `PipeState.GetReadOnlyResources(stage)` / `GetReadWriteResources(stage)` -> bound
    resources.
  - `PipeState.GetConstantBlocks(stage)` -> constant buffers.
  - `controller.GetBufferData(id, offset, len)` / `GetTextureData(id, sub)` -> raw
    bytes.

- **Action tree**: Hierarchical. Markers create parent-child relationships. Actual draw
  calls have `ActionFlags.Drawcall`. Traverse with `.children`, `.next`, `.previous`.
  Each action has `.eventId` (use with `SetFrameEvent`) and `.flags`.

- **Key enums**: `ShaderStage.Vertex` / `Pixel` / `Fragment` / `Compute`,
  `ActionFlags.Drawcall` / `Dispatch` / `Clear` / `PushMarker` / `PopMarker`,
  `MeshDataStage.VSIn` / `VSOut`.

- **Available utilities**: `inspect(obj)`, `diff_state(eid_a, eid_b)`,
  `goto_event(eid)`, `view_texture(id)`, and data interpretation helpers for decoding
  raw buffer/texture bytes.

- **Return convention**: The last expression in the code block is returned as the
  result. Return dicts or lists for structured data.

**Parameters**:
- `code` (string, required): Python code to execute.

**Returns**: JSON. The result of the last expression, or error details on failure.

**Error format**: On failure, return a structured error with the stack trace, the
failing line highlighted, and a contextual hint about common mistakes (e.g., "did you
call SetFrameEvent before querying pipeline state?"). Match the Rust compiler's approach:
show what went wrong, where, and what to try.

### 2. `search_api` (secondary)

Search the RenderDoc Python API reference. Built at extension startup by introspecting
the `renderdoc` module — enumerates all classes, methods, enums, and extracts their
RST-formatted `__doc__` strings.

**Description**: Search the RenderDoc Python API for classes, methods, enums, or
concepts. Use when you need to discover what API exists for a task, find exact method
signatures, or understand parameter types. Returns matching entries with their official
documentation.

**Parameters**:
- `query` (string, required): Search term — a class name, method name, enum name, or
  concept keyword.

**Returns**: JSON array of matching entries. Each entry contains:
- `name`: Fully qualified name (e.g., `ReplayController.SetFrameEvent`)
- `kind`: `class`, `method`, `property`, `enum`, `enum_value`
- `doc`: Full docstring (RST-formatted, includes param/type/return annotations)
- `signature`: Method signature if applicable

### 3. `instance` (management)

Manage RenderDoc instance connections. On first use, auto-connects to the first available
instance and reports any others found.

**Description**: Manage connections to running RenderDoc instances. Lists available
instances, connects to a specific one, or disconnects. On first use, automatically
connects to the first available instance.

**Parameters**:
- `action` (string, required): One of `list`, `connect`, `disconnect`.
- `port` (integer, optional): Port to connect to. Required for `connect`.

**Returns**: JSON. Instance info (port, capture state, API type) for `connect` / `list`.
Confirmation for `disconnect`.

## Pre-loaded Utilities

Available in the `eval` tool's execution environment. Not MCP tools — just functions
the agent can call in its Python code.

### `inspect(obj)`

Runtime introspection of any Python object. Returns methods, properties, and their
docstrings. More focused than `help()` — filters out dunder methods, groups by kind,
formats for structured consumption. The first-line discovery tool: "I have this object,
what can I do with it?"

### `diff_state(eid_a, eid_b)`

Diff pipeline state between two events. Sets each event, snapshots the pipeline state,
and returns a structured diff showing what changed (shaders, render targets, blend state,
depth state, bound resources, etc.). Encodes domain knowledge about what's meaningful
to compare in a GPU debugging context.

### Data Interpretation Helpers

Utility functions for decoding raw bytes from `GetBufferData` / `GetTextureData` into
typed, inspectable data. A human would decode these manually; these helpers encode the
"look at the format, unpack accordingly" workflow:
- Interpret buffer bytes given a format or vertex layout.
- Interpret texture bytes given dimensions and pixel format.
- Summarize numeric data (min, max, NaN/Inf detection, histogram).

### UI Helpers

Simple convenience functions for pointing the human at things in the RenderDoc UI:
- `goto_event(eid)` — Navigate the UI to a specific event.
- `view_texture(resource_id)` — Open the texture viewer for a resource.
- `highlight_drawcall(eid)` — Highlight a draw call in the event browser.

Thin wrappers around `pyrenderdoc` UI methods. They exist to give the agent obvious,
well-named entry points for common UI operations.

## RenderDoc Extension

### Handlers

Dramatically slimmed from v1's 70 handlers:

1. **`eval`** — Execute Python code. The primary handler. Executes in the extension's
   Python environment with access to `pyrenderdoc`, `renderdoc` module, and all
   pre-loaded utilities. Trusted environment (RenderDoc provides its own sandboxing).
2. **`api_index`** — Return the pre-built API reference index (or a filtered subset).
   Called by the MCP server's `search_api` tool.
3. **`instance_info`** — Return metadata about this RenderDoc instance (port, capture
   state, API type). Used by the MCP server for instance discovery and management.

### Bridge Server

Stays largely the same as v1:
- `BridgeServer`: Background thread, TCP server on localhost (port range 19876-19885).
- `JsonSocket`: JSON-lines protocol over TCP.
- `winsock.py`: ctypes Winsock2 wrapper (RenderDoc's embedded Python lacks `socket`).
- Dispatch lock serializes access to RenderDoc's single-threaded replay API.

### API Index Builder

Runs once at extension startup (or on `OnCaptureLoaded` if API surface changes with
capture type). Introspects the `renderdoc` module:
- Enumerate all classes, methods, properties, enums via `dir()` recursion.
- Extract `__doc__` strings (RST-formatted, include param/type/return annotations).
- Build a searchable index (list of entries with name, kind, doc, signature).
- Cache in memory. Serve via `api_index` handler.

### Serialization

The `serialize.py` module from v1 carries forward. Useful for converting RenderDoc C++
types to JSON-safe Python dicts in `eval` results. Exposed as a utility module in the
eval environment so the agent can call serialization helpers directly.

## MCP Server (Python)

### Framework

FastMCP (part of the `mcp` package, v1.26+). Decorator-based tool definition, stdio
transport.

### Components

1. **Tool definitions**: Three tools (`eval`, `search_api`, `instance`) with rich
   descriptions.
2. **TCP client**: Connects to RenderDoc extension over localhost. JSON-lines protocol.
   Lazy connection, auto-reconnect.
3. **Instance management**: Discovery (probe port range with short timeout per port).
   Auto-connect on first tool use, mention other available instances in the response.

### Transport

stdio. This is how Claude (and most MCP clients) spawn local MCP servers. No need for
HTTP or SSE.

## Implementation Order

1. **API index builder** — Build and validate the introspection system against a live
   RenderDoc instance. This is the highest-risk piece (depends on docstring quality
   and introspection coverage). Validates the core bet of the design.
2. **Extension slim-down** — Reduce handlers to eval + api_index + instance_info.
   Port the bridge server, winsock wrapper, and serialization from v1. Validate the
   eval handler covers common workflows that v1's dedicated handlers covered.
3. **MCP server** — Python FastMCP server with three tools. TCP client to extension.
   Instance discovery and auto-connection.
4. **Tool descriptions** — Write and iterate on the `eval` tool description. This is
   prompt engineering and needs testing against real debugging scenarios to get right.
5. **Utilities** — `inspect()`, `diff_state()`, data interpretation helpers, UI
   helpers.
6. **Integration testing** — End-to-end with Claude against real captures. Focus on
   measuring: how many calls to first useful insight? Does the agent write correct
   RenderDoc Python on the first try?

## Success Criteria

- Agent writes correct RenderDoc Python on the first call (no fumble iterations).
- Useful insight within 5 tool calls, not 20+.
- No dedicated tools needed — `eval` + `search_api` cover all debugging workflows.
- API reference is version-accurate with zero maintenance.
