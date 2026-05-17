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
]
