"""Command handlers for the RenderDoc bridge extension.

Handlers: eval, api_index, instance_info, get_texture, reload, shutdown.
"""
import ast
import traceback
from typing import Any, Callable, Dict, List, Optional

from .api_index import search_index

# Populated at registration time.
HANDLERS = {}  # type: Dict[str, Dict[str, Any]]

# Serializer dispatch table, keyed by RenderDoc type name.
# Built lazily on first use since the serialize module imports renderdoc,
# which may not be available during testing.
_SERIALIZER_MAP = None  # type: Optional[Dict[str, Callable[..., Any]]]


def handler(
    name: str,
    description: str = "",
    schema: Optional[Dict[str, Any]] = None,
) -> Callable[[Callable[..., Dict[str, Any]]], Callable[..., Dict[str, Any]]]:
    """Decorator to register a command handler."""
    def decorator(func: Callable[..., Dict[str, Any]]) -> Callable[..., Dict[str, Any]]:
        HANDLERS[name] = {
            "func"        : func,
            "description" : description,
            "schema"      : schema or {},
        }
        return func
    return decorator


# --- eval ---

@handler(
    "eval",
    description="Execute Python code in the RenderDoc environment.",
    schema={
        "properties": {
            "code"    : {"type": "string",  "description": "Python code to execute."},
            "dry_run" : {"type": "boolean", "description": "If True, parse the code and resolve names against the eval namespace but do not execute. Catches typos and NameErrors before paying for a SetFrameEvent."},
        },
        "required":   ["code"],
    },
)
def handle_eval(ctx: Any, params: Dict[str, Any]) -> Dict[str, Any]:
    """Execute arbitrary Python and return the result of the last expression.

    Builds a namespace with RenderDoc globals and utility modules, runs the
    code, serializes the result for JSON transport, and captures any print
    output. Returns error info with contextual hints on failure.

    When dry_run is set, the code is parsed and statically checked for
    unbound names against the live eval namespace, but no statement is
    executed. Lets the agent validate a long code block before paying
    for a SetFrameEvent or other expensive replay.
    """
    code = params.get("code", "")
    if not code:
        return {"error": "no code provided"}

    # Build the execution namespace with utilities and RenderDoc globals.
    captured_output = []
    namespace       = _build_namespace(ctx, captured_output)

    if params.get("dry_run"):
        return _dry_run(code, namespace)

    try:
        raw_result = _exec_with_result(code, namespace)
        result     = _serialize_result(raw_result)

        response = {"ok": True, "data": result}
        # Silent-null trap: code ended with `foo = ...` (not a bare
        # expression) and bound no `result`/`_` sentinel, so nothing came
        # back. Point the agent at the return convention instead of leaving
        # them to guess why data is null. Skip the hint when a `result`/`_`
        # sentinel IS bound — a deliberate `result = None` used the
        # convention correctly and shouldn't be told it didn't.
        if (raw_result is None and _last_node_is_assignment(code)
                and "result" not in namespace and "_" not in namespace):
            response["hint"] = (
                "Eval returns the last expression (or a `result`/`_` "
                "variable). Your final statement is an assignment, so the "
                "result is null — end with a bare expression (e.g. `result`) "
                "or assign to `result`."
            )
        if captured_output:
            response["output"] = captured_output
        if ctx._replay_warnings:
            response["warnings"] = list(ctx._replay_warnings)
        return response
    except Exception as e:
        response = {
            "ok"    : False,
            "error" : _format_error(e, code, namespace),
        }
        if captured_output:
            response["output"] = captured_output
        if ctx._replay_warnings:
            response["warnings"] = list(ctx._replay_warnings)
        return response


# --- api_index ---

@handler(
    "api_index",
    description="Search the RenderDoc Python API reference.",
    schema={
        "properties": {
            "query" : {"type": "string", "description": "Search term."},
            "limit" : {"type": "integer", "description": "Max results to return (default 20)."},
        },
        "required": ["query"],
    },
)
def handle_api_index(ctx: Any, params: Dict[str, Any]) -> Dict[str, Any]:
    """Search the cached API index for matching entries.

    Returns up to `limit` results from the pre-built API reference index.
    The index is built on first capture load by introspecting the live
    renderdoc module.
    """
    query = params.get("query", "").lower()
    if not query:
        return {"error": "no query provided"}

    if ctx.api_index is None:
        return {
            "ok"    : False,
            "error" : (
                "API index not built yet. "
                "It builds automatically when the first capture is loaded. "
                "Open a capture file and try again."
            ),
        }

    limit   = params.get("limit", 20)
    results = search_index(ctx.api_index, query)

    return {"ok": True, "data": results[:limit]}


# --- instance_info ---

@handler(
    "instance_info",
    description="Return metadata about this RenderDoc instance.",
    schema={},
)
def handle_instance_info(ctx: Any, params: Dict[str, Any]) -> Dict[str, Any]:
    """Return port, capture state, API type, path, and event count."""
    # Resolve the API type name from the SWIG enum. The enum wrapper may
    # or may not expose .name depending on the RenderDoc build.
    api_type = None
    if ctx._capture_loaded and ctx._api_type is not None:
        if hasattr(ctx._api_type, "name"):
            api_type = ctx._api_type.name
        else:
            api_type = str(ctx._api_type)

    capture_path = ctx._capture_path if ctx._capture_loaded else None
    event_count  = ctx._event_count  if ctx._capture_loaded else 0

    return {
        "ok"   : True,
        "data" : {
            "port"           : ctx._server_port,
            "capture_loaded" : ctx._capture_loaded,
            "api_type"       : api_type,
            "capture_path"   : capture_path,
            "event_count"    : event_count,
            "headless"       : getattr(ctx, "headless", False),
        },
    }


# --- get_texture ---

@handler(
    "get_texture",
    description="Read raw texture data as base64, with format metadata.",
    schema={
        "properties": {
            "resource_id" : {"type": "string",  "description": "Texture resource ID (as returned by other commands)."},
            "event_id"    : {"type": "integer", "description": "Event ID to set the replay cursor to before reading. Required for render targets. Omit for source textures."},
            "mip"         : {"type": "integer", "description": "Mip level (default 0)."},
            "slice"       : {"type": "integer", "description": "Array slice (default 0)."},
            "sample"      : {"type": "integer", "description": "Multisample index (default 0)."},
        },
        "required": ["resource_id"],
    },
)
def handle_get_texture(ctx: Any, params: Dict[str, Any]) -> Dict[str, Any]:
    """Read raw texture bytes and return them base64-encoded with format metadata.

    Uses GetTextureData for a direct memory read rather than SaveTexture,
    which triggers internal replays that can deadlock under agentic usage.
    Format conversion (HDR mapping, channel extraction, BGRA swizzle) is
    left to the MCP server where Pillow is available.
    """
    import base64

    import renderdoc as rd
    from . import serialize

    resource_id = params.get("resource_id")
    if not resource_id:
        return {"ok": False, "error": "resource_id is required"}

    event_id    = params.get("event_id")
    mip         = params.get("mip", 0)
    slice_param = params.get("slice", 0)
    sample      = params.get("sample", 0)

    def callback(controller: Any) -> Dict[str, Any]:
        # Find the matching texture by comparing serialized resource IDs.
        tex = None
        for t in controller.GetTextures():
            if str(int(t.resourceId)) == resource_id:
                tex = t
                break

        if tex is None:
            return {"ok": False, "error": f"no texture found with resource id {resource_id}"}

        # Position the replay cursor if an event was specified.
        # Required for render targets whose contents depend on the event.
        if event_id is not None:
            controller.SetFrameEvent(event_id, True)

        raw_bytes = controller.GetTextureData(
            tex.resourceId,
            rd.Subresource(mip, slice_param, sample),
        )

        if not raw_bytes:
            return {"ok": False, "error": "GetTextureData returned empty data"}

        mip_width  = max(1, tex.width >> mip)
        mip_height = max(1, tex.height >> mip)

        return {
            "ok"   : True,
            "data" : {
                "raw"        : base64.b64encode(bytes(raw_bytes)).decode("ascii"),
                "width"      : tex.width,
                "height"     : tex.height,
                "depth"      : tex.depth,
                "format"     : serialize.format_description(tex.format),
                "mip"        : mip,
                "mip_width"  : mip_width,
                "mip_height" : mip_height,
                "slice"      : slice_param,
                "sample"     : sample,
            },
        }

    return ctx.replay(callback)


# --- capture_load / capture_close / capture_list ---
#
# These manage the .rdc currently loaded in a *GUI* instance (the
# qrenderdoc-hosted bridge). Headless workers own the capture for
# their entire lifetime — opening or closing it is what spawning /
# closing the worker is for, so the handlers refuse to run there.

@handler(
    "capture_load",
    description="Open an .rdc capture file in this GUI instance.",
    schema={
        "properties": {
            "path"    : {"type": "string",  "description": "Absolute path to a .rdc capture file."},
            "replace" : {"type": "boolean", "description": "If True and a capture is already loaded, close it first. Default False."},
        },
        "required": ["path"],
    },
)
def handle_capture_load(ctx: Any, params: Dict[str, Any]) -> Dict[str, Any]:
    """Load a capture file via the UI thread.

    LoadCapture is async on RenderDoc's side: it queues a load and the
    OnCaptureLoaded callback fires when ready. This handler returns as
    soon as the UI thread has accepted the load request. Poll
    instance_info to confirm completion.
    """
    import os

    if getattr(ctx, "headless", False):
        return {
            "ok"    : False,
            "error" : (
                "capture_load is GUI-only; headless workers are pinned to "
                "the capture they were spawned for. Use "
                "Instance(action='open', file=...) to create a new worker."
            ),
        }

    path = params.get("path")
    if not path:
        return {"ok": False, "error": "path is required"}

    if not os.path.isfile(path):
        return {"ok": False, "error": "file not found: {}".format(path)}

    replace        = bool(params.get("replace", False))
    already_loaded = ctx._capture_loaded

    if already_loaded and not replace:
        return {
            "ok"           : False,
            "error"        : "a capture is already loaded; pass replace=True to close it first",
            "current_path" : ctx._capture_path,
        }

    if ctx._replay_controller is not None:
        return {
            "ok"    : False,
            "error" : "a replay is currently in flight on this instance; retry after it finishes",
        }

    try:
        import renderdoc as rd
        def do_load():
            if already_loaded:
                ctx.ctx.CloseCapture()
            ctx.ctx.LoadCapture(path, rd.ReplayOptions(), path, False, True)
        ctx.invoke_ui(do_load)
    except Exception as e:
        return {"ok": False, "error": "LoadCapture failed: {}".format(e)}

    return {
        "ok"   : True,
        "data" : {
            "path"     : path,
            "replaced" : already_loaded,
            "note"     : "load is asynchronous; poll instance_info to confirm completion",
        },
    }


@handler(
    "capture_close",
    description="Close the currently loaded capture in this GUI instance.",
    schema={},
)
def handle_capture_close(ctx: Any, params: Dict[str, Any]) -> Dict[str, Any]:
    """Close the active capture via the UI thread.

    Refuses to run while a replay is in flight — closing the capture
    out from under an active replay was the observed CTD in
    multi-session use.
    """
    if getattr(ctx, "headless", False):
        return {
            "ok"    : False,
            "error" : (
                "capture_close is GUI-only; close a headless worker via "
                "Instance(action='close', alias=...)."
            ),
        }

    if not ctx._capture_loaded:
        return {"ok": False, "error": "no capture loaded"}

    if ctx._replay_controller is not None:
        return {
            "ok"    : False,
            "error" : "a replay is currently in flight on this instance; retry after it finishes",
        }

    closed_path = ctx._capture_path

    try:
        ctx.invoke_ui(lambda: ctx.ctx.CloseCapture())
    except Exception as e:
        return {"ok": False, "error": "CloseCapture failed: {}".format(e)}

    return {"ok": True, "data": {"closed_path": closed_path}}


def _read_thumbnail(path, max_size):
    # type: (str, int) -> Optional[Dict[str, Any]]
    """Open a .rdc file and return its embedded thumbnail as base64 PNG.

    Uses rd.OpenCaptureFile().OpenFile()+.GetThumbnail() — these only
    parse file headers and never touch the live replay session, so they
    are safe to call from the bridge handler thread.

    Returns None on any failure (file missing, no embedded thumb,
    unsupported format).
    """
    import base64
    try:
        import renderdoc as rd
    except ImportError:
        return None

    cf = None
    try:
        cf = rd.OpenCaptureFile()
        result = cf.OpenFile(path, "rdc", None)
        if result is not None and hasattr(result, "code"):
            if rd.ResultCode is not None and result.code != rd.ResultCode.Succeeded:
                return None
        thumb = cf.GetThumbnail(rd.FileType.PNG, max(16, int(max_size)))
        if thumb is None or not thumb.data:
            return None
        return {
            "data_b64" : base64.b64encode(bytes(thumb.data)).decode("ascii"),
            "width"    : int(thumb.width),
            "height"   : int(thumb.height),
            "format"   : "png",
        }
    except Exception:
        return None
    finally:
        if cf is not None:
            try:
                cf.Shutdown()
            except Exception:
                pass


@handler(
    "capture_list",
    description="List .rdc files in the GUI instance's default capture directory.",
    schema={
        "properties": {
            "directory"          : {"type": "string",  "description": "Directory to scan. Defaults to RenderDoc's DefaultCaptureSaveDirectory (falling back to the directory of the last opened capture)."},
            "thumbnails"         : {"type": "boolean", "description": "Include embedded PNG thumbnails for each capture (base64-encoded). Default False."},
            "thumbnail_max_size" : {"type": "integer", "description": "Max width/height in pixels for thumbnails (default 256)."},
            "limit"              : {"type": "integer", "description": "When thumbnails=True, cap how many files are read (newest first). Default 12, 0 = no limit."},
        },
    },
)
def handle_capture_list(ctx: Any, params: Dict[str, Any]) -> Dict[str, Any]:
    """List .rdc files in the GUI's resolved capture directory.

    Resolution order: explicit ``directory`` param, then
    Config().DefaultCaptureSaveDirectory, then
    Config().TemporaryCaptureDirectory, then the directory of
    Config().LastCaptureFilePath. Also returns
    Config().RecentCaptureFiles for convenience.

    For server-side discovery without any connected instance, see
    Instance(action='discover') which scans /tmp/RenderDoc (Linux) or
    %TEMP%\\RenderDoc (Windows) instead of asking a RenderDoc UI.
    """
    import os

    if getattr(ctx, "headless", False):
        return {
            "ok"    : False,
            "error" : (
                "capture_list queries the GUI's PersistantConfig which "
                "has no analogue in a headless worker; use "
                "Instance(action='discover') for server-side FS scanning."
            ),
        }

    directory = params.get("directory")
    config    = None
    try:
        config = ctx.ctx.Config()
    except Exception:
        pass

    default_dir = ""
    temp_dir    = ""
    last_path   = ""
    recents     = []  # type: List[str]
    if config is not None:
        try:
            default_dir = str(getattr(config, "DefaultCaptureSaveDirectory", "") or "")
            temp_dir    = str(getattr(config, "TemporaryCaptureDirectory",    "") or "")
            last_path   = str(getattr(config, "LastCaptureFilePath",          "") or "")
            recents_raw = getattr(config, "RecentCaptureFiles", []) or []
            recents     = [str(p) for p in recents_raw]
        except Exception:
            pass

    if not directory:
        for candidate in (default_dir, temp_dir,
                          os.path.dirname(last_path) if last_path else ""):
            if candidate and os.path.isdir(candidate):
                directory = candidate
                break

    if not directory:
        return {
            "ok"    : False,
            "error" : (
                "no capture directory available — RenderDoc has no "
                "DefaultCaptureSaveDirectory set and no recent capture. "
                "Pass directory= explicitly."
            ),
            "config" : {
                "default_capture_save_directory" : default_dir,
                "temporary_capture_directory"    : temp_dir,
                "last_capture_file_path"         : last_path,
                "recent_capture_files"           : recents,
            },
        }

    if not os.path.isdir(directory):
        return {"ok": False, "error": "not a directory: {}".format(directory)}

    files = []
    try:
        for name in sorted(os.listdir(directory)):
            if not name.lower().endswith(".rdc"):
                continue
            full = os.path.join(directory, name)
            try:
                st = os.stat(full)
            except OSError:
                continue
            files.append({
                "name"  : name,
                "path"  : full,
                "size"  : st.st_size,
                "mtime" : st.st_mtime,
            })
    except OSError as e:
        return {"ok": False, "error": "listing failed: {}".format(e)}

    files.sort(key=lambda f: f["mtime"], reverse=True)

    if params.get("thumbnails"):
        max_size = int(params.get("thumbnail_max_size", 256))
        limit    = int(params.get("limit", 12))
        targets  = files if limit <= 0 else files[:limit]
        for entry in targets:
            thumb = _read_thumbnail(entry["path"], max_size)
            if thumb is not None:
                entry["thumbnail"] = thumb

    return {
        "ok"   : True,
        "data" : {
            "directory" : directory,
            "files"     : files,
            "config"    : {
                "default_capture_save_directory" : default_dir,
                "temporary_capture_directory"    : temp_dir,
                "last_capture_file_path"         : last_path,
                "recent_capture_files"           : recents,
            },
        },
    }


# --- targets_list / target_trigger_capture ---
#
# These talk to a *target* process (e.g. skyrim.exe) via the
# RenderDoc capture-layer control channel, not to a *replay analyzer*
# (which is what the rest of the bridge interacts with).
#
# rd.EnumerateRemoteTargets("", nextIdent) scans the local machine for
# RenderDoc-injected processes; rd.CreateTargetControl("", ident, …)
# opens a control channel. The channel is independent of the bridge's
# TCP socket and lives only as long as the handler holds it.

@handler(
    "targets_list",
    description="Enumerate running processes that have the RenderDoc layer loaded.",
    schema={
        "properties": {
            "host"      : {"type": "string", "description": "URL/host to scan (empty = localhost)."},
            "match"     : {"type": "string", "description": "Case-insensitive substring of the executable name; matching targets are returned under 'matched'."},
            "wait_secs" : {"type": "number", "description": "With 'match', poll until a matching target appears or this many seconds elapse (e.g. after injecting a game whose device takes a moment to register). Default: single pass."},
        },
    },
)
def handle_targets_list(ctx, params):
    # type: (Any, Dict[str, Any]) -> Dict[str, Any]
    """List live capture-control targets on the given host.

    For each ident reported by EnumerateRemoteTargets, opens a quick
    TargetControl connection to read the executable name, PID, API,
    and busy-client (if another tool already has the connection
    open). Each probe Shutdown()s before moving on so we don't keep
    the target busy.

    Pass ``match`` to filter targets by executable-name substring
    (case-insensitive), and ``wait_secs`` to poll until one appears —
    useful right after injecting a game, whose graphics device takes a
    moment to register a target and whose process is otherwise hard to
    tell from sibling helpers (e.g. SteamVR's).
    """
    import renderdoc as rd
    import time

    host        = params.get("host") or ""
    match       = params.get("match")
    wait_secs   = params.get("wait_secs")
    client_name = "agentic-renderdoc"

    def _enumerate():
        # Returns (targets, error_str). Hard cap on probes — pathological
        # hosts can otherwise spin.
        targets = []
        next_ident = 0
        for _ in range(64):
            try:
                next_ident = rd.EnumerateRemoteTargets(host, next_ident)
            except Exception as e:
                return None, "EnumerateRemoteTargets failed: {}".format(e)
            if not next_ident:
                break

            probe = None
            info = {"ident": int(next_ident)}
            try:
                probe = rd.CreateTargetControl(host, int(next_ident),
                                               client_name, False)
                if probe is not None:
                    try:
                        info["target"]      = str(probe.GetTarget())
                    except Exception:
                        pass
                    try:
                        info["api"]         = str(probe.GetAPI())
                    except Exception:
                        pass
                    try:
                        info["pid"]         = int(probe.GetPID())
                    except Exception:
                        pass
                    try:
                        busy = str(probe.GetBusyClient())
                        if busy:
                            info["busy_client"] = busy
                    except Exception:
                        pass
            except Exception as e:
                info["probe_error"] = str(e)
            finally:
                if probe is not None:
                    try:
                        probe.Shutdown()
                    except Exception:
                        pass

            targets.append(info)
        return targets, None

    def _matching(targets):
        m = (match or "").lower()
        return [t for t in targets if m in str(t.get("target", "")).lower()]

    # Only poll when both a name to wait for and a budget are given.
    deadline = (time.time() + float(wait_secs)) if (match and wait_secs) else None
    while True:
        targets, err = _enumerate()
        if err is not None:
            return {"ok": False, "error": err}
        matched = _matching(targets) if match else []
        if matched or deadline is None or time.time() >= deadline:
            break
        time.sleep(0.5)

    data = {"host": host or "localhost", "targets": targets}
    if match:
        data["match"]   = match
        data["matched"] = matched
        if deadline is not None:
            data["timed_out"] = not matched
    return {"ok": True, "data": data}


@handler(
    "target_trigger_capture",
    description="Trigger one or more frame captures on a running target process.",
    schema={
        "properties": {
            "ident"        : {"type": "integer", "description": "Target ident from targets_list. Required."},
            "host"         : {"type": "string",  "description": "Host of the target. Empty = localhost."},
            "num_frames"   : {"type": "integer", "description": "How many sequential frames to capture (default 1). Each lands as its own .rdc."},
            "frame_number" : {"type": "integer", "description": "If set, QueueCapture(frame_number, num_frames) instead of TriggerCapture(num_frames)."},
            "wait_secs"    : {"type": "number",  "description": "How long to keep the connection open waiting for NewCapture messages. Default 10s. Capped at 300s."},
            "copy_to"      : {"type": "string",  "description": "Optional local directory. Each capture is CopyCapture()'d to <copy_to>/<remote_basename> after arrival. If omitted, returns the on-target path only."},
            "force"        : {"type": "boolean", "description": "Pass forceConnection=True to CreateTargetControl. Steals the connection from any currently-attached client. Default False."},
            "include_thumbnails" : {"type": "boolean", "description": "Include the embedded thumbnail (base64 RGB8) for each arrived capture. Default False."},
        },
        "required": ["ident"],
    },
)
def handle_target_trigger_capture(ctx, params):
    # type: (Any, Dict[str, Any]) -> Dict[str, Any]
    """Trigger a capture on a running target and wait for the file(s).

    Sequence:
      1. Open ITargetControl to (host, ident).
      2. TriggerCapture(num_frames) or QueueCapture(frame_number, num_frames).
      3. Loop ReceiveMessage(progress=None) up to wait_secs wall-clock,
         collecting NewCapture messages until num_frames have arrived
         or the deadline hits.
      4. (Optional) CopyCapture each arrival to copy_to/.
      5. Shutdown the control connection.

    Returns a list of arrived captures with on-target path, frame
    number, captureId, size, and local_path (if copy_to was set).

    The handler runs on the bridge handler thread and holds the
    _dispatch_lock for its full wait_secs duration — other MCP calls
    are blocked while this runs. Keep wait_secs modest, or fire from
    a dedicated tab.
    """
    import base64
    import os
    import time
    import renderdoc as rd

    ident = params.get("ident")
    if not ident:
        return {"ok": False, "error": "ident is required (see Instance(action='targets'))"}

    host           = params.get("host") or ""
    num_frames     = max(1, int(params.get("num_frames", 1)))
    frame_number   = params.get("frame_number")
    wait_secs      = max(0.5, min(300.0, float(params.get("wait_secs", 10.0))))
    copy_to        = params.get("copy_to")
    force          = bool(params.get("force", False))
    include_thumbs = bool(params.get("include_thumbnails", False))

    if copy_to:
        if not os.path.isdir(copy_to):
            return {"ok": False, "error": "copy_to is not a directory: {}".format(copy_to)}

    control = None
    try:
        control = rd.CreateTargetControl(host, int(ident),
                                         "agentic-renderdoc", force)
    except Exception as e:
        return {"ok": False, "error": "CreateTargetControl failed: {}".format(e)}
    if control is None:
        return {
            "ok"    : False,
            "error" : "could not open target control to ident {}; "
                      "another client may hold it (try force=True)".format(ident),
        }

    arrived = []
    try:
        target_name = ""
        try:
            target_name = str(control.GetTarget())
        except Exception:
            pass

        try:
            if frame_number is not None:
                control.QueueCapture(int(frame_number), num_frames)
                mode = "queued at frame {}".format(int(frame_number))
            else:
                control.TriggerCapture(num_frames)
                mode = "triggered"
        except Exception as e:
            return {"ok": False, "error": "trigger failed: {}".format(e)}

        deadline = time.time() + wait_secs
        while time.time() < deadline and len(arrived) < num_frames:
            try:
                msg = control.ReceiveMessage(None)
            except Exception as e:
                return {"ok": False, "error": "ReceiveMessage failed: {}".format(e),
                        "arrived": arrived}

            if msg is None:
                continue

            mtype = getattr(msg.type, "name", str(msg.type)) if msg.type is not None else ""

            if mtype == "Disconnected":
                break

            if mtype != "NewCapture":
                # Noop, CaptureProgress, RegisterAPI, etc. — keep pumping.
                continue

            nc = msg.newCapture
            entry = {
                "captureId"    : int(nc.captureId),
                "frameNumber"  : int(nc.frameNumber),
                "timestamp"    : int(nc.timestamp),
                "byteSize"     : int(nc.byteSize),
                "target_path"  : str(nc.path),
                "title"        : str(getattr(nc, "title", "")),
                "thumb_width"  : int(getattr(nc, "thumbWidth", 0)),
                "thumb_height" : int(getattr(nc, "thumbHeight", 0)),
            }
            if include_thumbs and nc.thumbnail:
                try:
                    entry["thumbnail_rgb8_b64"] = base64.b64encode(
                        bytes(nc.thumbnail)).decode("ascii")
                except Exception:
                    pass

            # Optional CopyCapture to local directory.
            if copy_to:
                local_basename = os.path.basename(entry["target_path"]) or \
                                 "capture_{}.rdc".format(entry["captureId"])
                local_path = os.path.join(copy_to, local_basename)
                try:
                    control.CopyCapture(entry["captureId"], local_path)
                    entry["local_path"] = local_path
                except Exception as e:
                    entry["copy_error"] = str(e)

            arrived.append(entry)

        return {
            "ok"   : True,
            "data" : {
                "ident"       : int(ident),
                "target"      : target_name,
                "mode"        : mode,
                "requested"   : num_frames,
                "captures"    : arrived,
                "complete"    : len(arrived) >= num_frames,
                "waited_secs" : round(wait_secs, 2),
            },
        }
    finally:
        try:
            control.Shutdown()
        except Exception:
            pass


# --- reload (dev only) ---

@handler(
    "reload",
    description="Hot-reload extension modules without restarting RenderDoc.",
    schema={},
)
def handle_reload(ctx: Any, params: Dict[str, Any]) -> Dict[str, Any]:
    """Reload business-logic modules in dependency order.

    Leaves winsock and bridge untouched (infrastructure). Mutates the
    HANDLERS dict in-place so the bridge's reference stays valid.
    Rebuilds the API index and clears the serializer cache.
    """
    import importlib
    from . import serialize, concepts, api_index, utilities

    # Reload in dependency order: leaves first, then this module.
    # concepts before api_index: build_index() imports CONCEPTS from it,
    # so the curated entries only refresh if concepts is reloaded first.
    importlib.reload(serialize)
    importlib.reload(concepts)
    importlib.reload(api_index)
    importlib.reload(utilities)

    # Save reference to the dict the bridge is holding.
    old_handlers = HANDLERS

    # Reload this module. This re-runs all @handler decorators into a
    # fresh HANDLERS dict inside the new module object.
    from . import handlers as _self
    importlib.reload(_self)

    # Splice the new registrations into the old dict object.
    old_handlers.clear()
    old_handlers.update(_self.HANDLERS)

    # Rebuild the API index with the reloaded api_index module.
    from .api_index import build_index
    ctx._api_index = build_index()

    # Clear the serializer cache so it picks up the reloaded serialize module.
    global _SERIALIZER_MAP
    _SERIALIZER_MAP = None

    return {
        "ok"   : True,
        "data" : {
            "reloaded"  : ["serialize", "concepts", "api_index", "utilities", "handlers"],
            "handlers"  : list(old_handlers.keys()),
        },
    }


# --- shutdown ---

@handler(
    "shutdown",
    description="Request the bridge server to stop accepting connections and exit.",
    schema={},
)
def handle_shutdown(ctx: Any, params: Dict[str, Any]) -> Dict[str, Any]:
    """Trigger a graceful bridge shutdown.

    For headless workers, this also tears down the replay thread and
    closes the capture file. For the GUI extension, only the bridge
    stops; RenderDoc itself keeps running.

    The handler returns before the bridge actually stops; the caller
    should treat the connection as closed shortly after receiving the
    response.
    """
    bridge = getattr(ctx, "_bridge", None)

    # Run the actual stop in a separate thread so the response can be
    # written before the server socket closes.
    if bridge is not None:
        def _stop() -> None:
            try:
                bridge.stop()
            except Exception:
                traceback.print_exc()
            # Headless: also tear down the replay controller and capture.
            if getattr(ctx, "headless", False):
                shutdown_fn = getattr(ctx, "shutdown", None)
                if callable(shutdown_fn):
                    try:
                        shutdown_fn()
                    except Exception:
                        traceback.print_exc()

        import threading
        threading.Thread(target=_stop, daemon=True, name="agentic-shutdown").start()

    return {"ok": True, "data": {"shutting_down": True}}


# --- Internal helpers ---

# Methods on qrenderdoc.CaptureContext that mutate UI/replay state and MUST
# run on the Qt UI thread. Calling them from Eval (which runs on the bridge
# handler thread) crashes RenderDoc — Qt has no thread-affinity check in
# release builds. The proxy below intercepts them and points the agent at
# the Instance() actions, which route through invoke_ui correctly.
_CTX_FORBIDDEN_FROM_EVAL = {
    "LoadCapture"  : "use Instance(action='load_capture', file=...) — raw LoadCapture re-enters the replay lifecycle and deadlocks",
    "CloseCapture" : "use Instance(action='close_capture') — raw CloseCapture runs off the Qt UI thread and crashes RenderDoc (observed CTD)",
}


class _SafeCaptureContext(object):
    """Proxy for qrenderdoc.CaptureContext that intercepts known footguns.

    Forwards everything else (Config(), Replay(), GetCaptureFilename(),
    GetStructuredFile(), …) untouched. Only the methods listed in
    _CTX_FORBIDDEN_FROM_EVAL raise with a redirect to the safe MCP path.
    """

    def __init__(self, real):
        object.__setattr__(self, "_real", real)

    def __getattr__(self, name):
        if name in _CTX_FORBIDDEN_FROM_EVAL:
            redirect = _CTX_FORBIDDEN_FROM_EVAL[name]
            def _refuse(*args, **kwargs):
                raise RuntimeError(
                    "ctx.ctx.{}() is not safe from Eval: {}".format(name, redirect)
                )
            return _refuse
        return getattr(self._real, name)

    def __dir__(self):
        return sorted(set(dir(self._real)) | set(_CTX_FORBIDDEN_FROM_EVAL))


class _EvalHandlerContext(object):
    """Proxy for HandlerContext exposed to user code in Eval.

    On GUI contexts, routes the ``.ctx`` attribute through
    _SafeCaptureContext so the documented footguns are intercepted. On
    headless contexts, there is no Qt CaptureContext to wrap — the
    proxy is a pass-through. All other attributes (replay, invoke_ui,
    capture_loaded, structured_file, …) forward to the real
    HandlerContext unchanged so pre-loaded utilities and ctx.replay()
    keep working exactly as before.
    """

    def __init__(self, real):
        object.__setattr__(self, "_real", real)
        real_ctx = getattr(real, "ctx", None)
        if real_ctx is not None and not getattr(real, "headless", False):
            object.__setattr__(self, "_safe_ctx", _SafeCaptureContext(real_ctx))
        else:
            object.__setattr__(self, "_safe_ctx", real_ctx)

    @property
    def ctx(self):
        return self._safe_ctx

    def __getattr__(self, name):
        return getattr(self._real, name)

    def __dir__(self):
        return dir(self._real)


def _build_namespace(ctx: Any, captured_output: List[str]) -> Dict[str, Any]:
    """Build the execution namespace for eval, including utilities.

    Injects RenderDoc modules (rd, qrd), the handler context, the serialize
    module, and a print override that captures output to captured_output so
    it can be included in the response.

    ctx             -- HandlerContext shared with all handlers.
    captured_output -- Mutable list; print calls append strings here.
    """
    # Expose a guarded view of HandlerContext: pre-loaded utilities still
    # bind to the raw ctx via closure (bind_utilities below), so this only
    # affects user-typed code that touches ctx.ctx directly.
    ns = {"ctx": _EvalHandlerContext(ctx)}

    # RenderDoc modules are available globally inside the extension environment.
    # Import them into the namespace so eval code can use them directly.
    try:
        import renderdoc as rd
        ns["rd"] = rd
    except ImportError:
        pass

    try:
        import qrenderdoc as qrd
        ns["qrd"] = qrd
    except ImportError:
        pass

    # Expose the serialize module so agents can call serializers directly.
    try:
        from . import serialize
        ns["serialize"] = serialize
    except ImportError:
        pass

    # Inject utility functions (inspect, diff_state, goto_event, etc.)
    # bound to this handler context.
    try:
        from .utilities import bind_utilities
        ns.update(bind_utilities(ctx))
    except ImportError:
        pass

    # Override print to capture output alongside the eval result.
    def _capture_print(*args, **kwargs):
        """Replacement print that appends to captured_output."""
        sep = kwargs.get("sep", " ")
        end = kwargs.get("end", "\n")
        captured_output.append(sep.join(str(a) for a in args) + end)

    ns["print"] = _capture_print

    return ns


def _dry_run(code: str, namespace: Dict[str, Any]) -> Dict[str, Any]:
    """Parse + static-check user code without executing it.

    Verifies the code parses, then walks the AST collecting free names
    (identifiers that aren't bound by an assignment / import / function
    or class def inside the snippet). Any that aren't in the eval
    namespace or Python builtins are reported as likely typos.

    Caller has already established the namespace via _build_namespace,
    so this catches everything that wouldn't resolve at runtime — typos,
    forgotten imports, references to utilities that don't exist —
    without paying for any ctx.replay() or SetFrameEvent.
    """
    import ast
    import builtins

    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return {
            "ok"    : False,
            "error" : {
                "kind"         : "syntax",
                "message"      : str(e),
                "line"         : e.lineno,
                "column"       : e.offset,
                "failing_line" : (e.text or "").rstrip("\n"),
            },
        }

    bound  = set()  # type: set
    free   = []     # type: List[str]
    free_lines = {} # type: Dict[str, int]

    class _Walker(ast.NodeVisitor):
        def _bind(self, name):
            bound.add(name)

        def visit_Assign(self, node):
            for target in node.targets:
                for n in ast.walk(target):
                    if isinstance(n, ast.Name):
                        self._bind(n.id)
            self.generic_visit(node)

        def visit_AugAssign(self, node):
            if isinstance(node.target, ast.Name):
                self._bind(node.target.id)
            self.generic_visit(node)

        def visit_AnnAssign(self, node):
            if isinstance(node.target, ast.Name):
                self._bind(node.target.id)
            self.generic_visit(node)

        def visit_For(self, node):
            for n in ast.walk(node.target):
                if isinstance(n, ast.Name):
                    self._bind(n.id)
            self.generic_visit(node)

        def visit_FunctionDef(self, node):
            self._bind(node.name)
            for arg in node.args.args + node.args.kwonlyargs:
                self._bind(arg.arg)
            if node.args.vararg:
                self._bind(node.args.vararg.arg)
            if node.args.kwarg:
                self._bind(node.args.kwarg.arg)
            self.generic_visit(node)

        def visit_AsyncFunctionDef(self, node):
            self.visit_FunctionDef(node)

        def visit_ClassDef(self, node):
            self._bind(node.name)
            self.generic_visit(node)

        def visit_Import(self, node):
            for alias in node.names:
                self._bind((alias.asname or alias.name).split(".")[0])

        def visit_ImportFrom(self, node):
            for alias in node.names:
                self._bind(alias.asname or alias.name)

        def visit_Lambda(self, node):
            for arg in node.args.args + node.args.kwonlyargs:
                self._bind(arg.arg)
            self.generic_visit(node)

        def visit_comprehension(self, node):
            for n in ast.walk(node.target):
                if isinstance(n, ast.Name):
                    self._bind(n.id)

        def visit_Name(self, node):
            if isinstance(node.ctx, ast.Load):
                if node.id not in bound and node.id not in free_lines:
                    free.append(node.id)
                    free_lines[node.id] = node.lineno

    _Walker().visit(tree)

    available = set(namespace) | set(dir(builtins))
    unbound = [n for n in free if n not in available]

    response = {
        "ok"   : True,
        "data" : {
            "parsed"       : True,
            "statements"   : len(tree.body),
            "free_names"   : sorted(set(free)),
            "unbound"      : sorted(set(unbound)),
        },
    }
    if unbound:
        hints = ["use search_api(...) to find the right symbol"]
        response["data"]["hints"]      = hints
        response["data"]["unbound_at"] = {n: free_lines[n] for n in unbound}
    return response


def _exec_with_result(code: str, namespace: Dict[str, Any]) -> Any:
    """Execute code and return the value of the last expression, if any.

    Splits the code into statements and the final expression. Executes all
    statements, then evaluates the final expression and returns its value.

    If the final line is NOT an expression (e.g. ``result = {...}``), the
    last-expression channel yields nothing, so we honor the ``result`` /
    ``_`` convention: whichever of those names the code bound is returned.
    This makes both the "end with a bare expression" and "assign ``result``"
    patterns work. If neither is bound, returns None.
    """
    import ast

    tree = ast.parse(code)
    if not tree.body:
        return None

    last = tree.body[-1]

    # If the last node is an expression, split it off and eval it separately.
    if isinstance(last, ast.Expr):
        stmts = ast.Module(body=tree.body[:-1], type_ignores=[])
        expr  = ast.Expression(body=last.value)

        exec(compile(stmts, "<eval>", "exec"), namespace)
        return eval(compile(expr, "<eval>", "eval"), namespace)
    else:
        exec(compile(tree, "<eval>", "exec"), namespace)
        # Last statement isn't an expression. Fall back to the conventional
        # sentinel names so `result = {...}` (a very natural final line) is
        # returned instead of a silent null.
        if "result" in namespace:
            return namespace["result"]
        if "_" in namespace:
            return namespace["_"]
        return None


# AST node types whose presence as the final statement means the code ended
# with an assignment rather than a value-producing expression.
_ASSIGN_NODES = (ast.Assign, ast.AugAssign, ast.AnnAssign)


def _last_node_is_assignment(code: str) -> bool:
    """True if the final top-level statement is an assignment.

    Used to attach a helpful hint when Eval returns None: the most common
    cause is ending the block with ``result = ...`` and expecting it back.
    """
    import ast

    try:
        tree = ast.parse(code)
    except SyntaxError:
        return False
    return bool(tree.body) and isinstance(tree.body[-1], _ASSIGN_NODES)


def _format_error(exc: Exception, code: str, namespace: Dict[str, Any]) -> Dict[str, Any]:
    """Format an exception with stack trace, failing line, and contextual hints.

    Returns a dict with:
    - traceback:    full formatted traceback string
    - failing_line: the specific source line that failed (if extractable)
    - hints:        list of hint strings for common RenderDoc mistakes

    exc       -- The caught exception.
    code      -- The source code that was executed.
    namespace -- The execution namespace (used for NameError hints).
    """
    tb_lines  = traceback.format_exception(type(exc), exc, exc.__traceback__)
    formatted = "".join(tb_lines)
    msg       = str(exc)

    # Extract the user's failing source line from the traceback.
    failing_line = _extract_failing_line(exc, code)

    # Build contextual hints.
    hints = []

    # Missing SetFrameEvent before pipeline state queries.
    is_attr_error = isinstance(exc, AttributeError)
    if is_attr_error and "SetFrameEvent" not in code:
        hints.append(
            "did you call SetFrameEvent before querying pipeline state?"
        )

    # Threading / replay controller access.
    if "BlockInvoke" in msg or "replay" in msg.lower():
        hints.append(
            "use ctx.replay(callback) to access the replay controller"
        )

    # Generic AttributeError guidance.
    if is_attr_error:
        hints.append(
            "use inspect(obj) to see available attributes, "
            "or search_api('name') to find the right API"
        )

    # SyntaxError guidance.
    if isinstance(exc, SyntaxError):
        hint = "check Python syntax near the indicated position"
        if exc.offset is not None:
            hint += f" (column {exc.offset})"
        hints.append(hint)

    # NameError with available globals.
    if isinstance(exc, NameError):
        available = sorted(
            k for k in namespace
            if not k.startswith("_")
        )
        hints.append(
            f"available names: {', '.join(available)}"
        )

    return {
        "traceback"    : formatted,
        "failing_line" : failing_line,
        "hints"        : hints,
    }


def _extract_failing_line(exc: Exception, code: str = "") -> Optional[str]:
    """Extract the source line that caused the exception.

    Prefers frames from the user's eval code (filename ``<eval>``). Falls
    back to the last traceback frame if no eval frame is found. For syntax
    errors, the offending line is pulled from the exception itself.

    The ``<eval>`` frames have no backing source file, so linecache can't
    populate frame_summary.line. Instead we use the frame's lineno to
    index into the original source code.

    exc  -- The caught exception.
    code -- The original source code submitted by the user.
    """
    # SyntaxError carries the offending source line directly.
    if isinstance(exc, SyntaxError) and exc.text:
        return exc.text.strip()

    tb = exc.__traceback__
    if tb is None:
        return None

    code_lines = code.splitlines() if code else []

    # Walk frames, preferring <eval> frames over internal ones.
    best = None
    for frame_summary in traceback.extract_tb(tb):
        if frame_summary.filename == "<eval>":
            # Extract from original source since linecache can't find <eval>.
            lineno = frame_summary.lineno
            if code_lines and 1 <= lineno <= len(code_lines):
                best = code_lines[lineno - 1].strip()
            elif frame_summary.line:
                best = frame_summary.line
        elif best is None and frame_summary.line:
            best = frame_summary.line

    return best


def _get_serializer_map() -> Dict[str, Callable[..., Any]]:
    """Lazily build the type-name-to-serializer dispatch table.

    Deferred because the serialize module imports renderdoc at the top
    level, which may not be available during testing.
    """
    global _SERIALIZER_MAP
    if _SERIALIZER_MAP is not None:
        return _SERIALIZER_MAP

    try:
        from . import serialize
        _SERIALIZER_MAP = {
            "ResourceId"         : serialize.resource_id,
            "ActionDescription"  : serialize.action_description,
            "TextureDescription" : serialize.texture_description,
            "BufferDescription"  : serialize.buffer_description,
            "ShaderReflection"   : serialize.shader_reflection,
            "ResourceFormat"     : serialize.format_description,
            "PipeState"          : serialize.pipeline_state,
            "APIProperties"      : serialize.api_properties,
        }
    except ImportError:
        _SERIALIZER_MAP = {}

    return _SERIALIZER_MAP


def _serialize_result(value: Any) -> Any:
    """Convert an eval result to a JSON-serializable value.

    Handles basic JSON types, RenderDoc SWIG types (via the serialize
    module), SWIG enums, and falls back to repr() for anything else.
    """
    # None passes through.
    if value is None:
        return value

    # SWIG enums: inherit from int but also expose .name. Must be checked
    # before the basic int check, since isinstance(swig_enum, int) is True.
    if isinstance(value, int) and hasattr(value, "name"):
        return {"name": value.name, "value": int(value)}

    # Basic JSON scalars pass through.
    if isinstance(value, (bool, int, float, str)):
        return value

    # Recursively serialize lists.
    if isinstance(value, list):
        return [_serialize_result(v) for v in value]

    # Recursively serialize dicts.
    if isinstance(value, dict):
        return {
            _serialize_result(k): _serialize_result(v)
            for k, v in value.items()
        }

    # Known RenderDoc types: dispatch to the matching serializer.
    type_name      = type(value).__name__
    serializer_map = _get_serializer_map()
    serializer     = serializer_map.get(type_name)
    if serializer is not None:
        try:
            return serializer(value)
        except Exception:
            # Serialization failed; fall through to repr.
            pass

    # Try attribute-based serialization for unknown SWIG types.
    # SWIG objects can throw on attribute access, so be defensive.
    try:
        attrs = {}
        for name in dir(value):
            if name.startswith("_"):
                continue
            if name in ("thisown", "this"):
                continue
            try:
                attr_val = getattr(value, name)
                if not callable(attr_val):
                    attrs[name] = _serialize_result(attr_val)
            except Exception:
                continue
        if attrs:
            return {"__type__": type_name, **attrs}
    except Exception:
        pass

    # Fallback for anything we don't recognize.
    return repr(value)
