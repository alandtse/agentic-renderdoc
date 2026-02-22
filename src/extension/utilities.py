"""Utility functions injected into the eval handler's namespace.

Provides runtime introspection, pipeline state diffing, data interpretation,
and UI navigation helpers. Functions that need access to the replay controller
or UI thread are bound to a HandlerContext via closures.
"""

import inspect as _inspect
import math
import struct


try:
    import renderdoc as rd
except ImportError:
    rd = None

try:
    import builtins as _builtins
except ImportError:
    _builtins = None


# --- Introspection ---

# Attributes to suppress from SWIG-wrapped RenderDoc types.
_SWIG_INTERNAL = frozenset({"thisown", "this"})


def inspect_obj(obj):
    """Inspect any Python object and return a structured summary.

    For classes and instances: type name, methods (with signatures and
    first-line docstrings), and properties. Filters out dunder names and
    SWIG internal attributes.

    For enums (detected by __members__ or int inheritance): type name and
    a list of name/value pairs.

    For modules: type "module" with lists of classes, functions, and
    constants.

    Returns a plain dict, not a string.
    """
    # Enum detection: has __members__ dict or inherits from int with
    # class-level named values (SWIG enum pattern).
    if hasattr(obj, "__members__"):
        return _inspect_enum(obj)

    if isinstance(obj, type) and issubclass(obj, int) and obj is not int:
        # SWIG enums are int subclasses with class-level named constants.
        members = _extract_int_enum_members(obj)
        if members:
            return {
                "type"   : obj.__name__,
                "values" : members,
            }

    # Module detection.
    if _inspect.ismodule(obj):
        return _inspect_module(obj)

    # Class or instance.
    return _inspect_object(obj)


def _inspect_enum(obj):
    """Inspect an enum type that exposes __members__."""
    type_name = getattr(obj, "__name__", type(obj).__name__)
    members   = obj.__members__

    values = []
    for name in sorted(members):
        member = members[name]
        values.append({
            "name"  : name,
            "value" : int(member) if isinstance(member, int) else str(member),
        })

    return {
        "type"   : type_name,
        "values" : values,
    }


def _extract_int_enum_members(cls):
    """Extract named constants from a SWIG int-enum class.

    Returns a list of {name, value} dicts, or an empty list if this
    doesn't look like an enum.
    """
    members = []
    for name in dir(cls):
        if name.startswith("_"):
            continue
        val = getattr(cls, name, None)
        if isinstance(val, int):
            members.append({"name": name, "value": int(val)})
    return members


def _inspect_module(mod):
    """Inspect a module, grouping contents into classes, functions, and constants."""
    classes   = []
    functions = []
    constants = []

    for name in sorted(dir(mod)):
        if name.startswith("_"):
            continue

        attr = getattr(mod, name, None)
        if attr is None:
            continue

        if _inspect.isclass(attr):
            classes.append(name)
        elif callable(attr):
            functions.append(name)
        else:
            constants.append(name)

    return {
        "type"      : "module",
        "classes"   : classes,
        "functions" : functions,
        "constants" : constants,
    }


def _inspect_object(obj):
    """Inspect a class or instance, extracting methods and properties."""
    # Resolve the type for attribute enumeration.
    if isinstance(obj, type):
        cls       = obj
        type_name = cls.__name__
    else:
        cls       = type(obj)
        type_name = cls.__name__

    methods    = []
    properties = []

    for name in sorted(dir(obj)):
        # Skip dunder names and SWIG internals.
        if name.startswith("__") and name.endswith("__"):
            continue
        if name.startswith("_") or name in _SWIG_INTERNAL:
            continue

        attr = getattr(cls, name, None)
        if attr is None:
            # Fall back to the instance if the class doesn't have it.
            attr = getattr(obj, name, None)
            if attr is None:
                continue

        if isinstance(attr, property):
            doc = _first_line(attr.fget.__doc__) if attr.fget else None
            properties.append({"name": name, "doc": doc})
        elif callable(attr):
            sig = _get_signature(attr)
            doc = _first_line(attr.__doc__)
            methods.append({"name": name, "signature": sig, "doc": doc})
        else:
            # Might be a SWIG descriptor or similar. Treat as property.
            doc = _first_line(getattr(attr, "__doc__", None))
            properties.append({"name": name, "doc": doc})

    return {
        "type"       : type_name,
        "methods"    : methods,
        "properties" : properties,
    }


def _get_signature(func):
    """Get a function's call signature as a string.

    Tries inspect.signature first. Falls back to parsing the first line
    of the docstring for SWIG-generated signatures. Returns None if
    neither works.
    """
    try:
        return str(_inspect.signature(func))
    except (ValueError, TypeError):
        pass

    # SWIG docstrings often start with "name(args) -> return_type".
    doc = getattr(func, "__doc__", None)
    if doc:
        first = doc.strip().split("\n")[0]
        if "(" in first and ")" in first:
            return first

    return None


def _first_line(doc):
    """Return the first non-empty line of a docstring, or None."""
    if not doc:
        return None
    for line in doc.strip().split("\n"):
        stripped = line.strip()
        if stripped:
            return stripped
    return None


# --- Pipeline State Diffing ---

def _deep_diff(a, b):
    """Recursively diff two dicts, returning only changed paths.

    For nested dicts, recurses and only includes keys whose subtrees
    contain actual changes. For non-dict values, compares directly and
    returns {"before": a, "after": b} when they differ.

    Returns None if the two values are equal.
    """
    if type(a) is dict and type(b) is dict:
        diff = {}
        all_keys = set(a) | set(b)

        for key in sorted(all_keys, key=str):
            val_a = a.get(key)
            val_b = b.get(key)

            if val_a is None and val_b is not None:
                diff[key] = {"before": None, "after": val_b}
            elif val_a is not None and val_b is None:
                diff[key] = {"before": val_a, "after": None}
            else:
                sub = _deep_diff(val_a, val_b)
                if sub is not None:
                    diff[key] = sub

        return diff if diff else None

    # Lists: compare element-wise. If lengths differ or any element
    # differs, report the whole list as changed.
    if type(a) is list and type(b) is list:
        if a == b:
            return None
        return {"before": a, "after": b}

    # Scalar comparison.
    if a != b:
        return {"before": a, "after": b}

    return None


def make_diff_state(ctx):
    """Create a diff_state function bound to the given HandlerContext.

    The returned function captures ctx and manages the replay callback
    internally, so the caller just passes two event IDs.

    ctx -- HandlerContext with replay() access.
    """
    def diff_state(eid_a, eid_b):
        """Diff pipeline state between two events.

        Moves the replay cursor to each event, snapshots the full
        pipeline state via serialize.pipeline_state(), and returns a
        recursive diff containing only the keys that changed.

        eid_a -- First event ID.
        eid_b -- Second event ID.
        """
        from . import serialize

        def _snapshot_both(controller):
            controller.SetFrameEvent(eid_a, True)
            state_a = serialize.pipeline_state(controller.GetPipelineState())

            controller.SetFrameEvent(eid_b, True)
            state_b = serialize.pipeline_state(controller.GetPipelineState())

            return (state_a, state_b)

        state_a, state_b = ctx.replay(_snapshot_both)
        diff              = _deep_diff(state_a, state_b)

        return diff if diff is not None else {}

    return diff_state


# --- Data Interpretation ---

# Integer format codes indexed by byte width. Used when the component
# type is an integer variant and the byte width isn't the default 4.
_UINT_BY_WIDTH = {1: "B", 2: "H", 4: "I", 8: "Q"}
_SINT_BY_WIDTH = {1: "b", 2: "h", 4: "i", 8: "q"}


def interpret_buffer(data, fmt):
    """Decode raw buffer bytes into typed values.

    data -- bytes from GetBufferData.
    fmt  -- either a renderdoc.ResourceFormat object or a dict with keys:
            component_type (str), component_count (int),
            component_byte_width (int).

    Returns a list of values for single-component formats, or a list of
    tuples for multi-component formats.
    """
    if isinstance(fmt, dict):
        comp_type  = fmt.get("component_type", "Float")
        comp_count = fmt.get("component_count", 1)
        comp_width = fmt.get("component_byte_width", 4)
    else:
        # ResourceFormat object.
        comp_type  = fmt.compType.name if hasattr(fmt.compType, "name") else str(fmt.compType)
        comp_count = fmt.compCount
        comp_width = fmt.compByteWidth

    # Determine the struct format character.
    if comp_type in ("UInt", "UByte"):
        fmt_char = _UINT_BY_WIDTH.get(comp_width, "I")
    elif comp_type in ("SInt", "SByte"):
        fmt_char = _SINT_BY_WIDTH.get(comp_width, "i")
    elif comp_type == "Double":
        fmt_char = "d"
    else:
        # Float, UNorm, SNorm all decode as float.
        fmt_char = "f"

    stride      = comp_count * comp_width
    elem_count  = len(data) // stride if stride > 0 else 0
    pack_fmt    = f"<{comp_count}{fmt_char}"

    result = []
    for i in range(elem_count):
        offset = i * stride
        values = struct.unpack_from(pack_fmt, data, offset)

        if comp_count == 1:
            result.append(values[0])
        else:
            result.append(values)

    return result


def summarize_data(values):
    """Summarize a flat list of numbers.

    Returns a dict with min, max, mean, count, nan_count, and inf_count.
    Useful for quick inspection of buffer or texture data.

    values -- flat list (or iterable) of numeric values.
    """
    total     = 0.0
    count     = 0
    nan_count = 0
    inf_count = 0
    lo        = float("inf")
    hi        = float("-inf")

    for v in values:
        count += 1
        fv     = float(v)

        if math.isnan(fv):
            nan_count += 1
            continue
        if math.isinf(fv):
            inf_count += 1
            continue

        total += fv
        if fv < lo:
            lo = fv
        if fv > hi:
            hi = fv

    finite_count = count - nan_count - inf_count
    mean         = (total / finite_count) if finite_count > 0 else None

    # If no finite values were seen, min/max are undefined.
    if finite_count == 0:
        lo = None
        hi = None

    return {
        "min"       : lo,
        "max"       : hi,
        "mean"      : mean,
        "count"     : count,
        "nan_count" : nan_count,
        "inf_count" : inf_count,
    }


# --- UI Helpers ---

def make_goto_event(ctx):
    """Create a goto_event function bound to the given HandlerContext.

    ctx -- HandlerContext with invoke_ui() access.
    """
    def goto_event(eid):
        """Navigate the RenderDoc UI to the specified event.

        eid -- Event ID to navigate to.
        """
        def _nav():
            pyrenderdoc = getattr(_builtins, "pyrenderdoc", None)
            if pyrenderdoc is None:
                raise RuntimeError("pyrenderdoc not available")
            pyrenderdoc.SetEventID([], eid, eid)

        ctx.invoke_ui(_nav)

    return goto_event


def make_view_texture(ctx):
    """Create a view_texture function bound to the given HandlerContext.

    ctx -- HandlerContext with invoke_ui() access.
    """
    def view_texture(resource_id):
        """Open the texture viewer for the given resource.

        resource_id -- ResourceId to display.
        """
        def _view():
            pyrenderdoc = getattr(_builtins, "pyrenderdoc", None)
            if pyrenderdoc is None:
                raise RuntimeError("pyrenderdoc not available")
            if hasattr(pyrenderdoc, "ViewTextureDisplay"):
                pyrenderdoc.ViewTextureDisplay(resource_id)
            elif hasattr(pyrenderdoc, "ShowTextureViewer"):
                pyrenderdoc.ShowTextureViewer()

        ctx.invoke_ui(_view)

    return view_texture


def make_highlight_drawcall(ctx):
    """Create a highlight_drawcall function bound to the given HandlerContext.

    ctx -- HandlerContext with invoke_ui() access.
    """
    def highlight_drawcall(eid):
        """Navigate the RenderDoc UI to highlight a draw call.

        Equivalent to goto_event. Navigates the event browser to the
        specified event ID so the draw call is selected and visible.

        eid -- Event ID of the draw call.
        """
        def _nav():
            pyrenderdoc = getattr(_builtins, "pyrenderdoc", None)
            if pyrenderdoc is None:
                raise RuntimeError("pyrenderdoc not available")
            pyrenderdoc.SetEventID([], eid, eid)

        ctx.invoke_ui(_nav)

    return highlight_drawcall


# --- Binding ---

def bind_utilities(ctx):
    """Create all utility functions bound to the given HandlerContext.

    Returns a dict suitable for merging into the eval handler's
    execution namespace. Functions that need the replay controller or
    UI thread are pre-bound to ctx via closures. Stateless helpers are
    included directly.

    ctx -- HandlerContext providing replay() and invoke_ui().
    """
    return {
        "inspect"           : inspect_obj,
        "diff_state"        : make_diff_state(ctx),
        "goto_event"        : make_goto_event(ctx),
        "view_texture"      : make_view_texture(ctx),
        "highlight_drawcall" : make_highlight_drawcall(ctx),
        "interpret_buffer"  : interpret_buffer,
        "summarize_data"    : summarize_data,
    }
