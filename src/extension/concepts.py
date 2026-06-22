"""Hand-curated concept entries for the Search-API index.

The base ``api_index`` is built by introspecting the live ``renderdoc``
module. That gives the agent a name-keyed lookup for every class,
method, and enum. What it does NOT give is task-keyed lookup —
``Search-API("how do I read a constant buffer")`` against pure
introspection returns weak matches.

These concept entries fix that. They're appended to the index after
``build_index()`` finishes, so the same search machinery applies. Each
entry's ``doc`` is a short recipe with one runnable code example.

Concept entries use kind="concept" so callers can filter them when
they want pure API results.
"""

CONCEPTS = [
    # --- Frame structure ---
    {
        "name"      : "concept:find_marker_by_name",
        "kind"      : "concept",
        "signature" : "find_marker(name, regex=False, parent=None)",
        "doc"       : """\
Find a PushMarker scope or draw by its name without writing the
recursive walk yourself.

    find_marker("Bloom")
    # -> [{'eventId': 412, 'name': 'BloomBlur',
    #      'path': 'RenderImageSpaceEffect/BloomBlur',
    #      'is_marker': True, 'customName': 'BloomBlur'}, …]

Add ``parent=eventId`` to scope to a subtree, ``markers_only=True``
to skip leaf draws, or ``regex=True`` for pattern matching. Use the
returned ``path`` to disambiguate when multiple subtrees share names.
""",
    },
    {
        "name"      : "concept:enumerate_draw_calls",
        "kind"      : "concept",
        "signature" : "get_draw_calls() / get_all_actions()",
        "doc"       : """\
Get every leaf draw call in the current frame as a flat list:

    get_draw_calls()
    # -> [{'eventId': 12, 'name': 'vkCmdDrawIndexed(36, ...)'}, ...]

For markers, dispatches, clears, copies — anything not a leaf draw:

    get_all_actions()
    # -> [{'eventId': ..., 'name': ..., 'flags': ['PushMarker', ...]}, ...]

Both work inside or outside a ctx.replay() callback.
""",
    },

    # --- Draw inspection ---
    {
        "name"      : "concept:describe_one_draw",
        "kind"      : "concept",
        "signature" : "describe_draw(eventId=eid)",
        "doc"       : """\
Comprehensive one-shot summary of a draw — shaders, render targets,
depth target, draw params, vertex/index buffers, push constants:

    describe_draw(eventId=412)

Note: keyword-only. describe_draw(412) raises. For multiple events
in one ctx.replay() trip, use describe_draws([eid1, eid2, ...]).
""",
    },
    {
        "name"      : "concept:describe_many_draws_efficiently",
        "kind"      : "concept",
        "signature" : "describe_draws([eid, eid, ...])",
        "doc"       : """\
Batched describe_draw — snapshots N events in a single ctx.replay():

    describe_draws([100, 150, 200, 250])
    # -> [ describe_draw result, describe_draw result, ... ]

Pays the bridge→replay-thread handoff cost once instead of N times.
Each event still triggers its own SetFrameEvent (full GPU replay), so
keep the list small (~20 max). If any single replay hangs, the whole
batch hangs — combine with async via Task(action="poll") for very
large batches.
""",
    },

    # --- Constant buffers ---
    {
        "name"      : "concept:read_constant_buffer",
        "kind"      : "concept",
        "signature" : "auto_decode_cb(stage, slot=0, eventId=None)",
        "doc"       : """\
Read and decode a constant buffer in one call:

    auto_decode_cb("ps", slot=0, eventId=412)
    # -> {'stage': 'Pixel', 'slot': 0, 'name': 'PerFrame',
    #     'resource': 'ResourceId(...)', 'byte_offset': 0,
    #     'byte_size': 256,
    #     'decoded': [{'name': 'View', 'type': 'float4x4', ...}, ...]}

Handles the Vulkan VK_WHOLE_SIZE footgun (u64::MAX in byteSize) by
looking up the buffer's real length. ``stage`` accepts either an
rd.ShaderStage enum or a string alias: vs/ps/cs/vertex/pixel/compute/
fragment/...
""",
    },

    # --- Textures ---
    {
        "name"      : "concept:summarize_render_target",
        "kind"      : "concept",
        "signature" : "summarize_texture(resource_id, event_id)",
        "doc"       : """\
First diagnostic when a render target looks wrong — per-channel
min/max/mean/NaN/Inf:

    summarize_texture("ResourceId(123)", event_id=412)
    # -> {'channels': ['r','g','b','a'],
    #     'stats': {'r': {'min': 0.0, 'max': 1.2, 'mean': 0.5,
    #                     'nan_count': 0, 'inf_count': 0}, ...}}

Catches blown-out HDR, NaN poisoning, and dead-black RTs without
needing to save_texture and look. Supports 8-bit UNORM/SRGB and
16/32-bit float; block-compressed formats return an error.
""",
    },
    {
        "name"      : "concept:visual_diff_two_textures",
        "kind"      : "concept",
        "signature" : "Get-Texture(compare_with=...)",
        "doc"       : """\
Pixel-wise diff between two textures in a single tool call:

    Get-Texture(resource_id="A", event_id=412,
                compare_with="B", compare_event_id=412)

Returns four content blocks: JSON stats (max_delta,
pixels_changed_pct, size mismatch) + A + B + the absolute-difference
image amplified for visibility. Use cases observed in this fork:

  - Stereo VR — A = left eye, B = right eye render target at the
    same event.
  - Baseline vs broken — A from instance="baseline", B with
    compare_instance="broken" (the killer ConnectionPool use case).
  - Before/after — A and B at different event_id on the same texture
    to see how a post-process stage modified it.

If A and B differ in size, B is resized to A's dimensions before
the diff.
""",
    },

    # --- Cross-capture comparison ---
    {
        "name"      : "concept:compare_two_captures",
        "kind"      : "concept",
        "signature" : "ConnectionPool + Instance(action=find_first_divergence)",
        "doc"       : """\
Multi-instance comparison workflow for "this used to work, now it
doesn't":

    Instance(action="connect", port=19876, alias="baseline")
    Instance(action="connect", port=19878, alias="broken")
    Instance(action="find_first_divergence",
             instance_a="baseline", instance_b="broken")
    # -> {'first_divergent_event': 412, 'name': 'BloomBlur',
    #     'examined': {'a': {...}, 'b': {...}}, ...}

Bisects events between the two pool aliases and reports the first
event where pipeline state diverges (different shaders bound, render
targets, or depth target). For a 10K-event frame this is ~14 GPU
replays per side instead of ~20K linear comparisons. Long enough
to fire async; combine with Task(action="poll").
""",
    },

    # --- VR / stereo ---
    {
        "name"      : "concept:stereo_vr_inspection",
        "kind"      : "concept",
        "signature" : "stereo render target debugging",
        "doc"       : """\
For stereo VR captures, both eye render targets live as separate
elements in state.GetOutputTargets() at the same event. To compare
left vs right in one MCP call:

    def work(controller):
        controller.SetFrameEvent(eid, True)
        state   = controller.GetPipelineState()
        targets = state.GetOutputTargets()
        return [serialize.resource_id(t.resource) for t in targets[:2]]
    left, right = ctx.replay(work)

    Get-Texture(resource_id=left, event_id=eid,
                compare_with=right, compare_event_id=eid)
""",
    },

    # --- Tools ---
    {
        "name"      : "concept:tracy_vs_renderdoc",
        "kind"      : "concept",
        "signature" : "when to reach for which tool",
        "doc"       : """\
Tracy and RenderDoc are complementary, not redundant:

  - Tracy (mcp__tracy__*) answers "what is slow?", "what is blocked
    on what?", "did this change regress perf?" — zone-level CPU+GPU
    timing, lock contention, memory deltas.

  - RenderDoc (this MCP) answers "what is this draw doing?", "what
    is bound here?", "why does this pixel look wrong?" — single-
    frame state snapshot, per-draw inspection.

Typical paired workflow: Tracy locates the suspicious zone, then
open or capture the corresponding RenderDoc frame to inspect GPU
state inside it. Don't use RenderDoc's per-draw counters to answer
"what is slow?" before Tracy has narrowed the window.
""",
    },
    {
        "name"      : "concept:dry_run_before_long_eval",
        "kind"      : "concept",
        "signature" : "Eval(code=..., dry_run=True)",
        "doc"       : """\
Validate a long codeblock before paying for a SetFrameEvent:

    Eval(code='''
    def work(controller):
        controller.SetFrameEvent(412, True)
        # ... 50 lines of analysis ...
    ctx.replay(work)
    ''', dry_run=True)
    # -> {'parsed': True, 'unbound': ['descrbie_draw'], ...}

Reports SyntaxError + unbound names from a single AST walk; no
replay or UI thread invocation happens. Catches typos and forgotten
imports before they hit a 60s replay timeout.
""",
    },

    # --- Capture triggering ---
    {
        "name"      : "concept:trigger_capture_from_target",
        "kind"      : "concept",
        "signature" : "Instance(action=targets) → Instance(action=trigger_capture)",
        "doc"       : """\
Two-step "make a fresh capture from a running game" workflow:

  1. List live capture-layer targets:

        Instance(action="targets")
        # -> {'targets': [
        #       {'ident': 38920, 'target': 'SkyrimSE.exe',
        #        'pid': 12480, 'api': 'D3D11'},
        #     ]}

  2. Trigger one or more frame captures on a target:

        Instance(action="trigger_capture", target_ident=38920,
                 num_frames=1, directory="C:/captures/run1/")
        # -> {'captures': [
        #       {'captureId': 0, 'frameNumber': 12345,
        #        'target_path': 'C:/Users/.../SkyrimSE_2026...rdc',
        #        'local_path':  'C:/captures/run1/SkyrimSE_...rdc',
        #        'byteSize': 268435456}],
        #     'complete': True}

Use ``num_frames=N`` for sequential multi-frame; each lands as its
own .rdc. Use ``frame_number=N`` to QueueCapture starting at frame N
instead of "next frame". Pass ``directory=`` to also CopyCapture each
arrival to a local folder; omit it to leave the file on the target.

``wait_secs`` (default 10, max 300) is how long the handler blocks
waiting for captures to arrive. For long multi-frame waits, fire
async — wrap the call in Eval(async_mode=True) and poll Task —
because the handler holds the bridge lock for its full duration.

After a capture lands, hand the local_path (or the target_path if
the file is shared) to Instance(action="open", file=...) to spawn a
headless worker for analysis, or to Instance(action="load_capture",
file=..., alias=...) to load it into the GUI.

Pass ``force=True`` to steal the target-control channel from any
RenderDoc UI that's currently attached to the target.
""",
    },

    # --- Large captures ---
    {
        "name"      : "concept:large_capture_workflow",
        "kind"      : "concept",
        "signature" : "GUI load_capture + Eval(async_mode) + Task(poll)",
        "doc"       : """\
Multi-GB captures (e.g. a single ~5GB stereo VR frame) wedge the
headless worker: a SetFrameEvent to a late event replays the whole
frame and outruns the socket deadline. Use the persistent GUI path:

  1. Load into a GUI instance (no replay cutoff):

        Instance(action="load_capture", file="C:/caps/vr.rdc", alias="vr")
        # poll until ready:
        Instance(action="list")   # wait for capture_loaded: true

  2. Run slow queries as background tasks so they can't trip the
     socket deadline:

        Eval(code="describe_draw(eventId=8000)", instance="vr",
             async_mode=True, timeout=600)
        Task(action="poll", task_id=...)

  3. Order SetFrameEvent calls by INCREASING eventId across calls so
     replay steps forward incrementally instead of re-replaying the
     frame from event 0 each time.

Instance(action="open") (headless) is the wrong first choice here and
returns a ``warning`` when handed a multi-GB file. Note: raw
ctx.ctx.LoadCapture from Eval is blocked — use Instance(load_capture).
""",
    },

    # --- Pixel debugging ---
    {
        "name"      : "concept:pixel_debugger_workflow",
        "kind"      : "concept",
        "signature" : "pixel_history → debug_pixel",
        "doc"       : """\
Two-step "why is this pixel this colour" workflow:

  1. Identify the draws that touched it:

        pixel_history("ResourceId(123)", x=512, y=384, event_id=900)
        # -> {'events': [
        #       {'event_id': 412, 'frag_index': 0, 'primitive_id': 5,
        #        'pre':  {'col': [0,0,0,1]},
        #        'shader_out': {'col': [0.5, 0.0, 0.0, 1.0]},
        #        'post': {'col': [0.5, 0.0, 0.0, 1.0]},
        #        'failed_tests': []},
        #       …]}

     Look for the event that wrote the wrong value, or for tests that
     unexpectedly culled the fragment (depth_test_failed,
     shader_discarded, scissor_clipped, …).

  2. Step into the offending pixel shader:

        debug_pixel(event_id=412, x=512, y=384)
        # -> {'had_trace': True, 'has_source': True,
        #     'inputs': [{'name': 'TEXCOORD0', 'value': [0.5, 0.5]}, ...],
        #     'constant_blocks': [...], 'readonly_resources': [...]}

For full per-step inspection use ctrl.DebugPixel /
ctrl.ContinueDebug directly inside ctx.replay() — the utility
auto-frees the trace handle so it can't be reused for stepping.
""",
    },

    # --- API discovery cheat-sheet ---
    {
        "name"      : "concept:api_gotchas",
        "kind"      : "concept",
        "signature" : "common RenderDoc Python attribute traps",
        "doc"       : """\
Attribute mismatches that cost round-trips, with the correct form:

  - Action names: there is NO controller.GetAction(eid). Action
    objects carry their own name — action.GetName(ctx.structured_file).
    Find the action with the recursive tree walk, or just use
    describe_draw(eventId=eid) / find_marker(name).

  - Bound-resource lists return UsedDescriptor, which wraps a single
    Descriptor in ``.descriptor`` (singular) — there is no
    ``.descriptors``:
        ud.descriptor.resource    # the bound ResourceId
        ud.descriptor.byteOffset  # NOT ud.descriptors / .resourceId

  - Descriptor.resource (a ResourceId), not Descriptor.resourceId.

  - Depth/stencil TEST state (enable, write, compare func) is NOT on
    the API-agnostic PipeState. Use the API-specific object:
        controller.GetD3D11PipelineState().outputMerger.depthStencilState
        controller.GetVulkanPipelineState().depthStencil

  - controller.GetUsage(rid) needs a ResourceId OBJECT, not an int.
    Get it from GetTextures()/GetResources(), or just call the
    ``usage(resource)`` helper which accepts an int/string and resolves
    it (see concept:resource_usage).

  - ResourceFormat name is fmt.Name() (a method), not fmt.name.

When in doubt, inspect(obj) lists the real attributes, and Search-API
looks up exact signatures.
""",
    },
    {
        "name"      : "concept:resource_usage",
        "kind"      : "concept",
        "signature" : "usage(resource) / controller.GetUsage(ResourceId)",
        "doc"       : """\
"Which events touched this resource, and how?"

    usage("12345")        # int, string id, or ResourceId all accepted
    # -> {'resource': '12345',
    #     'usage': [{'eventId': 412, 'usage': 'ColorTarget'},
    #               {'eventId': 980, 'usage': 'PS_Resource'}, ...]}

The helper wraps controller.GetUsage, which raw requires a ResourceId
OBJECT (passing an int raises). Pass an id straight from describe_draw
or get_outputs and it resolves the handle for you.
""",
    },
    {
        "name"      : "concept:output_targets_and_viewport",
        "kind"      : "concept",
        "signature" : "get_outputs(eventId) / get_viewport(eventId)",
        "doc"       : """\
"What is this draw writing to, and where on screen?"

    get_outputs(eventId=412)
    # -> {'color': [{'resource': '88', 'format': 'R16G16B16A16_FLOAT',
    #                'firstMip': 0, 'firstSlice': 0}],
    #     'depth': {'resource': '90', 'format': 'D32_FLOAT'}}

    get_viewport(eventId=412)
    # -> {'index': 0, 'x': 0.0, 'y': 0.0, 'width': 8688.0,
    #     'height': 4615.0, 'minDepth': 0.0, 'maxDepth': 1.0}

Both seek to eventId first (omit to use the current cursor) and return
plain dicts — no GetOutputTargets()/GetViewport() boilerplate. Hand a
returned color/depth ``resource`` straight to Get-Texture,
summarize_texture, or usage().
""",
    },

    {
        "name"      : "concept:depth_stencil_state",
        "kind"      : "concept",
        "signature" : "depth_stencil(eventId)",
        "doc"       : """\
"Is depth test/write on? What's the compare func and stencil op?"

Depth/stencil TEST state is NOT on the API-agnostic PipeState — it
lives on the API-specific object. Skip the guess:

    depth_stencil(eventId=412)
    # -> {'event_id': 412, 'api': 'D3D11',
    #     'depth_stencil': {'depthEnable': True, 'depthWrites': False,
    #                       'depthFunction': 'GreaterEqual',
    #                       'stencilEnable': False,
    #                       'frontFace': {...}, 'backFace': {...}}}

Finds the right per-API object for you
(D3D11/D3D12 outputMerger.depthStencilState, Vulkan depthStencil) and
dumps its real fields. Raw path if you need it:
controller.GetD3D11PipelineState().outputMerger.depthStencilState.
""",
    },

    # --- Fresh captures ---
    {
        "name"      : "concept:capture_completion_signal",
        "kind"      : "concept",
        "signature" : "Instance(action=trigger_capture) waits for NewCapture",
        "doc"       : """\
After TriggerCapture, the NewCapture message often does NOT arrive in
a short ReceiveMessage pump — a raw scripted trigger leaves you with
no reliable in-API "it's ready" signal, and a multi-GB VR .rdc takes
several seconds to finish writing.

Use Instance(action="trigger_capture") instead of scripting
TriggerCapture by hand: it pumps ReceiveMessage up to ``wait_secs``
(default 10, max 300) and returns each arrival's ``target_path`` and
``byteSize`` once NewCapture lands, with ``complete: true`` when all
requested frames arrived:

    Instance(action="trigger_capture", target_ident=38920,
             num_frames=1, wait_secs=30, directory="C:/caps/")

If a capture is slow to write, raise ``wait_secs``. With ``directory=``
set, each file is CopyCapture'd locally (a synchronous, complete copy);
without it you get the on-target path, which may still be flushing — if
you must read it directly, wait for its size to stop growing.
""",
    },

    # --- Connections ---
    {
        "name"      : "concept:stable_connection_aliases",
        "kind"      : "concept",
        "signature" : "pin alias= on connect/open; rebind after restart",
        "doc"       : """\
A connection alias auto-derived from the capture re-keys after a
game/target restart (e.g. "vr" -> "port_19876"), breaking later
Eval(instance="vr") calls.

Always PIN an explicit alias so it never auto-derives:

    Instance(action="connect", port=19876, alias="vr")
    Instance(action="open",    file="vr.rdc", alias="vr")

After a restart the old port is dead. Reconnect to the NEW port under
the SAME alias — the pool evicts the stale entry and rebinds the name:

    Instance(action="list")                      # find the new port
    Instance(action="connect", port=19880, alias="vr")  # "vr" now -> 19880

Eval(instance="vr", ...) keeps working across the restart. Use
Instance(action="set_default", alias="vr") to drop the instance= arg
entirely.
""",
    },

    # --- Safety ---
    {
        "name"      : "concept:dont_load_or_close_from_eval",
        "kind"      : "concept",
        "signature" : "use Instance(action=load_capture/close_capture)",
        "doc"       : """\
NEVER call ctx.ctx.LoadCapture() or ctx.ctx.CloseCapture() from
Eval. Both are intercepted by the safety proxy and will raise:

  - LoadCapture re-enters the replay lifecycle while the replay
    thread is active. Guaranteed deadlock.
  - CloseCapture must run on the Qt UI thread; Eval runs on the
    bridge handler thread. Off-thread Qt calls crash RenderDoc
    (observed CTD).

Use the Instance tool instead, which dispatches via invoke_ui:

    Instance(action="load_capture",  file="/path/to/c.rdc",
             alias="my-instance")
    Instance(action="close_capture", alias="my-instance")
""",
    },

    # --- API drift: removed / renamed names ---
    # Named after the missing API so a search for it matches and redirects,
    # instead of returning nothing and forcing trial-and-error.
    {
        "name"      : "GetConstantBuffers",
        "kind"      : "concept",
        "signature" : "PipelineState.GetConstantBlocks(stage)  +  auto_decode_cb(stage, slot)",
        "doc"       : """\
There is no GetConstantBuffers / GetConstantBuffer on PipelineState in
this RenderDoc API — the accessor is GetConstantBlocks(stage). To read
and decode a constant buffer, prefer the helper:

    auto_decode_cb("ps", slot=0, eventId=412)

Raw chain if you need it: state.GetConstantBlocks(stage)[slot].descriptor
gives .resource / .byteOffset / .byteSize; then
controller.GetBufferData(resource, off, size). See
concept:read_constant_buffer.
""",
    },
    {
        "name"      : "Descriptor.resourceId",
        "kind"      : "concept",
        "signature" : "Descriptor.resource  (ResourceId)",
        "doc"       : """\
A Descriptor has no .resourceId in this API — the field is .resource (a
ResourceId). Applies to descriptors from GetConstantBlocks(),
GetReadOnlyResources(), and the output-merger targets:

    depth = state.GetDepthTarget()   # a Descriptor
    rid   = depth.resource           # NOT depth.resourceId

Caveat: TextureDescription and BufferDescription DO use .resourceId; the
rename is only on Descriptor.
""",
    },
    {
        "name"      : "concept:getusage_empty_on_aliased_resource",
        "kind"      : "concept",
        "signature" : "GetUsage(rid) is keyed by exact ResourceId; copies/aliases read a different id",
        "doc"       : """\
GetUsage(resourceId) lists usage for THAT exact ResourceId only. If a
later pass samples an aliased COPY of a resource (a different
ResourceId), those reads do not appear under the original — GetUsage can
return just Clear + DepthStencilTarget with no SRV reads. This is a
RenderDoc data limitation, not a bug, and following the original id
dead-ends.

To walk from a sampled value back to its real source, use pixel history
on the consuming draw's output and inspect the contributing events:

    pixel_history(resource_id=<RT/backbuffer>, x=.., y=.., event_id=..)

Also scan get_all_actions() for Copy/Resolve events around the draw to
spot where the alias was produced, then GetUsage() that copy's id.
""",
    },
]
