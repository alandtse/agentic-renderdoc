"""MCP tool definitions for eval, search_api, and instance."""

from server.app import mcp
from server.client import RenderDocClient

_client = RenderDocClient()


# --- eval ---

@mcp.tool()
def eval(code: str) -> dict:
    """Execute Python code in a live RenderDoc replay session.

    This is your primary interface for all GPU capture inspection, analysis,
    and debugging. Code runs inside RenderDoc's embedded Python interpreter
    with full access to the replay engine.

    ACCESS MODEL
    ============
    The global `pyrenderdoc` provides access to the capture context. To query
    replay state, you must obtain a ReplayController through BlockInvoke:

        def work(controller):
            controller.SetFrameEvent(eventId, True)
            state = controller.GetPipelineState()
            # ... query state ...
            return result
        pyrenderdoc.Replay().BlockInvoke(work)

    BlockInvoke runs your callback on the replay thread and blocks until it
    completes. You MUST use this pattern for any ReplayController access.
    Code outside BlockInvoke can use pyrenderdoc for UI operations.

    CURSOR MODEL
    ============
    The replay engine maintains a cursor position in the frame's event
    timeline. All state queries return state AT the current cursor position.

    - controller.SetFrameEvent(eventId, True) moves the cursor.
    - You MUST call SetFrameEvent before calling GetPipelineState or any
      other state query. Forgetting this is the most common mistake.
    - The second argument (True) forces full pipeline state resolution.

    OBJECT GRAPH
    ============
    ReplayController is the central hub. Key accessors:

    Actions (draw calls, dispatches, markers):
        controller.GetRootActions() -> list of ActionDescription
        Each action has:
            .eventId    -- unique event ID (use with SetFrameEvent)
            .actionId   -- action index
            .flags      -- ActionFlags bitmask (Drawcall, Dispatch, etc.)
            .children   -- list of child actions (markers contain children)
            .next       -- next sibling action (or None)
            .previous   -- previous sibling action (or None)
            .customName -- user-defined marker name (empty string if none)
            .numIndices, .numInstances, .indexOffset, .baseVertex
            .dispatchDimension -- [x, y, z] for compute dispatches

    Pipeline state (after SetFrameEvent):
        controller.GetPipelineState() -> PipeState
        PipeState is API-agnostic. Key methods:
            .GetShader(stage)                   -> ResourceId
            .GetShaderReflection(stage)         -> ShaderReflection
            .GetOutputTargets()                 -> list of render target descriptors
            .GetDepthTarget()                   -> depth target descriptor
            .GetReadOnlyResources(stage)        -> bound SRVs / textures
            .GetReadWriteResources(stage)       -> bound UAVs / storage
            .GetConstantBlocks(stage)           -> constant buffer bindings
            .GetViewport(index)                 -> viewport rect
            .GetScissor(index)                  -> scissor rect
            .GetPrimitiveTopology()             -> topology enum
            .GetColorBlends()                   -> per-target blend state
            .GetStencilFaces()                  -> (front, back) stencil state
            .GetIBuffer()                       -> index buffer binding
            .GetVBuffers()                      -> vertex buffer bindings

    Raw data access:
        controller.GetBufferData(resourceId, offset, length) -> bytes
        controller.GetTextureData(resourceId, subresource)   -> bytes

    Resource metadata:
        controller.GetTextures()  -> list of TextureDescription
        controller.GetBuffers()   -> list of BufferDescription
        controller.GetResources() -> list of ResourceDescription

    ACTION TREE
    ===========
    The action list is hierarchical. Debug markers (PushMarker/PopMarker)
    create parent-child relationships. Actual GPU work lives in leaf nodes.

    To find all draw calls, recurse through children:

        def find_draws(actions):
            draws = []
            for a in actions:
                if a.flags & rd.ActionFlags.Drawcall:
                    draws.append(a)
                draws.extend(find_draws(a.children))
            return draws

    Use .next and .previous for sequential traversal within a level.

    KEY ENUMS
    =========
    Import as `rd.EnumName.Value` (the `rd` module is pre-loaded as
    `import renderdoc as rd`).

    ShaderStage:
        Vertex, Hull, Domain, Geometry, Pixel, Compute
        (Fragment is an alias for Pixel)

    ActionFlags (bitmask -- use & to test):
        Drawcall, Dispatch, Clear, Copy, Resolve, Present,
        PushMarker, PopMarker, SetMarker,
        Indexed, Instanced, Indirect,
        ClearColor, ClearDepthStencil,
        BeginPass, EndPass

    MeshDataStage:
        VSIn, VSOut

    AVAILABLE GLOBALS AND UTILITIES
    ===============================
    These are pre-loaded in the execution environment:

    Modules:
        rd           -- the renderdoc module (import renderdoc as rd)
        qrd          -- the qrenderdoc module (UI types)
        pyrenderdoc  -- the capture context global
        serialize    -- type serialization (see below)

    Functions:
        inspect(obj)
            Introspect any RenderDoc object to discover its methods,
            properties, and their docstrings. Use this when you are
            unsure what an object supports. Returns structured info.

        diff_state(eid_a, eid_b)
            Diff pipeline state between two events. Returns a structured
            diff showing what changed (shaders, render targets, blend,
            depth, bound resources, etc.).

        interpret_buffer(data, fmt)
            Decode raw bytes from GetBufferData into typed values.
            fmt is a ResourceFormat object or a dict with keys:
            component_type, component_count, component_byte_width.

        summarize_data(values)
            Compute min, max, mean, count, nan_count, inf_count over
            a flat list of numbers. Quick buffer/texture inspection.

        goto_event(eid)
            Navigate the RenderDoc UI to a specific event.

        view_texture(resource_id)
            Open the texture viewer for a resource.

        highlight_drawcall(eid)
            Navigate the event browser to highlight a draw call.

    Serialization:
        The `serialize` module converts RenderDoc C++ types to plain
        dicts for JSON transport. Useful functions:
            serialize.pipeline_state(state)    -> dict
            serialize.action_description(act)  -> dict
            serialize.shader_reflection(refl)  -> dict
            serialize.texture_description(tex) -> dict
            serialize.buffer_description(buf)  -> dict
            serialize.format_description(fmt)  -> dict
            serialize.resource_id(rid)         -> str
            serialize.cbuffer_variables(vars, data) -> list of dicts

    RETURN CONVENTION
    =================
    - The last expression in your code block is captured and returned as
      the result. You do not need to assign it or call return.
    - Return dicts or lists for structured data.
    - print() output is also captured and included in the response.
    - If BlockInvoke's callback returns a value, you must capture it:
          results = []
          def work(controller):
              ...
              results.append(data)
          pyrenderdoc.Replay().BlockInvoke(work)
          results[0]  # <-- last expression, becomes the result

    EXAMPLES
    ========

    1. List all draw calls in the frame:

        results = []
        def work(controller):
            def find_draws(actions):
                draws = []
                for a in actions:
                    if a.flags & rd.ActionFlags.Drawcall:
                        draws.append({
                            "eventId": a.eventId,
                            "name": a.customName or f"Draw({a.numIndices})",
                        })
                    draws.extend(find_draws(a.children))
                return draws
            results.extend(find_draws(controller.GetRootActions()))
        pyrenderdoc.Replay().BlockInvoke(work)
        results

    2. Inspect pipeline state at a specific event:

        results = []
        def work(controller):
            controller.SetFrameEvent(42, True)
            state = controller.GetPipelineState()
            results.append(serialize.pipeline_state(state))
        pyrenderdoc.Replay().BlockInvoke(work)
        results[0]

    3. Read constant buffer data for the pixel shader at event 100:

        import struct
        results = []
        def work(controller):
            controller.SetFrameEvent(100, True)
            state = controller.GetPipelineState()
            cbs = state.GetConstantBlocks(rd.ShaderStage.Pixel)
            if cbs and cbs[0].descriptor.resource != rd.ResourceId.Null():
                rid  = cbs[0].descriptor.resource
                data = controller.GetBufferData(rid, 0, 256)
                refl = state.GetShaderReflection(rd.ShaderStage.Pixel)
                if refl and refl.constantBlocks:
                    results.append(
                        serialize.cbuffer_variables(
                            refl.constantBlocks[0].variables, data
                        )
                    )
        pyrenderdoc.Replay().BlockInvoke(work)
        results[0] if results else "no constant buffers bound"

    ERRORS
    ======
    On failure, the response includes:
    - traceback:    full formatted traceback
    - failing_line: the specific source line that failed
    - hints:        contextual suggestions (e.g., "did you call
                    SetFrameEvent before querying pipeline state?")

    If you get an AttributeError, use inspect(obj) to see what is
    actually available, or use the search_api tool to look up the
    correct method name.
    """
    return _client.send("eval", {"code": code})


# --- search_api ---

@mcp.tool()
def search_api(query: str) -> dict:
    """Search the RenderDoc Python API reference by name or concept.

    Use this tool for discovery: finding what API exists for a task,
    looking up exact method signatures, checking parameter types, or
    exploring enum values. The index is built by introspecting the live
    renderdoc module, so it always matches the running RenderDoc version.

    query: A class name, method name, enum name, or concept keyword.
           Examples: "SetFrameEvent", "ShaderStage", "GetBufferData",
                     "constant buffer", "blend".

    Returns a JSON array of matching entries ranked by relevance. Each entry:
        name:      Fully qualified name (e.g., "ReplayController.SetFrameEvent")
        kind:      "class", "method", "property", "enum", or "enum_value"
        doc:       Full RST-formatted docstring with param/type/return info
        signature: Method signature string, if applicable (e.g., "(eventId, force)")
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
        return _enrich_instances(_client.discover_instances())
    elif action == "connect":
        if port is None:
            return {"error": "port is required for connect"}
        _client.connect(port)
        info   = _client.send("instance_info", {})
        others = [
            inst for inst in _client.discover_instances()
            if inst["port"] != port
        ]
        if others:
            info["other_instances"] = _enrich_instances(others)["instances"]
        return info
    elif action == "disconnect":
        _client.disconnect()
        return {"status": "disconnected"}
    else:
        return {"error": f"unknown action: {action}"}


def _enrich_instances(instances: list[dict]) -> dict:
    """Probe each discovered instance for metadata.

    Attempts a temporary connection to each instance to fetch instance_info
    (capture state, API type, etc.). Falls back to port-only info if the
    probe fails.
    """
    enriched = []
    for inst in instances:
        port = inst["port"]
        # If we are already connected to this port, query directly.
        if _client._port == port and _client._sock is not None:
            try:
                info = _client.send("instance_info", {})
                if info.get("ok") and "data" in info:
                    enriched.append(info["data"])
                else:
                    enriched.append({"port": port})
            except Exception:
                enriched.append({"port": port})
            continue

        # Otherwise, open a temporary connection to probe.
        probe = RenderDocClient()
        try:
            probe.connect(port)
            info = probe.send("instance_info", {})
            if info.get("ok") and "data" in info:
                enriched.append(info["data"])
            else:
                enriched.append({"port": port})
        except Exception:
            enriched.append({"port": port})
        finally:
            probe.disconnect()

    return {"instances": enriched}
