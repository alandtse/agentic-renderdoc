"""MCP tool definitions for eval, search_api, instance, get_texture, and task management."""

from __future__ import annotations

import base64
import io
import json
import os
import re
import struct
import sys
import threading
import time
import uuid
from pathlib import Path
from typing  import Any

from PIL import Image as PILImage

from mcp.server.fastmcp.utilities.types import Image as MCPImage
from mcp.types import TextContent

from server.app import mcp
from server.client import ConnectionPool

_pool = ConnectionPool()


# ---------------------------------------------------------------------------
# Async task registry — backs Eval(async_mode=True) + Task tool
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
            "status"     : "pending",
            "started_at" : time.monotonic(),
            "alias"      : alias,
        }

    t = threading.Thread(
        target = _run_async,
        args   = (task_id, cmd, params, alias, timeout),
        daemon = True,
    )
    t.start()
    return task_id


# Default capture-dump locations scanned by Instance(action='discover').
# Override via AGENTIC_RENDERDOC_CAPTURE_DIRS (os.pathsep-separated).
_DEFAULT_CAPTURE_DIRS_LINUX  = ["/tmp/RenderDoc"]
_DEFAULT_CAPTURE_DIRS_WIN    = [r"%TEMP%\RenderDoc"]

# RenderDoc's default capture filename template:
# <exename>_<YYYY>.<MM>.<DD>_<HH>.<MM>_frame<N>.rdc
_CAPTURE_NAME_RE = re.compile(
    r"^(?P<exe>.+?)_(?P<date>\d{4}\.\d{2}\.\d{2})_"
    r"(?P<time>\d{2}\.\d{2})_frame(?P<frame>\d+)\.rdc$"
)


# --- eval ---

@mcp.tool(name="Eval")
def eval(code: str, instance: str | None = None,
         async_mode: bool = False, timeout: float = 300.0,
         dry_run: bool = False) -> dict:
    """Execute Python code in a live RenderDoc replay session.

    This is your primary interface for all GPU capture inspection, analysis,
    and debugging. Code runs inside RenderDoc's embedded Python interpreter
    with full access to the replay engine.

    WHEN TO REACH FOR RENDERDOC (vs Tracy)
    ======================================
    If a Tracy MCP is also available (``mcp__tracy__*``), the two tools
    are complementary, not redundant:

      - Tracy gives you frame-level / zone-level CPU+GPU timing, lock
        contention, memory deltas. Reach for it first when the question
        is "what is slow?", "what is blocked on what?", or "did this
        change regress perf?".

      - RenderDoc gives you a single-frame state snapshot — every draw
        call, pipeline state, bound resource, and pixel. Reach for it
        when the question is "what is this draw doing?", "what is bound
        here?", or "why does this pixel look wrong?".

    Typical paired workflow: use Tracy to find the suspicious zone,
    then open or capture the matching frame in RenderDoc to inspect
    GPU state inside it. Don't try to answer perf-style questions with
    RenderDoc's per-draw counters until Tracy has narrowed the window —
    the cost of replaying a whole frame to get those numbers is wasted
    if Tracy already shows the answer at the zone level.

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
        qrd          -- the qrenderdoc module (UI types; GUI only)
        ctx          -- HandlerContext:
                        ctx.replay(callback) for replay access
                        ctx.structured_file  for ActionDescription.GetName()
                        ctx.ctx              the live qrenderdoc.CaptureContext
                                             (GUI only — see UI-LEVEL CONTEXT)
                        ctx.headless         True inside a headless worker
        serialize    -- type serialization (see below)

    UI-LEVEL CONTEXT (ctx.ctx, GUI only)
    ------------------------------------
    On a GUI instance, ``ctx.ctx`` is the live qrenderdoc.CaptureContext —
    the same object the RenderDoc UI uses. Useful read-only handles:

        ctx.ctx.Config()                         -- PersistantConfig
            .DefaultCaptureSaveDirectory         -- str, default save dir
            .TemporaryCaptureDirectory           -- str
            .LastCaptureFilePath                 -- str, last opened capture
            .RecentCaptureFiles                  -- list[str] (often stale)
        ctx.ctx.GetCaptureFilename()             -- str, currently loaded
        ctx.ctx.GetCaptureFile()                 -- ICaptureFile
        ctx.ctx.GetStructuredFile()              -- SDFile (also ctx.structured_file)
        ctx.ctx.APIProps().pipelineType          -- GraphicsAPI enum

    DO NOT call ctx.ctx.LoadCapture() or ctx.ctx.CloseCapture() from Eval.
    Both are intercepted and refuse to run — see PERFORMANCE AND STABILITY.
    For browsing captures on disk, prefer Instance(action="captures",
    thumbnails=True) which bundles the same info with inline previews.

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

        find_marker(name, regex=False, case_sensitive=False,
                    markers_only=False, parent=None)
            Search the action tree for markers (PushMarker scopes) or
            draw names containing the given substring or regex. Returns
            a list of {"eventId", "name", "path", "is_marker",
            "customName"} where "path" is the "/"-joined ancestor marker
            scopes ("RenderImageSpaceEffect/BloomBlur/..."). Pass
            ``parent=eventId`` to scope the search to a subtree, or
            ``markers_only=True`` to skip leaf draws. Skip the manual
            recursive walk.

        describe_draw(eventId=eid)
            (keyword-only — `describe_draw(eid)` raises.)
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

    TIMEOUT AND CRASH RECOVERY
    ==========================
    The MCP server enforces per-command deadlines on every send. This
    tool has a 90-second deadline (covers a full SetFrameEvent replay).
    On timeout, the response is::

        {"ok": false, "error": {
            "kind": "worker_timeout",
            "port": ...,
            "headless": true,
            "pid": ...,
            "hints": ["force-terminate it with Instance(action='close', "
                      "port=..., force=True)", ...]
        }}

    A timeout means the worker is wedged (typically a hung SetFrameEvent
    in a replay driver). The replay state cannot be recovered — kill
    the worker via Instance and reopen the capture in a fresh worker.

    Similarly, if the worker crashes mid-call, the response has
    ``"kind": "worker_dead"`` and the dead worker is automatically
    untracked — you can immediately spawn a new one.

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
    - NEVER call LoadCapture or CloseCapture from eval (raw
      ctx.ctx.LoadCapture / ctx.ctx.CloseCapture). Both are intercepted
      and will raise. Reasons:
        LoadCapture  -- re-enters the replay lifecycle while the replay
                        thread is active. Guaranteed deadlock.
        CloseCapture -- CaptureContext is Qt-UI-thread-only. Eval runs
                        on the bridge handler thread; off-thread Qt
                        calls crash RenderDoc (observed CTD in
                        multi-session use). Worse during a concurrent
                        replay on another connection.
      Use Instance(action="load_capture", file=...) and
      Instance(action="close_capture") — they dispatch through invoke_ui
      and refuse to run while a replay is in flight.
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
    When more than one bridge is connected, target a specific one:

        Eval(code="get_draw_calls()", instance="baseline")
        Eval(code="get_draw_calls()", instance="broken")

    Omit instance= when only one connection is active. Use
    Instance(action='list') to see available aliases.

    DRY RUN
    =======
    Pass ``dry_run=True`` to parse the code, walk the AST, and report
    any unbound names — typos, forgotten imports, references to
    utilities that don't exist — without executing anything. Useful
    before a long codeblock that would otherwise pay for a SetFrameEvent
    just to discover you misspelled ``descrbie_draw``. Returns
    {parsed, statements, free_names, unbound, unbound_at}. Free of
    syntax errors and unbound names means the code at least *resolves*;
    runtime exceptions (NoneType attribute access, etc.) still happen
    only on a real call.

    ASYNC MODE
    ==========
    For long-running operations (large buffer scans, full-frame pixel
    diffs, etc.) that may exceed the normal read timeout, set
    async_mode=True. The call returns immediately with a task_id; use
    Task(action="poll", task_id=...) to retrieve the result.

        t = Eval(code="...", async_mode=True, timeout=120)
        # ... do other work ...
        Task(action="poll", task_id=t["task_id"])

    timeout controls the socket read deadline for the background call
    (default 300s). Ignored when async_mode=False.

    To fire work at two instances in parallel and collect both results:

        t1 = Eval(code="describe_draw(eventId=100)", instance="baseline", async_mode=True)
        t2 = Eval(code="describe_draw(eventId=100)", instance="broken",   async_mode=True)
        Task(action="poll", task_id=t1["task_id"])
        Task(action="poll", task_id=t2["task_id"])
    """
    if not _pool.aliases:
        try:
            _pool.ensure_connected()
        except ConnectionError as e:
            return {"ok": False, "error": str(e)}

    params = {"code": code}
    if dry_run:
        params["dry_run"] = True

    if async_mode:
        try:
            task_id = _start_task("eval", params,
                                  alias=instance, timeout=timeout)
            return {"task_id": task_id, "status": "pending"}
        except (ConnectionError, KeyError) as e:
            return {"ok": False, "error": str(e)}

    return _pool.send("eval", params, alias=instance)


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

    Returns a JSON array of matching entries ranked by relevance. Each entry:
        name:      Fully qualified name (e.g., "ReplayController.SetFrameEvent")
        kind:      "class", "method", "property", "enum", or "enum_value"
        doc:       Full RST-formatted docstring with param/type/return info
        signature: Method signature string, if applicable (e.g., "(eventId, force)")
    """
    if not _pool.aliases:
        try:
            _pool.ensure_connected()
        except ConnectionError as e:
            return {"ok": False, "error": str(e)}
    return _pool.send("api_index", {"query": query}, alias=instance)


# --- get_texture ---

@mcp.tool(name="Get-Texture")
def get_texture(
    resource_id      : str,
    event_id         : int | None = None,
    mip              : int   = 0,
    slice            : int   = 0,
    sample           : int   = 0,
    max_size         : int   = 2048,
    region_x         : int | None = None,
    region_y         : int | None = None,
    region_w         : int | None = None,
    region_h         : int | None = None,
    channel          : int   = -1,
    black_point      : float = 0.0,
    white_point      : float = 1.0,
    instance         : str | None = None,
    compare_with     : str | None = None,
    compare_event_id : int | None = None,
    compare_instance : str | None = None,
    diff_amplify     : float = 4.0,
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

    compare_with:     Optional second resource_id. When set, both
                      textures are fetched and post-processed identically
                      (same channel / region / black_point / white_point),
                      and the response contains FOUR content blocks:
                      a JSON summary with diff statistics, then the A,
                      B, and a per-pixel absolute-difference image
                      (amplified for visibility). Use this for stereo
                      left-vs-right comparison, baseline-vs-broken
                      render-target diffs, or before/after stages of a
                      post-process chain. If A and B differ in size, B
                      is resized to A's dimensions.
    compare_event_id: Event ID to seek before fetching the comparison
                      texture. Defaults to event_id (same event, useful
                      for paired-output stereo render targets).
    compare_instance: Pool alias of the connection to fetch B from.
                      Defaults to instance. Set to a different alias to
                      diff between two captures (the killer use case for
                      ConnectionPool — baseline vs broken).
    diff_amplify:     Multiplier applied to the absolute-difference image
                      before clamping to 0-255. Default 4.0 — small
                      differences become visible. Set 1.0 for no
                      amplification, or 0 to disable the diff image
                      (stats still computed).
    """
    if not _pool.aliases:
        try:
            _pool.ensure_connected()
        except ConnectionError as e:
            return [TextContent(type="text", text=json.dumps(
                {"ok": False, "error": str(e)}))]

    img_a, meta_a, err = _fetch_and_decode_texture(
        resource_id, event_id, mip, slice, sample, max_size,
        region_x, region_y, region_w, region_h,
        channel, black_point, white_point, instance,
    )
    if err is not None:
        return [TextContent(type="text", text=json.dumps(err))]

    if compare_with is None:
        buf = io.BytesIO()
        img_a.save(buf, format="PNG")
        return [
            TextContent(type="text", text=json.dumps(meta_a, indent=2)),
            MCPImage(data=buf.getvalue(), format="png").to_image_content(),
        ]

    # --- Compare path ---
    img_b, meta_b, err = _fetch_and_decode_texture(
        compare_with,
        compare_event_id if compare_event_id is not None else event_id,
        mip, slice, sample, max_size,
        region_x, region_y, region_w, region_h,
        channel, black_point, white_point,
        compare_instance if compare_instance is not None else instance,
    )
    if err is not None:
        return [TextContent(type="text", text=json.dumps(err))]

    # Resize B to A if needed for pixel-wise diff.
    if img_b.size != img_a.size:
        img_b_resized = img_b.resize(img_a.size, PILImage.Resampling.LANCZOS)
    else:
        img_b_resized = img_b

    diff_img, diff_stats = _diff_images(img_a, img_b_resized, diff_amplify)

    summary = {
        "a"     : meta_a,
        "b"     : meta_b,
        "diff"  : diff_stats,
    }

    blocks = [TextContent(type="text", text=json.dumps(summary, indent=2))]
    for label, im in (("A", img_a), ("B", img_b)):
        buf = io.BytesIO()
        im.save(buf, format="PNG")
        blocks.append(TextContent(type="text", text=label))
        blocks.append(MCPImage(data=buf.getvalue(), format="png").to_image_content())
    if diff_img is not None:
        buf = io.BytesIO()
        diff_img.save(buf, format="PNG")
        blocks.append(TextContent(type="text", text="diff (|A - B| x {:g})".format(diff_amplify)))
        blocks.append(MCPImage(data=buf.getvalue(), format="png").to_image_content())
    return blocks


def _fetch_and_decode_texture(
    resource_id, event_id, mip, slice, sample, max_size,
    region_x, region_y, region_w, region_h,
    channel, black_point, white_point, instance,
):
    """Fetch a texture through the pool, decode + post-process to a Pillow Image.

    Returns (image, metadata, error). On any failure image is None and
    error is a dict suitable for returning to the client.
    """
    resp = _pool.send("get_texture", {
        "resource_id" : resource_id,
        "event_id"    : event_id,
        "mip"         : mip,
        "slice"       : slice,
        "sample"      : sample,
    }, alias=instance)

    if not resp.get("ok"):
        return None, None, resp

    data     = resp["data"]
    raw      = base64.b64decode(data["raw"])
    fmt      = data["format"]
    width    = data["mip_width"]
    height   = data["mip_height"]
    metadata = {k: v for k, v in data.items() if k != "raw"}
    metadata["resource_id"] = resource_id
    if event_id is not None:
        metadata["event_id"] = event_id

    img = _decode_texture(raw, width, height, fmt, black_point, white_point)
    if img is None:
        fmt_name = fmt.get("name", "unknown")
        return None, None, {
            "ok"    : False,
            "error" : f"unsupported texture format: {fmt_name}. use RenderDoc's texture viewer instead.",
        }

    if channel >= 0:
        bands = img.split()
        if channel < len(bands):
            img = bands[channel].convert("L")
            metadata["channel_extracted"] = channel

    has_region = all(v is not None for v in (region_x, region_y, region_w, region_h))
    if has_region:
        box = (region_x, region_y, region_x + region_w, region_y + region_h)
        img = img.crop(box)
        metadata["region"] = {"x": region_x, "y": region_y, "w": region_w, "h": region_h}

    if max_size > 0:
        w, h = img.size
        if w > max_size or h > max_size:
            scale        = max_size / max(w, h)
            new_w, new_h = int(w * scale), int(h * scale)
            img          = img.resize((new_w, new_h), PILImage.Resampling.LANCZOS)
            metadata["scaled"] = {"from": [w, h], "to": [new_w, new_h]}

    return img, metadata, None


def _diff_images(img_a, img_b, amplify):
    """Compute |A - B| per pixel and return (diff_image, stats).

    diff_amplify multiplies the per-pixel delta before clamping to 0-255
    so subtle differences are visible. amplify <= 0 disables the diff
    image (stats are still computed).
    """
    from PIL import ImageChops

    # Coerce to a common mode (RGBA preferred so we don't lose alpha).
    if img_a.mode != img_b.mode:
        target = "RGBA" if "A" in img_a.mode or "A" in img_b.mode else "RGB"
        a = img_a.convert(target)
        b = img_b.convert(target)
    else:
        a, b = img_a, img_b

    raw_diff = ImageChops.difference(a, b)

    # Stats: max delta + percent of pixels that differ at all.
    extrema = raw_diff.getextrema()
    # extrema is per-band [(min,max), …] for multiband images, or (min,max) for L.
    if isinstance(extrema[0], tuple):
        max_delta = max(hi for _, hi in extrema)
    else:
        max_delta = extrema[1]

    # Percent of pixels with any non-zero channel.
    if raw_diff.mode == "L":
        flat = raw_diff.point(lambda v: 255 if v > 0 else 0)
    else:
        flat = raw_diff.convert("L").point(lambda v: 255 if v > 0 else 0)
    nonzero = sum(1 for px in flat.getdata() if px > 0)
    total   = flat.width * flat.height
    pct     = (100.0 * nonzero / total) if total else 0.0

    stats = {
        "max_delta"          : int(max_delta),
        "pixels_changed"     : nonzero,
        "pixels_total"       : total,
        "pixels_changed_pct" : round(pct, 4),
        "size"               : list(a.size),
        "size_mismatch"      : list(img_a.size) != list(img_b.size),
    }

    if amplify <= 0:
        return None, stats

    scaled = raw_diff.point(lambda v: min(255, int(v * amplify)))
    return scaled, stats


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
    action             : str,
    port               : int | None = None,
    file               : str | None = None,
    force              : bool       = False,
    alias              : str | None = None,
    directory          : str | None = None,
    thumbnails         : bool       = False,
    thumbnail_max_size : int        = 256,
    limit              : int        = 12,
) -> Any:
    """Manage RenderDoc replay instances — both live GUIs and headless workers.

    Connections are tracked by alias in a pool, so multiple instances can
    be queried side-by-side (e.g. baseline vs broken capture). When only
    one connection is active it is used automatically and you can omit
    ``alias=``.

    action : One of:
             - ``list``        : Probe the agentic port range for active
                                 instances. Returns metadata for each,
                                 annotated with pool alias / headless flag.
             - ``discover``    : Scan known capture-dump locations for
                                 .rdc files. Does not spawn anything.
             - ``connect``     : Connect to a running bridge on ``port``.
                                 Registers under ``alias`` (auto-derived
                                 from the capture filename if omitted).
             - ``open``        : Spawn a headless worker for ``file`` via
                                 ``renderdoccmd remoteserver``, register
                                 it under ``alias``.
             - ``disconnect``  : Drop the named connection. Does NOT kill
                                 the underlying instance.
             - ``close``       : Stop a headless worker we spawned and
                                 drop its connection. Optionally
                                 ``force=True`` to SIGKILL immediately.
             - ``set_default`` : Pin a default alias so calls without
                                 ``instance=`` route to it.
             - ``captures``    : Ask a *GUI* instance to list .rdc files
                                 from RenderDoc's configured capture
                                 directory (DefaultCaptureSaveDirectory,
                                 etc.). The Qt-only "Captures" panel that
                                 the live UI shows when attached to a
                                 target has no Python-API surface; this
                                 is the closest scriptable equivalent.
                                 Returns its Config()-discovered dirs
                                 plus RecentCaptureFiles for context.
                                 Pass ``thumbnails=True`` to also stream
                                 the embedded PNG thumbnails as inline
                                 MCP image blocks (one per .rdc, newest
                                 first; capped by ``limit``, default 12).
             - ``load_capture``: Load ``file`` (.rdc) in a GUI instance.
                                 Dispatched on the Qt UI thread — never
                                 call ctx.ctx.LoadCapture from Eval (it
                                 deadlocks the replay thread). Pass
                                 ``force=True`` to close any currently
                                 loaded capture first.
             - ``close_capture``: Close the currently loaded capture in
                                 a GUI instance (Qt-UI-thread safe).

    Headless workers refuse load_capture/close_capture/captures — they
    are pinned to the file they were spawned for; use ``open``/``close``
    for their lifecycle and ``discover`` for server-side FS scanning.

    port      : Port for connect.
    file      : .rdc path for open / load_capture.
    directory : Optional dir override for captures (defaults to the
                GUI's DefaultCaptureSaveDirectory / TemporaryCaptureDirectory).
    force     : close: skip graceful shutdown and SIGKILL immediately.
                load_capture: close any currently loaded capture first.
    alias     : Pool alias. For connect/open: name to register under
                (auto-derived from capture filename if omitted). For
                disconnect/close/set_default/captures/load_capture/
                close_capture: which alias to target. If omitted with
                exactly one connection active, that one is used.

    Capture discovery directories for ``discover`` default to
    ``/tmp/RenderDoc`` (Linux) or ``%TEMP%\\RenderDoc`` (Windows). Add
    extra paths via the ``AGENTIC_RENDERDOC_CAPTURE_DIRS`` env var
    (os.pathsep-separated).
    """
    if action == "list":
        _pool.reap_dead()
        return {"instances": _pool.discover_instances(enrich=True)}

    if action == "discover":
        return {"captures": _discover_captures()}

    if action == "open":
        if not file:
            return {"ok": False, "error": "file is required for open"}
        try:
            spawn = _pool.open(file, alias=alias)
        except RuntimeError as e:
            return {"ok": False, "error": str(e)}
        return {"ok": True, **spawn}

    if action == "connect":
        if port is None:
            return {"ok": False, "error": "port is required for connect"}
        try:
            info = _pool.connect(port, alias=alias)
        except (ConnectionError, OSError) as e:
            return {"ok": False, "error": str(e)}
        return {"ok": True, **info, "connections": _pool.connection_info()}

    if action == "disconnect":
        try:
            dropped = _pool.disconnect(alias=alias)
        except KeyError as e:
            return {"ok": False, "error": str(e)}
        return {"ok": True, "disconnected": dropped,
                "connections": _pool.connection_info()}

    if action == "close":
        try:
            return _pool.close(alias=alias, force=force)
        except KeyError as e:
            return {"ok": False, "error": str(e)}

    if action == "set_default":
        if alias is None:
            return {"ok": False, "error": "alias is required for set_default"}
        try:
            _pool.set_default(alias)
        except KeyError as e:
            return {"ok": False, "error": str(e)}
        return {"ok": True, "default": alias}

    if action == "captures":
        params: dict = {}
        if directory is not None:
            params["directory"] = directory
        if thumbnails:
            params["thumbnails"]         = True
            params["thumbnail_max_size"] = thumbnail_max_size
            params["limit"]              = limit
        try:
            resp = _pool.send("capture_list", params, alias=alias)
        except (ConnectionError, KeyError) as e:
            return {"ok": False, "error": str(e)}
        if not thumbnails or not resp.get("ok"):
            return resp
        return _captures_with_thumbnails(resp)

    if action == "load_capture":
        if not file:
            return {"ok": False, "error": "file is required for load_capture"}
        try:
            return _pool.send(
                "capture_load",
                {"path": file, "replace": force},
                alias        = alias,
                read_timeout = 120.0,
            )
        except (ConnectionError, KeyError) as e:
            return {"ok": False, "error": str(e)}

    if action == "close_capture":
        try:
            return _pool.send("capture_close", {}, alias=alias)
        except (ConnectionError, KeyError) as e:
            return {"ok": False, "error": str(e)}

    return {"ok": False, "error": f"unknown action: {action}"}


def _captures_with_thumbnails(resp: dict) -> list:
    """Convert a capture_list response with embedded thumbnails into MCP
    content blocks: one JSON metadata block plus an inline PNG per file.
    """
    data    = resp.get("data", {})
    files   = data.get("files", [])
    summary = {
        "directory" : data.get("directory"),
        "config"    : data.get("config", {}),
        "files"     : [
            {k: v for k, v in f.items() if k != "thumbnail"}
            for f in files
        ],
    }

    blocks: list = [TextContent(type="text", text=json.dumps(summary, indent=2))]
    for f in files:
        thumb = f.get("thumbnail")
        if not thumb:
            continue
        try:
            png_bytes = base64.b64decode(thumb["data_b64"])
        except (KeyError, ValueError):
            continue
        blocks.append(TextContent(type="text", text=f["name"]))
        blocks.append(MCPImage(data=png_bytes, format="png").to_image_content())
    return blocks


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


# --- Capture discovery ---

def _discover_captures() -> list[dict]:
    """Scan default + env-override dirs for .rdc files."""
    dirs    = _capture_dirs()
    results = []
    seen    = set()

    for d in dirs:
        if not d.is_dir():
            continue
        for path in sorted(d.glob("*.rdc")):
            try:
                resolved = path.resolve()
            except OSError:
                continue
            if resolved in seen:
                continue
            seen.add(resolved)

            try:
                stat = path.stat()
            except OSError:
                continue

            entry = {
                "path"          : str(resolved),
                "size_bytes"    : stat.st_size,
                "mtime"         : stat.st_mtime,
            }
            exe = _captured_program(path.name)
            if exe is not None:
                entry["captured_program"] = exe
            results.append(entry)

    # Newest first.
    results.sort(key=lambda e: e["mtime"], reverse=True)
    return results


def _capture_dirs() -> list[Path]:
    """Resolve the list of capture directories to scan."""
    extra = os.environ.get("AGENTIC_RENDERDOC_CAPTURE_DIRS", "")
    extras = [Path(os.path.expandvars(p)) for p in extra.split(os.pathsep) if p]

    if sys.platform == "win32":
        defaults = [Path(os.path.expandvars(p)) for p in _DEFAULT_CAPTURE_DIRS_WIN]
    else:
        defaults = [Path(p) for p in _DEFAULT_CAPTURE_DIRS_LINUX]

    return defaults + extras


def _captured_program(filename: str) -> str | None:
    """Extract the captured executable name from a RenderDoc default filename."""
    m = _CAPTURE_NAME_RE.match(filename)
    if m is None:
        return None
    return m.group("exe")


