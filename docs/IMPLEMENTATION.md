# Implementation Plan

Actionable steps for building agentic-renderdoc v2. Each phase has clear inputs,
outputs, and validation criteria. Phases are sequential unless noted otherwise.

v1 source for porting: `../orb-renderdoc/`

## Phase 1: Port Foundation (Extension Infrastructure)

Port the pieces that are known-good from v1 and don't need redesign. This gets us a
working extension that can accept TCP connections and execute commands.

### 1a. Port `winsock.py`

**Source**: `../orb-renderdoc/python/agentic_renderdoc/winsock.py` (190 lines)
**Target**: `src/extension/winsock.py`

Straight port. This is a ctypes wrapper around ws2_32.dll and doesn't need changes.
The API surface is: `WinsockSocket` with `bind`, `listen`, `accept`, `send`, `recv`,
`close`, plus `WSAStartup` / `WSACleanup` lifecycle.

**Validate**: Unit test that opens a socket, sends a message to itself, receives it.
Can run outside RenderDoc (just needs Windows).

### 1b. Port `serialize.py`

**Source**: `../orb-renderdoc/python/agentic_renderdoc/serialize.py` (559 lines)
**Target**: `src/extension/serialize.py`

Straight port. Converts RenderDoc SWIG types to JSON-safe dicts. Functions like
`pipeline_state()`, `shader_reflection()`, `action_description()`, etc.

Review for cleanup opportunities but don't redesign. This is utility code that works.

**Validate**: Can't unit test without RenderDoc types. Validate during Phase 3
integration.

### 1c. Port and simplify bridge server

**Source**: `../orb-renderdoc/python/agentic_renderdoc/__init__.py` (BridgeServer,
JsonSocket, AgenticExtension classes)
**Target**: `src/extension/bridge.py` (BridgeServer, JsonSocket) and
`src/extension/__init__.py` (extension entry point, `register()` function)

Port the networking layer (BridgeServer, JsonSocket) into `bridge.py`. These use the
winsock wrapper from 1a for TCP server functionality.

Port the extension entry point (`register()`, `AgenticExtension` as `CaptureViewer`)
into `src/extension/__init__.py`. Simplify: the v1 extension registered 70 handlers
and had complex state management. The v2 extension registers 3 handlers and the state
is simpler.

Key pieces to preserve:
- `BridgeServer`: Background thread, port range binding, connection handling.
- `JsonSocket`: JSON-lines read/write with newline delimiters.
- `HandlerContext`: Capture context, `replay(callback)` for thread-safe replay access,
  `invoke_ui(callback)` for UI thread access.
- `register(version, ctx)` and `unregister()` entry points.
- `AgenticExtension(CaptureViewer)` with `OnCaptureLoaded` / `OnCaptureClosed` hooks.

Key pieces to drop:
- All 70 handler registrations (replaced by 3 in handlers.py).
- Response formatting logic (agent controls output format now).
- Grep filter system (agent can filter in Python).
- Complex target/capture management state (simplify to capture-loaded bool).

**Validate**: Extension loads in RenderDoc, bridge server starts, can connect with
a TCP client and send/receive JSON-lines messages.

## Phase 2: Core Handlers

### 2a. Implement `eval` handler

**File**: `src/extension/handlers.py`

The scaffolded version has the core logic (`_exec_with_result` with AST splitting).
What needs work:

1. **Namespace construction** (`_build_namespace`): Inject `rd`, `qrd`,
   `pyrenderdoc`, the serialization module, and all utility functions. The namespace
   is rebuilt per-call (stateless between calls, utilities always available).

2. **Thread safety**: The eval code may need replay access. Two patterns:
   - Agent writes a `BlockInvoke` callback directly (the documented pattern).
   - We provide a helper that wraps eval code in a `BlockInvoke` automatically when
     it touches the replay controller. Start with the explicit pattern; add the
     convenience wrapper later if agents struggle with it.

3. **Error formatting** (`_format_error`): Stack trace with the failing line
   highlighted, plus contextual hints. Hints to implement:
   - "SetFrameEvent not called" — if the error involves pipeline state access and no
     `SetFrameEvent` appears in the code.
   - "BlockInvoke not used" — if the error suggests replay thread access without the
     callback pattern.
   - "Unknown attribute" — suggest `inspect(obj)` or `search_api`.

4. **Result serialization**: The last expression's value needs to be JSON-serializable.
   For RenderDoc types, auto-apply the serialize module. For basic Python types, pass
   through. For unserializable types, fall back to `repr()`.

**Validate**: Execute simple Python (math, string ops) through the handler. Execute
RenderDoc-specific code (action tree traversal, pipeline state query) with a capture
loaded.

### 2b. Implement `api_index` handler

**File**: `src/extension/api_index.py` and `src/extension/handlers.py`

The scaffolded `api_index.py` has the walker structure. What needs work:

1. **Enum detection**: RenderDoc enums are SWIG-generated IntEnum-like types. The
   `_classify` function needs to distinguish these from regular classes. Check for
   `__members__` attribute or inheritance from `int`.

2. **Enum value extraction**: For enum types, enumerate individual values as separate
   index entries (kind: `enum_value`). These are what agents actually search for
   (e.g., "ActionFlags.Drawcall").

3. **Property handling**: SWIG-generated properties on struct types. These appear as
   descriptors on the class. Detect and index them with their docstrings.

4. **Index caching**: Build once at extension startup (`register()` or first
   `OnCaptureLoaded`). Store on the `HandlerContext`. The `api_index` handler in
   `handlers.py` calls `search_index()` against the cached data.

5. **Search quality**: The simple substring match in the scaffold is a starting point.
   Consider: name matches ranked above doc-body matches, exact matches ranked above
   partial matches. Keep it simple — this doesn't need to be a search engine, just
   useful enough to find things.

**Validate**: Build the index against a live RenderDoc instance. Verify coverage:
spot-check that `ReplayController`, `PipeState`, `ActionDescription`, `ShaderStage`,
`ActionFlags` all appear with meaningful docstrings. Verify search returns sensible
results for queries like "SetFrameEvent", "pipeline", "Drawcall".

### 2c. Implement `instance_info` handler

**File**: `src/extension/handlers.py`

Simple. Return:
- `port`: The bridge server's listening port.
- `capture_loaded`: Whether a capture is open.
- `api_type`: Which graphics API the capture uses (D3D11, D3D12, Vulkan, GL), if a
  capture is loaded. Pull from `GetAPIProperties()`.
- `capture_path`: Path to the loaded capture file, if any.
- `event_count`: Total event count in the capture, if loaded.

**Validate**: Query with and without a capture loaded.

## Phase 3: MCP Server

### 3a. TCP client

**File**: `src/server/client.py`

The scaffolded version is mostly complete. What needs work:

1. **Auto-reconnect**: If a `send()` fails due to connection drop, try reconnecting
   once and retrying. The v1 Rust client did this.

2. **Discovery enrichment**: When discovering instances, optionally query each for
   `instance_info` to return richer data (capture state, API type) instead of just
   port numbers. This lets the `instance list` tool show useful info.

3. **Connection state on first use**: When `ensure_connected` auto-connects, include
   the instance info and any other available instances in the response metadata so
   the agent knows what it connected to.

**Validate**: Connect to a running extension, send an eval command, get a response.
Test auto-reconnect by killing and restarting the extension.

### 3b. Tool definitions with descriptions

**File**: `src/server/tools.py`

The scaffolded version has the tool structure. The critical work is writing the `eval`
tool's description. This is prompt engineering and is the most important single piece
of the project.

**`eval` description contents** (see DESIGN.md for the full spec):
- Access model (BlockInvoke pattern)
- Cursor model (SetFrameEvent)
- Object graph (ReplayController -> PipeState -> resources)
- Action tree structure and traversal
- Key enums with values
- Available utility functions
- Return convention
- Common patterns (e.g., "to inspect a draw call's state: set the event, get pipeline
  state, query what you need")

Write the description as a Python multiline string. It will be long — that's
intentional. This is the thing that eliminates the 6-iteration fumble.

**`search_api` description**: Shorter. Explain when to use it (discovery, exact
signatures, parameter types) and what it returns.

**`instance` description**: Shortest. Explain the three actions.

**Validate**: Start the MCP server, list tools, verify descriptions appear correctly
in the tool listing. Check that descriptions are well-formed and complete.

### 3c. Server entry point and packaging

**Files**: `src/server/__main__.py`, `src/server/app.py`, `pyproject.toml`

The scaffolded versions are mostly complete. Verify:
- `python -m server` starts the FastMCP server on stdio.
- The `agentic-renderdoc` console script entry point works.
- Tool imports resolve correctly.

**Validate**: `mcp dev src/server/__main__.py` launches the dev inspector (if
available) and shows all three tools.

## Phase 4: Utilities

### 4a. `inspect(obj)`

**File**: New file `src/extension/utilities.py`

Runtime introspection helper. Given any Python object, return a structured summary:
- For classes/instances: methods (with signatures and first-line docstring), properties
  (with type from docstring), grouped by kind.
- For enums: all values with their int value and docstring.
- For modules: classes, functions, constants.
- Filter out dunder names. Filter out SWIG internal attributes.

Return a dict, not a string. The agent can format as needed.

**Validate**: `inspect(rd.ReplayController)` returns a useful summary. `inspect(rd.ShaderStage)`
lists all stages. `inspect(some_pipeline_state_instance)` shows available methods.

### 4b. `diff_state(eid_a, eid_b)`

**File**: `src/extension/utilities.py`

Pipeline state diffing. Implementation:
1. `SetFrameEvent(eid_a)`, snapshot pipeline state via `serialize.pipeline_state()`.
2. `SetFrameEvent(eid_b)`, snapshot again.
3. Deep-diff the two dicts. Return only the changed keys with before/after values.

Needs access to the replay controller, so must run inside a `BlockInvoke` callback.
Provide as a function that takes `controller` as the first arg (the agent calls it
inside their own `BlockInvoke`), or as a standalone that manages the `BlockInvoke`
internally.

Leaning toward standalone — the agent calls `diff_state(eid_a, eid_b)` and the
function handles the replay thread access internally. Less boilerplate for the agent.

**Validate**: Diff between a clear and a draw call shows meaningful changes (shaders
bound, render targets set, etc.). Diff between two adjacent draw calls shows what
actually changed.

### 4c. Data interpretation helpers

**File**: `src/extension/utilities.py`

Functions for decoding raw bytes:
- `interpret_buffer(data, format_or_layout)` — Unpack bytes into typed values given
  a ResourceFormat or vertex layout description.
- `interpret_texture(data, width, height, format)` — Decode texture bytes into a
  grid of pixel values.
- `summarize_data(values)` — Min, max, mean, NaN/Inf count, histogram buckets.

Use `struct.unpack` for decoding. The format information comes from RenderDoc's
`ResourceFormat` type (component type, count, byte width).

**Validate**: Decode a known constant buffer and verify values match RenderDoc's
cbuffer viewer. Decode texture pixels and verify against `PickPixel` results.

### 4d. UI helpers

**File**: `src/extension/utilities.py`

Thin wrappers:
- `goto_event(eid)` — `pyrenderdoc.SetEventID([], eid, eid)`
- `view_texture(resource_id)` — Open texture viewer via `pyrenderdoc` context.
- `highlight_drawcall(eid)` — Highlight in event browser.

These need UI thread access (`invoke_ui` pattern from v1). Make them self-contained
so the agent just calls `goto_event(42)` without boilerplate.

**Validate**: Each function visibly changes the RenderDoc UI when called.

## Phase 5: Integration Testing

### 5a. Manual end-to-end test

Load a capture in RenderDoc. Start the MCP server. Connect via Claude. Run through
a realistic debugging scenario:
- "What draw calls are in this frame?"
- "Show me the pipeline state for draw call N."
- "What shaders are bound? Show me the pixel shader source."
- "What's in constant buffer 0 for the pixel shader?"
- "Compare the state between event A and event B."

**Measure**: How many tool calls to first useful insight? Does the agent write correct
RenderDoc Python on the first call?

### 5b. Tool description iteration

Based on 5a results, refine the `eval` tool description. Common failure patterns to
watch for:
- Agent doesn't use `BlockInvoke` -> add more emphasis or a code example.
- Agent uses wrong method names -> verify the description lists correct names.
- Agent doesn't know about utility functions -> make them more prominent.
- Agent writes overly verbose code -> add a "prefer concise" note.

This is iterative. Budget multiple rounds.

### 5c. Edge cases

Test with:
- No capture loaded (eval should return a clear error, not crash).
- Multiple RenderDoc instances (instance tool should list all, auto-connect to first).
- Large captures (performance of action tree traversal, buffer reads).
- Different graphics APIs (D3D11, D3D12, Vulkan) if captures available.
- Extension hot-reload (stop/start extension while MCP server is connected).

## File Summary

After implementation, the project should contain:

```
agentic-renderdoc/
├── docs/
│   ├── DESIGN.md                # Architecture and design rationale
│   └── IMPLEMENTATION.md        # This file
├── src/
│   ├── server/
│   │   ├── __init__.py
│   │   ├── __main__.py          # Entry point
│   │   ├── app.py               # FastMCP server instance
│   │   ├── tools.py             # eval, search_api, instance tools
│   │   └── client.py            # TCP client to RenderDoc
│   └── extension/
│       ├── __init__.py          # Extension entry point (register, AgenticExtension)
│       ├── extension.json       # RenderDoc extension metadata
│       ├── handlers.py          # eval, api_index, instance_info handlers
│       ├── bridge.py            # BridgeServer, JsonSocket
│       ├── api_index.py         # API introspection and index builder
│       ├── utilities.py         # inspect, diff_state, data helpers, UI helpers
│       ├── winsock.py           # ctypes Winsock2 wrapper (ported from v1)
│       └── serialize.py         # RenderDoc type serialization (ported from v1)
├── tests/
│   └── ...
├── pyproject.toml
└── LICENSE
```

## Notes for the Implementing Session

- v1 source is at `../orb-renderdoc/`. Key files to port from:
  - `python/agentic_renderdoc/winsock.py` -> `src/extension/winsock.py`
  - `python/agentic_renderdoc/serialize.py` -> `src/extension/serialize.py`
  - `python/agentic_renderdoc/__init__.py` -> `src/extension/bridge.py` + `src/extension/__init__.py`
- The `renderdoc` and `qrenderdoc` Python modules only exist inside RenderDoc's
  embedded Python. Code that imports them will fail outside RenderDoc. Guard with
  try/except in the extension, don't import in the server.
- RenderDoc's embedded Python lacks the standard `socket` module. The extension MUST
  use winsock.py for all networking. The MCP server (separate process) has standard
  Python and uses `socket` normally.
- The tool description for `eval` is the single most important deliverable. Everything
  else is plumbing. Get the plumbing working, then spend real time on the description.
