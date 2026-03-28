"""MCP tool definitions for eval, search_api, instance, get_texture, and task management."""

from __future__ import annotations

import base64
import io
import json
import struct
import threading
import time
import uuid

from PIL import Image as PILImage

from mcp.server.fastmcp.utilities.types import Image as MCPImage
from mcp.types import TextContent

from server.app import mcp
from server.client import ConnectionPool

_pool = ConnectionPool()

# ---------------------------------------------------------------------------
# Async task registry
# ---------------------------------------------------------------------------

_tasks: dict[str, dict] = {}
_tasks_lock = threading.Lock()


def _make_task_id() -> str:
    return uuid.uuid4().hex[:12]


def _run_async(task_id: str, cmd: str, params: dict, alias: str | None,
               timeout: float) -> None:
    """Worker target: run a pool command and store the result in _tasks."""
    try:
        result = _pool.send(cmd, params, alias=alias, read_timeout=timeout)
        with _tasks_lock:
            _tasks[task_id]["status"] = "done"
            _tasks[task_id]["result"] = result
    except Exception as e:
        with _tasks_lock:
            _tasks[task_id]["status"] = "error"
            _tasks[task_id]["error"]  = str(e)


def _start_task(cmd: str, params: dict, alias: str | None,
                timeout: float = 300.0) -> str:
    """Register a task, fire it on a daemon thread, return the task_id."""
    task_id = _make_task_id()

    with _tasks_lock:
        _tasks[task_id] = {
            "status"    : "pending",
            "started_at": time.monotonic(),
            "alias"     : alias,
        }

    t = threading.Thread(
        target=_run_async,
        args=(task_id, cmd, params, alias, timeout),
        daemon=True,
    )
    t.start()
    return task_id


# --- eval ---

@mcp.tool(name="Eval")
def eval(code: str, instance: str | None = None,
         async_mode: bool = False, timeout: float = 300.0) -> dict:
    """Execute Python code in a live RenderDoc replay session.

    This is your primary interface for all GPU capture inspection, analysis,
    and debugging. Code runs inside RenderDoc's embedded Python interpreter
    with full access to the replay engine.

    ACCESS MODEL
    ============
    The global `ctx` (HandlerContext) provides thread-safe replay access.
    To query replay state, use ctx.replay(callback):

        def work(controller):
            controller.SetFrameEvent(eventId, True)
            state = controller.GetPipelineState()
            # ... query state ...
            return result
        ctx.replay(work)

    ctx.replay() runs your callback on the replay thread with a
    ReplayController argument, returns the callback's return value, and
    properly propagates exceptions. You MUST use this pattern for any
    ReplayController access.

    CURSOR MODEL
    ============
    The replay engine maintains a cursor position in the frame's event
    timeline. All state queries return state AT the current cursor position.

    - controller.SetFrameEvent(eventId, True) moves the cursor.
    - You MUST call SetFrameEvent before calling GetPipelineState or any
      other state query. Forgetting this is the most common mistake.
      WARNING: GetPipelineState() will NOT error without SetFrameEvent —
      it silently returns stale state from whatever event was last active.
      Always call SetFrameEvent first inside every ctx.replay() callback.
    - The second argument (True) forces full pipeline state resolution.
    - goto_event(eid) navigates the RenderDoc UI to an event. It does NOT
      move the replay cursor. Only SetFrameEvent(eventId, True) inside a
      ctx.replay() callback sets the replay cursor. Pipeline state queries
      always reflect the last SetFrameEvent call, not goto_event.

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
            .GetName(ctx.structured_file) -- formatted display name
                (e.g., "vkCmdDrawIndexed(36, 1, 0, 0, 0)"). Always
                prefer this over customName for human-readable names.
            .numIndices, .numInstances, .indexOffset, .baseVertex
            .dispatchDimension -- [x, y, z] for compute dispatches

    Pipeline state (after SetFrameEvent):
        controller.GetPipelineState() -> PipeState
        PipeState is API-agnostic. Key methods:
            .GetShader(stage)                   -> ResourceId
                WARNING: GetShader(Compute) at a graphics draw call
                returns the stale shader from the last dispatch, not
                Null(). The serialize module filters this automatically,
                but raw GetShader calls will see the stale ID. Check
                the action's flags to know whether CS is relevant.
            .GetShaderReflection(stage)         -> ShaderReflection
            .GetOutputTargets()                 -> list of Descriptor (direct)
                rt.resource, rt.format, rt.firstMip, rt.numMips, etc.
            .GetDepthTarget()                   -> Descriptor (direct)
                depth.resource, depth.format, etc.
            .GetReadOnlyResources(stage)        -> list of UsedDescriptor
            .GetReadWriteResources(stage)       -> list of UsedDescriptor
            .GetConstantBlocks(stage)           -> list of UsedDescriptor
                UsedDescriptor wraps a Descriptor in .descriptor:
                    ud.descriptor.resource   -- ResourceId
                    ud.descriptor.byteOffset -- offset in buffer
                    ud.descriptor.byteSize   -- size in bytes
                Note: on Vulkan, VK_WHOLE_SIZE maps to byteSize =
                18446744073709551615 (u64::MAX). This does NOT mean the
                buffer is that large. Read the buffer's actual length
                from controller.GetBuffers() and clamp accordingly.

                UsedDescriptor also has an .access (DescriptorAccess) field:
                    ud.access.arrayElement   -- index into the descriptor array
                    ud.access.descriptorStore -- ResourceId of the backing store
                    ud.access.stage          -- ShaderStage that accessed this
                    ud.access.type           -- DescriptorType enum
                For bindless renderers, GetReadWriteResources/GetReadOnlyResources
                return only the descriptors actually accessed by the draw call.
                Use ud.access.arrayElement to map back to the original array index.

        Shader reflection containers:
            refl.constantBlocks[i] is a ConstantBlock:
                .name, .fixedBindNumber, .fixedBindSetOrSpace, .variables
            refl.readOnlyResources[i] / readWriteResources[i] is a ShaderResource:
                .name, .fixedBindNumber, .fixedBindSetOrSpace
            .GetViewport(index)                 -> viewport rect
            .GetScissor(index)                  -> scissor rect
            .GetPrimitiveTopology()             -> topology enum
            .GetColorBlends()                   -> per-target blend state
            .GetStencilFaces()                  -> (front, back) stencil state
            .GetIBuffer()                       -> index buffer binding
            .GetVBuffers()                      -> vertex buffer bindings

        Depth/stencil test configuration (enable, writes, compare function)
        is NOT available through the API-agnostic PipeState. Use the
        API-specific state object instead:
            controller.GetVulkanPipelineState().depthStencil
            controller.GetD3D11PipelineState().outputMerger.depthStencilState

        Push constant data (Vulkan only):
            controller.GetVulkanPipelineState().pushconsts -> bytes
            Decode with struct.unpack. Typically contains descriptor
            indices or buffer offsets in bindless renderers.

    Raw data access:
        controller.GetBufferData(resourceId, offset, length) -> bytes
        controller.GetTextureData(resourceId, subresource)   -> bytes
            subresource is an rd.Subresource(mip, slice, sample).
            For the base mip of the first slice: rd.Subresource(0, 0, 0).

    Resource metadata:
        controller.GetTextures()  -> list of TextureDescription
            Note: TextureDescription does not carry names. Use
            get_resource_name(resource_id) to look up human-readable names.
        controller.GetBuffers()   -> list of BufferDescription
        controller.GetResources() -> list of ResourceDescription

        Note: ResourceFormat uses .Name() (method) not .name (property)
        for the format name string. The serialize module handles this
        automatically.

        Note: ResourceId is a one-way opaque handle. You can convert to
        int via int(rid) or to string via serialize.resource_id(rid),
        but there is no way to reconstruct a ResourceId from an integer.
        Always hold onto live ResourceId objects within your ctx.replay()
        callback rather than serializing and trying to reconstruct later.

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
        BeginPass, EndPass, PassBoundary
        Note: PassBoundary marks both Vulkan render pass boundaries
        AND command buffer boundaries. To distinguish, check the
        action name (e.g., "vkCmdBeginRenderPass" vs
        "vkBeginCommandBuffer").

    MeshDataStage:
        VSIn, VSOut

    SHADER REFLECTION TYPES
    =======================
    ShaderReflection.constantBlocks[i].variables[j].type is a
    ShaderConstantType with:
        .baseType   -- VarType enum (Float, Int, UInt, etc.)
        .rows       -- number of rows (1 for scalars/vectors)
        .columns    -- number of columns
        .elements   -- array length (0 if not an array)
        .members    -- list of sub-variables (for structs)

    AVAILABLE GLOBALS AND UTILITIES
    ===============================
    These are pre-loaded in the execution environment:

    Modules:
        rd           -- the renderdoc module (import renderdoc as rd)
        qrd          -- the qrenderdoc module (UI types)
        ctx          -- HandlerContext:
                        ctx.replay(callback) for replay access
                        ctx.structured_file  for ActionDescription.GetName()
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

        action_flags(flags)
            Decode an ActionDescription.flags int into a list of flag name strings.

        goto_event(eid)
            Navigate the RenderDoc UI to a specific event.

        view_texture(resource_id)
            Open the texture viewer for a resource.

        save_texture(resource_id, path, mip=0, slice_index=0, event_id=None)
            Save a texture or render target to a PNG file on disk and return
            {"ok": bool, "path": str}. The returned path can be passed
            directly to the host's file-reading tool (e.g. Claude Code's
            Read tool) for visual inspection — no Pillow dependency required.
            Set event_id to seek the replay cursor before saving, which is
            required when the resource is only bound as a render target at a
            specific draw call. Useful for capturing stereo render targets by
            saving each eye's output to separate paths and comparing visually.

            Example — save both eyes of a stereo frame:
                targets = state.GetOutputTargets()
                save_texture(targets[0].resource, "/tmp/left_eye.png",
                             event_id=eid)
                save_texture(targets[1].resource, "/tmp/right_eye.png",
                             event_id=eid)

        highlight_drawcall(eid)
            Alias for goto_event. Both call SetEventID under the hood.
            Use whichever name reads better in context.

        get_resource_name(resource_id)
            Look up the human-readable name of a resource by its ResourceId.
            Names come from ResourceDescription, not TextureDescription or
            BufferDescription.

        get_draw_calls()
            Collect all leaf draw calls in the frame. Returns a flat list
            of {"eventId": int, "name": str}. Handles the recursive action
            tree walk internally. Works both inside and outside ctx.replay().

        get_all_actions()
            Flat walk of the entire action tree (markers, draws, dispatches,
            clears, copies, etc.). Returns a list of {"eventId": int,
            "name": str, "flags": [str]}. Useful for frame structure
            exploration. Works both inside and outside ctx.replay().

        describe_draw(eventId=eid)
            One-shot comprehensive summary of a draw call. Returns event_id,
            name, shaders, render_targets, depth_target, draw_params,
            vertex_buffers, index_buffer, and push_constants in a single
            dict. Works both inside and outside ctx.replay().

        decode_push_constants(controller, stage)
            Decode Vulkan push constant bytes against shader reflection.
            Must be called inside a ctx.replay() callback. Returns a dict
            with stage name, raw_hex string, and decoded variables list.

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
    - ctx.replay(callback) returns the callback's return value directly:
          def work(controller):
              ...
              return data
          ctx.replay(work)  # <-- last expression, becomes the result

    EXAMPLES
    ========

    1. List all draw calls in the frame:

        get_draw_calls()

       Or manually (equivalent to what get_draw_calls does internally):

        def work(controller):
            def find_draws(actions):
                draws = []
                for a in actions:
                    if a.flags & rd.ActionFlags.Drawcall:
                        draws.append({
                            "eventId": a.eventId,
                            "name": a.GetName(ctx.structured_file),
                        })
                    draws.extend(find_draws(a.children))
                return draws
            return find_draws(controller.GetRootActions())
        ctx.replay(work)

    2. Inspect pipeline state at a specific event:

        def work(controller):
            controller.SetFrameEvent(42, True)
            state = controller.GetPipelineState()
            return serialize.pipeline_state(state)
        ctx.replay(work)

    3. Read constant buffer data for the pixel shader at event 100:

        import struct
        def work(controller):
            controller.SetFrameEvent(100, True)
            state = controller.GetPipelineState()
            cbs = state.GetConstantBlocks(rd.ShaderStage.Pixel)
            if cbs and cbs[0].descriptor.resource != rd.ResourceId.Null():
                desc = cbs[0].descriptor
                data = controller.GetBufferData(desc.resource, desc.byteOffset, desc.byteSize)
                refl = state.GetShaderReflection(rd.ShaderStage.Pixel)
                if refl and refl.constantBlocks:
                    return serialize.cbuffer_variables(
                        refl.constantBlocks[0].variables, data
                    )
            return "no constant buffers bound"
        ctx.replay(work)

    4. Discover what methods a pipeline state object has:

        def work(controller):
            controller.SetFrameEvent(42, True)
            state = controller.GetPipelineState()
            return inspect(state)
        ctx.replay(work)

    5. Summarize a specific draw call:

        describe_draw(eventId=42)

    6. Decode push constants for the vertex shader at an event:

        def work(controller):
            controller.SetFrameEvent(100, True)
            return decode_push_constants(controller, rd.ShaderStage.Vertex)
        ctx.replay(work)

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

    PERFORMANCE AND STABILITY
    =========================
    RenderDoc's replay engine was designed for interactive, one-event-at-
    a-time use — NOT for automated/agentic batch processing. Violating
    these constraints causes application freezes or crashes with NO
    recovery (the GUI, MCP server, and Python environment all lock up).

    SetFrameEvent is EXPENSIVE:
        Each call triggers a full GPU frame replay from event 0 to the
        target event (re-executing every GPU command), followed by a
        blocking vkQueueWaitIdle with no timeout. This is the single
        most expensive operation available and cannot be cancelled once
        started.

    Hard rules:
    - ONE SetFrameEvent call per ctx.replay() callback, maximum.
      Multiple calls in one callback occupy the replay thread for the
      sum of all replays with no interleaving. If any replay hangs
      (driver timeout, device lost), the entire application freezes
      permanently.
    - NEVER call LoadCapture from eval. It re-enters the replay
      lifecycle while the replay thread is active. Guaranteed deadlock.
    - NEVER issue rapid-fire ctx.replay() calls in a tight loop.
      Each call blocks the replay thread. Allow the system to breathe.

    Safe patterns:
    - GetPipelineState(), GetTextures(), GetBuffers(), GetResources()
      after a SetFrameEvent are cheap data lookups — call freely.
    - Use force=False in SetFrameEvent(eid, False) when you don't need
      full state resolution and the cursor may already be at that event.
    - Cache results. Pipeline state does not change between queries for
      the same event — query once and reuse.
    - For multi-event analysis, issue SEPARATE ctx.replay() calls for
      each event rather than looping inside one callback.
    - get_draw_calls(), get_all_actions(), and describe_draw() are
      designed to be safe single-replay-per-call utilities.

    MULTIPLE INSTANCES
    ==================
    When more than one RenderDoc instance is connected, use the
    instance parameter to target a specific one:

        Eval(code="get_draw_calls()", instance="baseline")
        Eval(code="get_draw_calls()", instance="broken")

    Omit instance= when only one connection is active (backward-
    compatible). Use Instance(action='list') to see available aliases.

    ASYNC MODE
    ==========
    For long-running operations (large buffer scans, full-frame pixel
    diffs, etc.) that may exceed the normal 30-second timeout, set
    async_mode=True. The call returns immediately with a task_id; use
    Task(action="poll", task_id=...) to retrieve the result.

        t = Eval(code="...", async_mode=True, timeout=120)
        # ... do other work ...
        Task(action="poll", task_id=t["task_id"])

    timeout controls the socket read deadline for the background call
    (default 300s). Ignored when async_mode=False.

    To fire work at two instances in parallel and collect both results:

        t1 = Eval(code="describe_draw(100)", instance="baseline", async_mode=True)
        t2 = Eval(code="describe_draw(100)", instance="broken",   async_mode=True)
        Task(action="poll", task_id=t1["task_id"])
        Task(action="poll", task_id=t2["task_id"])
    """
    if async_mode:
        try:
            task_id = _start_task("eval", {"code": code},
                                  alias=instance, timeout=timeout)
            return {"task_id": task_id, "status": "pending"}
        except (ConnectionError, KeyError) as e:
            return {"ok": False, "error": str(e)}

    try:
        return _pool.send("eval", {"code": code}, alias=instance)
    except (TimeoutError, OSError) as e:
        return {
            "ok"    : False,
            "error" : {
                "message" : f"Connection to RenderDoc timed out: {e}",
                "hints"   : [
                    "use Instance(action='list') to check connectivity",
                    "RenderDoc may have closed or the capture may have changed",
                ],
            },
        }
    except (ConnectionError, KeyError) as e:
        return {"ok": False, "error": {"message": str(e)}}


# --- search_api ---

@mcp.tool(name="Search-API")
def search_api(query: str, instance: str | None = None) -> dict:
    """Search the RenderDoc Python API reference by name or concept.

    Use this tool for discovery: finding what API exists for a task,
    looking up exact method signatures, checking parameter types, or
    exploring enum values. The index is built by introspecting the live
    renderdoc module, so it always matches the running RenderDoc version.

    query: A class name, method name, enum name, or concept keyword.
           Examples: "SetFrameEvent", "ShaderStage", "GetBufferData",
                     "constant buffer", "blend".
    instance: Optional alias of the RenderDoc instance to query. The
              API surface is identical across instances running the same
              build, so this is only needed when instances differ in
              RenderDoc version.

    Returns a JSON array of matching entries ranked by relevance. Each entry:
        name:      Fully qualified name (e.g., "ReplayController.SetFrameEvent")
        kind:      "class", "method", "property", "enum", or "enum_value"
        doc:       Full RST-formatted docstring with param/type/return info
        signature: Method signature string, if applicable (e.g., "(eventId, force)")
    """
    try:
        return _pool.send("api_index", {"query": query}, alias=instance)
    except (ConnectionError, KeyError) as e:
        return {"ok": False, "error": {"message": str(e)}}


# --- get_texture ---

@mcp.tool(name="Get-Texture")
def get_texture(
    resource_id  : str,
    instance     : str | None = None,
    event_id     : int | None = None,
    mip          : int   = 0,
    slice        : int   = 0,
    sample       : int   = 0,
    max_size     : int   = 2048,
    region_x     : int | None = None,
    region_y     : int | None = None,
    region_w     : int | None = None,
    region_h     : int | None = None,
    channel      : int   = -1,
    black_point  : float = 0.0,
    white_point  : float = 1.0,
) -> list:
    """Capture a texture or render target as a viewable image.

    Returns the texture as a PNG image alongside its metadata. Use this
    to visually inspect render targets, depth buffers, textures, or any
    other image resource in the current capture.

    Raw texture bytes are read via GetTextureData and converted to a
    viewable PNG on the server side. HDR and float textures are mapped
    to LDR using the black_point/white_point range.

    Supported formats: R8G8B8A8, B8G8R8A8 (UNORM/SRGB), R16/R16G16B16A16
    (Float), R32/R32G32B32A32 (Float), and single-channel 8-bit. Block-
    compressed formats (BC1-7) are not supported — use RenderDoc's
    texture viewer for those.

    resource_id: Texture resource ID string, as returned by pipeline
                 state queries, describe_draw, or GetTextures().
    event_id:    Event ID to replay to before reading. Required for
                 render targets (their contents depend on replay cursor
                 position). Omit for source textures. NOTE: this calls
                 SetFrameEvent internally — see the Eval tool's
                 PERFORMANCE AND STABILITY section for constraints.
    mip:         Mip level to capture (default 0 = full resolution).
    slice:       Array slice or cube face index (default 0).
    sample:      Multisample sample index (default 0).
    max_size:    Maximum width or height in pixels. Images larger than
                 this are downscaled preserving aspect ratio. Set to 0
                 to return at native resolution. Default 2048.
    region_x:    Left edge of a subregion to crop (texel coords at the
                 selected mip level). All four region_* params must be
                 set together, or all omitted for the full image.
    region_y:    Top edge of the subregion.
    region_w:    Width of the subregion.
    region_h:    Height of the subregion.
    channel:     Extract a single channel as grayscale (-1 = all
                 channels, 0 = R, 1 = G, 2 = B, 3 = A). Default -1.
    black_point: Low end of the value range mapped to black (default
                 0.0). For HDR textures, values below this are clamped.
    white_point: High end of the value range mapped to white (default
                 1.0). For HDR textures, values above this are clamped.
    """
    try:
        resp = _pool.send("get_texture", {
            "resource_id" : resource_id,
            "event_id"    : event_id,
            "mip"         : mip,
            "slice"       : slice,
            "sample"      : sample,
        }, alias=instance)
    except (TimeoutError, OSError) as e:
        return [TextContent(
            type = "text",
            text = json.dumps({
                "ok"    : False,
                "error" : f"connection to RenderDoc timed out: {e}",
            }),
        )]

    if not resp.get("ok"):
        return [TextContent(
            type = "text",
            text = json.dumps(resp),
        )]

    data     = resp["data"]
    raw      = base64.b64decode(data["raw"])
    fmt      = data["format"]
    width    = data["mip_width"]
    height   = data["mip_height"]
    metadata = {k: v for k, v in data.items() if k != "raw"}

    # Decode raw GPU bytes into a Pillow Image.
    img = _decode_texture(raw, width, height, fmt, black_point, white_point)
    if img is None:
        fmt_name = fmt.get("name", "unknown")
        return [TextContent(
            type = "text",
            text = json.dumps({
                "ok"    : False,
                "error" : f"unsupported texture format: {fmt_name}. use RenderDoc's texture viewer instead.",
            }),
        )]

    # Extract single channel as grayscale.
    if channel >= 0:
        bands = img.split()
        if channel < len(bands):
            img = bands[channel].convert("L")
            metadata["channel_extracted"] = channel

    # Crop subregion.
    has_region = all(v is not None for v in (region_x, region_y, region_w, region_h))
    if has_region:
        box = (region_x, region_y, region_x + region_w, region_y + region_h)
        img = img.crop(box)
        metadata["region"] = {"x": region_x, "y": region_y, "w": region_w, "h": region_h}

    # Downscale if the image exceeds max_size on either axis.
    if max_size > 0:
        w, h = img.size
        if w > max_size or h > max_size:
            scale        = max_size / max(w, h)
            new_w, new_h = int(w * scale), int(h * scale)
            img          = img.resize((new_w, new_h), PILImage.Resampling.LANCZOS)
            metadata["scaled"] = {"from": [w, h], "to": [new_w, new_h]}

    # Encode to PNG.
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    png_bytes = buf.getvalue()

    metadata_text = json.dumps(metadata, indent=2)

    return [
        TextContent(type="text", text=metadata_text),
        MCPImage(data=png_bytes, format="png").to_image_content(),
    ]


def _decode_texture(raw: bytes, width: int, height: int, fmt: dict, black_point: float, white_point: float) -> PILImage.Image | None:
    """Decode raw texture bytes into a Pillow Image.

    Handles RGBA/BGRA 8-bit, half-float, and float formats. Applies
    black_point/white_point range mapping for float data. Returns None
    for unsupported formats.

    raw         -- Raw bytes from GetTextureData.
    width       -- Texture width at the target mip level.
    height      -- Texture height at the target mip level.
    fmt         -- Format dict with name, component_type, component_count,
                   component_byte_width.
    black_point -- Low end of the value range mapped to 0.
    white_point -- High end of the value range mapped to 255.
    """
    comp_type  = fmt.get("component_type", "")
    comp_count = fmt.get("component_count", 0)
    comp_bytes = fmt.get("component_byte_width", 0)
    fmt_name   = fmt.get("name", "")
    pixel_count = width * height

    is_bgra = fmt_name.startswith("B8G8R8A8") or fmt_name.startswith("B8G8R8X8")

    # 8-bit UNORM / SRGB — direct byte data.
    if comp_bytes == 1 and comp_type in ("UNorm", "UNormSRGB"):
        if comp_count == 4:
            img = PILImage.frombytes("RGBA", (width, height), raw)
            if is_bgra:
                r, g, b, a = img.split()
                img = PILImage.merge("RGBA", (b, g, r, a))
            return img
        elif comp_count == 3:
            img = PILImage.frombytes("RGB", (width, height), raw)
            if fmt_name.startswith("B8G8R8"):
                r, g, b = img.split()
                img = PILImage.merge("RGB", (b, g, r))
            return img
        elif comp_count == 2:
            # RG → grayscale from R channel.
            stride = 2 * width
            r_bytes = bytearray(pixel_count)
            for y in range(height):
                for x in range(width):
                    r_bytes[y * width + x] = raw[y * stride + x * 2]
            return PILImage.frombytes("L", (width, height), bytes(r_bytes))
        elif comp_count == 1:
            return PILImage.frombytes("L", (width, height), raw)

    # Float formats — unpack and range-map to 8-bit.
    if comp_type in ("Float",) and comp_bytes in (2, 4):
        struct_fmt = "e" if comp_bytes == 2 else "f"
        total_floats = pixel_count * comp_count
        expected_bytes = total_floats * comp_bytes

        if len(raw) < expected_bytes:
            return None

        floats = struct.unpack(f"<{total_floats}{struct_fmt}", raw[:expected_bytes])

        # Map [black_point, white_point] → [0, 255].
        scale = white_point - black_point
        if scale <= 0:
            scale = 1.0

        def to_byte(v):
            normalized = (v - black_point) / scale
            return max(0, min(255, int(normalized * 255 + 0.5)))

        if comp_count == 4:
            pixels = bytearray(pixel_count * 4)
            for i in range(pixel_count):
                base = i * 4
                pixels[base]     = to_byte(floats[base])
                pixels[base + 1] = to_byte(floats[base + 1])
                pixels[base + 2] = to_byte(floats[base + 2])
                pixels[base + 3] = to_byte(floats[base + 3])
            return PILImage.frombytes("RGBA", (width, height), bytes(pixels))

        elif comp_count == 3:
            pixels = bytearray(pixel_count * 3)
            for i in range(pixel_count):
                src = i * 3
                dst = i * 3
                pixels[dst]     = to_byte(floats[src])
                pixels[dst + 1] = to_byte(floats[src + 1])
                pixels[dst + 2] = to_byte(floats[src + 2])
            return PILImage.frombytes("RGB", (width, height), bytes(pixels))

        elif comp_count == 2:
            pixels = bytearray(pixel_count)
            for i in range(pixel_count):
                pixels[i] = to_byte(floats[i * 2])
            return PILImage.frombytes("L", (width, height), bytes(pixels))

        elif comp_count == 1:
            pixels = bytearray(pixel_count)
            for i in range(pixel_count):
                pixels[i] = to_byte(floats[i])
            return PILImage.frombytes("L", (width, height), bytes(pixels))

    return None


# --- instance ---

@mcp.tool(name="Instance")
def instance(
    action: str,
    port: int | None = None,
    capture: str | None = None,
    alias: str | None = None,
) -> dict:
    """Manage connections to running RenderDoc instances.

    action: One of "list", "connect", "disconnect", "set_default".

    LIST
    ----
    Instance(action="list")
        Probe all ports and return every running RenderDoc instance with
        its port, capture path, and alias (if already connected).

    CONNECT
    -------
    Instance(action="connect", port=19876)
        Connect to a specific port. An alias is auto-derived from the
        capture filename stem (e.g. "clean_build" from clean_build.rdc),
        or "port_19876" if no capture is loaded.

    Instance(action="connect", port=19876, alias="baseline")
        Connect and assign an explicit alias.

    Instance(action="connect", capture="clean_build", alias="baseline")
        Find the instance whose capture path contains "clean_build"
        (case-insensitive substring match) and connect to it. Raises if
        zero or more than one instance matches.

    DISCONNECT
    ----------
    Instance(action="disconnect", alias="baseline")
        Close the named connection. Other connections are unaffected.
        Omit alias to disconnect the sole active connection.

    SET_DEFAULT
    -----------
    Instance(action="set_default", alias="baseline")
        Set which instance Eval targets when instance= is omitted.
        Unnecessary when only one connection is active.

    TYPICAL COMPARISON WORKFLOW
    ---------------------------
        Instance(action="list")
        Instance(action="connect", capture="clean",     alias="baseline")
        Instance(action="connect", capture="artifacts", alias="broken")
        Eval(code="describe_draw(eid)", instance="baseline")
        Eval(code="describe_draw(eid)", instance="broken")
    """
    if action == "list":
        instances = _pool.discover_instances(enrich=True)
        connected = _pool.connection_info()
        return {"instances": instances, "connected": connected}

    elif action == "connect":
        if port is None and capture is None:
            return {"ok": False, "error": "provide port= or capture="}
        try:
            info = _pool.connect(alias=alias, port=port, capture=capture)
            return {"ok": True, **info,
                    "connected": _pool.connection_info()}
        except (ValueError, ConnectionError, OSError) as e:
            return {"ok": False, "error": str(e)}

    elif action == "disconnect":
        target = alias or (
            _pool.aliases[0] if len(_pool.aliases) == 1 else None
        )
        if target is None:
            if not _pool.aliases:
                return {"ok": False, "error": "no active connections"}
            return {
                "ok": False,
                "error": (
                    "multiple connections active; "
                    "specify alias= to choose one"
                ),
            }
        try:
            _pool.disconnect(target)
            return {"ok": True, "disconnected": target,
                    "connected": _pool.connection_info()}
        except KeyError as e:
            return {"ok": False, "error": str(e)}

    elif action == "set_default":
        if alias is None:
            return {"ok": False, "error": "alias= required for set_default"}
        try:
            _pool.set_default(alias)
            return {"ok": True, "default": alias}
        except KeyError as e:
            return {"ok": False, "error": str(e)}

    else:
        return {"ok": False, "error": f"unknown action: {action!r}"}


# --- task ---

@mcp.tool(name="Task")
def task(action: str, task_id: str | None = None) -> dict:
    """Manage async tasks started by Eval(async_mode=True).

    action: One of "poll", "cancel", "list".

    POLL
    ----
    Task(action="poll", task_id="abc123")
        Check whether an async task has completed.

        Returns one of:

            {"task_id": "...", "status": "pending", "elapsed_s": 1.2}
                Still running. Poll again later.

            {"task_id": "...", "status": "done", "elapsed_s": 4.7,
             "result": {...}}
                Completed. "result" is identical to what a synchronous
                Eval call would have returned.

            {"task_id": "...", "status": "error", "elapsed_s": 2.1,
             "error": "..."}
                Failed (connection dropped, exception in eval, etc.).

            {"ok": False, "error": "unknown task_id"}
                Never issued or already collected.

        Completed and errored tasks are removed on first poll
        (collect-once semantics).

    CANCEL
    ------
    Task(action="cancel", task_id="abc123")
        Remove a pending or completed task from the registry without
        collecting its result. No-op if already collected.

    LIST
    ----
    Task(action="list")
        Return all tasks currently in the registry (pending and any
        that completed but have not yet been collected). Useful for
        checking what is still running after firing multiple async evals.
    """
    if action == "poll":
        if task_id is None:
            return {"ok": False, "error": "task_id= required for poll"}
        with _tasks_lock:
            entry = _tasks.get(task_id)
            if entry is None:
                return {"ok": False, "error": "unknown task_id"}

            status  = entry["status"]
            elapsed = round(time.monotonic() - entry["started_at"], 2)

            if status == "pending":
                return {"task_id": task_id, "status": "pending",
                        "elapsed_s": elapsed}

            del _tasks[task_id]

            if status == "done":
                return {"task_id": task_id, "status": "done",
                        "elapsed_s": elapsed, "result": entry["result"]}

            return {"task_id": task_id, "status": "error",
                    "elapsed_s": elapsed, "error": entry.get("error", "")}

    elif action == "cancel":
        if task_id is None:
            return {"ok": False, "error": "task_id= required for cancel"}
        with _tasks_lock:
            _tasks.pop(task_id, None)
        return {"ok": True, "cancelled": task_id}

    elif action == "list":
        with _tasks_lock:
            now = time.monotonic()
            return {
                "tasks": [
                    {
                        "task_id"   : tid,
                        "status"    : e["status"],
                        "elapsed_s" : round(now - e["started_at"], 2),
                        "instance"  : e.get("alias"),
                    }
                    for tid, e in _tasks.items()
                ]
            }

    else:
        return {"ok": False, "error": f"unknown action: {action!r}"}
