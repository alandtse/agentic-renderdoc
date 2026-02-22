"""Command handlers for the RenderDoc bridge extension.

Three handlers: eval, api_index, instance_info.
"""

# Populated at registration time.
HANDLERS = {}


def handler(name, description="", schema=None):
    """Decorator to register a command handler."""
    def decorator(func):
        HANDLERS[name] = {
            "func":        func,
            "description": description,
            "schema":      schema or {},
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
    """Execute arbitrary Python and return the result of the last expression."""
    code = params.get("code", "")
    if not code:
        return {"error": "no code provided"}

    # Build the execution namespace with utilities and RenderDoc globals.
    namespace = _build_namespace(ctx)

    try:
        result = _exec_with_result(code, namespace)
        return {"ok": True, "data": result}
    except Exception as e:
        return {
            "ok":    False,
            "error": _format_error(e, code),
        }


# --- api_index ---

@handler(
    "api_index",
    description="Search the RenderDoc Python API reference.",
    schema={
        "properties": {"query": {"type": "string", "description": "Search term."}},
        "required":   ["query"],
    },
)
def handle_api_index(ctx, params):
    """Search the cached API index for matching entries."""
    query = params.get("query", "").lower()
    if not query:
        return {"error": "no query provided"}

    # TODO: Search the pre-built index (ctx.api_index).
    return {"ok": True, "data": []}


# --- instance_info ---

@handler(
    "instance_info",
    description="Return metadata about this RenderDoc instance.",
    schema={},
)
def handle_instance_info(ctx, params):
    """Return port, capture state, and API type."""
    # TODO: Pull from ctx.
    return {
        "ok":   True,
        "data": {
            "port":           getattr(ctx, "_server_port", None),
            "capture_loaded": getattr(ctx, "_capture_loaded", False),
        },
    }


# --- Internal helpers ---

def _build_namespace(ctx):
    """Build the execution namespace for eval, including utilities."""
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

    # TODO: Pre-load utility functions (inspect, diff_state, goto_event, etc.)

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


def _format_error(exc, code):
    """Format an exception with stack trace, failing line, and hint.

    Follows the Rust compiler model: show what went wrong, where, and
    what to try.
    """
    import traceback

    tb = traceback.format_exception(type(exc), exc, exc.__traceback__)
    formatted = "".join(tb)

    # TODO: Add contextual hints for common RenderDoc mistakes.
    # e.g., "did you call SetFrameEvent before querying pipeline state?"

    return formatted
