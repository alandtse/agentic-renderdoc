"""Command handlers for the RenderDoc bridge extension.

Three handlers: eval, api_index, instance_info.
"""

import traceback

from .api_index import search_index

# Populated at registration time.
HANDLERS = {}

# Serializer dispatch table, keyed by RenderDoc type name.
# Built lazily on first use since the serialize module imports renderdoc,
# which may not be available during testing.
_SERIALIZER_MAP = None


def handler(name, description="", schema=None):
    """Decorator to register a command handler."""
    def decorator(func):
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
        "properties": {"code": {"type": "string", "description": "Python code to execute."}},
        "required":   ["code"],
    },
)
def handle_eval(ctx, params):
    """Execute arbitrary Python and return the result of the last expression.

    Builds a namespace with RenderDoc globals and utility modules, runs the
    code, serializes the result for JSON transport, and captures any print
    output. Returns error info with contextual hints on failure.
    """
    code = params.get("code", "")
    if not code:
        return {"error": "no code provided"}

    # Build the execution namespace with utilities and RenderDoc globals.
    captured_output = []
    namespace       = _build_namespace(ctx, captured_output)

    try:
        raw_result = _exec_with_result(code, namespace)
        result     = _serialize_result(raw_result)

        response = {"ok": True, "data": result}
        if captured_output:
            response["output"] = captured_output
        return response
    except Exception as e:
        response = {
            "ok"    : False,
            "error" : _format_error(e, code, namespace),
        }
        if captured_output:
            response["output"] = captured_output
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
def handle_api_index(ctx, params):
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
def handle_instance_info(ctx, params):
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
        },
    }


# --- Internal helpers ---

def _build_namespace(ctx, captured_output):
    """Build the execution namespace for eval, including utilities.

    Injects RenderDoc modules (rd, qrd, pyrenderdoc), the handler context,
    the serialize module, and a print override that captures output to
    captured_output so it can be included in the response.

    ctx             -- HandlerContext shared with all handlers.
    captured_output -- Mutable list; print calls append strings here.
    """
    ns = {"ctx": ctx}

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

    # The pyrenderdoc global is injected by RenderDoc into the extension
    # environment. It may not exist during testing.
    import builtins
    if hasattr(builtins, "pyrenderdoc"):
        ns["pyrenderdoc"] = builtins.pyrenderdoc

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


def _exec_with_result(code, namespace):
    """Execute code and return the value of the last expression, if any.

    Splits the code into statements and the final expression. Executes all
    statements, then evaluates the final expression and returns its value.
    If the final line is not an expression, returns None.
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
        return None


def _format_error(exc, code, namespace):
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

    # Try to extract the failing line from the traceback. The last
    # "File "<eval>"" frame usually contains the offending source line.
    failing_line = _extract_failing_line(tb_lines)

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
            "use pyrenderdoc.Replay().BlockInvoke(callback) "
            "to access the replay controller"
        )

    # Generic AttributeError guidance.
    if is_attr_error:
        hints.append(
            "use inspect(obj) to see available attributes, "
            "or search_api('name') to find the right API"
        )

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


def _extract_failing_line(tb_lines):
    """Extract the source line that caused the exception from traceback lines.

    Walks the formatted traceback backwards looking for the last line that
    came from our eval context. Returns None if no source line is found.
    """
    # Formatted traceback lines alternate between "File ..." location lines
    # and indented source lines. We want the last source line from <eval>.
    found_eval = False
    for line in reversed(tb_lines):
        # Each element from format_exception may contain multiple lines.
        for subline in reversed(line.splitlines()):
            stripped = subline.strip()
            if found_eval and stripped and not stripped.startswith("File "):
                return stripped
            if '<eval>' in subline:
                found_eval = True

    return None


def _get_serializer_map():
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


def _serialize_result(value):
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

    # Fallback for anything we don't recognize.
    return repr(value)
