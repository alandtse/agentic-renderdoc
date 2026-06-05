"""Utility functions injected into the eval handler's namespace.

Provides runtime introspection, pipeline state diffing, data interpretation,
and UI navigation helpers. Functions that need access to the replay controller
or UI thread are bound to a HandlerContext via closures.
"""

import inspect as _inspect
import math
import struct
from typing import Any, Callable, Dict, List, Optional, Tuple
try:
    import renderdoc as rd
except ImportError:
    rd = None


# --- Introspection ---

# Attributes to suppress from SWIG-wrapped RenderDoc types.
_SWIG_INTERNAL = frozenset({"thisown", "this"})


def inspect_obj(obj: Any) -> dict:
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


def _inspect_enum(obj: Any) -> dict:
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


def _extract_int_enum_members(cls: type) -> List[dict]:
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


def _inspect_module(mod: Any) -> dict:
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


def _inspect_object(obj: Any) -> dict:
    """Inspect a class or instance, extracting methods and properties.

    Follows the __wrapped__ protocol for proxy objects so that the
    full API surface of the wrapped target is visible.
    """
    # Follow __wrapped__ to the real object if this is a proxy.
    # This handles _TrackedController and similar wrappers.
    wrapped = getattr(obj, "__wrapped__", None)
    if wrapped is not None:
        result = _inspect_object(wrapped)
        result["type"] = f"{type(obj).__name__} (wrapping {result['type']})"
        return result

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


def _get_signature(func: Any) -> Optional[str]:
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


def _first_line(doc: Optional[str]) -> Optional[str]:
    """Return the first non-empty line of a docstring, or None."""
    if not doc:
        return None
    for line in doc.strip().split("\n"):
        stripped = line.strip()
        if stripped:
            return stripped
    return None


# --- Pipeline State Diffing ---

def _deep_diff(a: Any, b: Any) -> Optional[dict]:
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


def _annotate_resource_names(diff: dict, name_map: Dict[str, str]) -> dict:
    """Walk a diff dict and annotate leaf values that are resource IDs.

    For each leaf {"before": x, "after": y}, if x or y is a string
    present in name_map, replaces it with {"id": x, "name": ...}.
    Recurses into nested dicts that aren't before/after leaves.

    Mutates diff in place and returns it.

    diff     -- Diff dict from _deep_diff.
    name_map -- Dict mapping serialized resource ID strings to names.
    """
    if not isinstance(diff, dict) or not name_map:
        return diff

    # Detect before/after leaf nodes.
    is_leaf = "before" in diff and "after" in diff and len(diff) == 2

    if is_leaf:
        for key in ("before", "after"):
            val = diff[key]
            if isinstance(val, str) and val in name_map:
                diff[key] = {"id": val, "name": name_map[val]}
    else:
        for val in diff.values():
            if isinstance(val, dict):
                _annotate_resource_names(val, name_map)

    return diff


def make_diff_state(ctx: Any) -> Callable[..., dict]:
    """Create a diff_state function bound to the given HandlerContext.

    The returned function captures ctx and manages the replay callback
    internally, so the caller just passes two event IDs.

    ctx -- HandlerContext with replay() access.
    """
    def diff_state(eid_a: int, eid_b: int) -> dict:
        """Diff pipeline state between two events.

        Moves the replay cursor to each event, snapshots the full
        pipeline state via serialize.pipeline_state(), and returns a
        recursive diff containing only the keys that changed.

        Safe to call both inside and outside ctx.replay() callbacks.

        eid_a -- First event ID.
        eid_b -- Second event ID.
        """
        from . import serialize

        def _snapshot_push_constants(controller: Any) -> Optional[str]:
            """Try to capture Vulkan push constant data.

            Returns the raw bytes as a hex string, or None for
            non-Vulkan captures or if the API is unavailable.
            """
            try:
                vk_state = controller.GetVulkanPipelineState()
                data     = vk_state.pushconsts
                if data:
                    return data.hex()
            except Exception:
                pass
            return None

        def _snapshot_both(controller: Any) -> Tuple[dict, dict, Dict[str, str]]:
            controller.SetFrameEvent(eid_a, True)
            state_a = serialize.pipeline_state(controller.GetPipelineState())
            push_a  = _snapshot_push_constants(controller)

            controller.SetFrameEvent(eid_b, True)
            state_b = serialize.pipeline_state(controller.GetPipelineState())
            push_b  = _snapshot_push_constants(controller)

            # Attach push constants alongside pipeline state so they
            # show up in the diff when they change between events.
            if push_a is not None:
                state_a["push_constants"] = push_a
            if push_b is not None:
                state_b["push_constants"] = push_b

            # Build a mapping from serialized resource ID strings to
            # human-readable names. Only includes resources that have
            # a non-empty name.
            name_map = {}
            for res in controller.GetResources():
                key = serialize.resource_id(res.resourceId)
                if res.name:
                    name_map[key] = res.name

            return (state_a, state_b, name_map)

        # If already on the replay thread, use the active controller.
        controller = ctx._replay_controller
        if controller is not None:
            state_a, state_b, name_map = _snapshot_both(controller)
        else:
            state_a, state_b, name_map = ctx.replay(_snapshot_both)

        diff = _deep_diff(state_a, state_b)
        if diff is None:
            return {}

        _annotate_resource_names(diff, name_map)
        return diff

    return diff_state


# --- Data Interpretation ---

# Integer format codes indexed by byte width. Used when the component
# type is an integer variant and the byte width isn't the default 4.
_UINT_BY_WIDTH = {1: "B", 2: "H", 4: "I", 8: "Q"}
_SINT_BY_WIDTH = {1: "b", 2: "h", 4: "i", 8: "q"}


def interpret_buffer(data: bytes, fmt: Any) -> list:
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


def summarize_data(values: Any) -> dict:
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


def make_summarize_texture(ctx: Any) -> Callable[..., dict]:
    """Create a summarize_texture function bound to the given HandlerContext.

    Equivalent of summarize_data for texture pixels. The first
    diagnostic when a render target looks "wrong" — is it all black?
    Blown out? Full of NaNs? — should be a one-line judgment, not
    save_texture + visual inspection.
    """
    def summarize_texture(resource_id : Any,
                          event_id    : Optional[int] = None,
                          mip         : int           = 0,
                          slice_index : int           = 0,
                          sample      : int           = 0,
                          channel     : Optional[int] = None,
                          controller  : Any           = None) -> dict:
        """Per-channel min/max/mean/NaN/Inf over a texture's pixels.

        Supports the same formats as Get-Texture's decoder (8-bit
        UNORM/SRGB, 16/32-bit Float). Block-compressed formats and
        depth/stencil produce a structured error.

        Safe to call both inside and outside a ctx.replay() callback.

        resource_id -- int, str (decimal), or rd.ResourceId.
        event_id    -- Event ID to SetFrameEvent before reading.
                       Required for render targets.
        mip         -- Mip level (default 0).
        slice_index -- Array slice or cube face (default 0).
        sample      -- Multisample index (default 0).
        channel     -- If set, summarize only that channel index
                       (0=R, 1=G, 2=B, 3=A). Default: all channels.
        controller  -- Optional ReplayController if already inside
                       ctx.replay(); auto-dispatched otherwise.

        Returns:
            {
              "resource"   : "ResourceId(…)",
              "format"     : "R16G16B16A16_FLOAT",
              "mip_width"  : 1920,
              "mip_height" : 1080,
              "channels"   : ["r", "g", "b", "a"],
              "stats"      : {
                  "r" : {min, max, mean, count, nan_count, inf_count},
                  "g" : { … },
                  …
              }
            }
        """
        import struct as _struct
        from . import serialize

        # Coerce resource_id to compare against TextureDescription.resourceId.
        if hasattr(resource_id, "__int__"):
            try:
                target_id = int(resource_id)
            except Exception:
                target_id = None
        elif isinstance(resource_id, str):
            try:
                target_id = int(resource_id)
            except ValueError:
                target_id = None
        else:
            target_id = None

        if target_id is None:
            return {"error": "resource_id must be an int, decimal str, or rd.ResourceId"}

        def _do(ctrl: Any) -> dict:
            if event_id is not None:
                ctrl.SetFrameEvent(int(event_id), True)

            tex = None
            for t in ctrl.GetTextures():
                if int(t.resourceId) == target_id:
                    tex = t
                    break
            if tex is None:
                return {"error": "no texture with resource id {}".format(target_id)}

            mip_w = max(1, tex.width  >> mip)
            mip_h = max(1, tex.height >> mip)

            try:
                raw = ctrl.GetTextureData(tex.resourceId,
                                          rd.Subresource(mip, slice_index, sample))
            except Exception as e:
                return {"error": "GetTextureData failed: {}".format(e)}
            if not raw:
                return {"error": "GetTextureData returned empty"}

            fmt = tex.format
            comp_type  = getattr(fmt, "compType", None)
            comp_count = getattr(fmt, "compCount", 0)
            comp_bytes = getattr(fmt, "compByteWidth", 0)
            try:
                fmt_name = fmt.Name()
            except Exception:
                fmt_name = str(fmt)

            # Resolve component-type name (SWIG enum-like).
            ct_name = comp_type.name if hasattr(comp_type, "name") else str(comp_type)

            channel_names = ["r", "g", "b", "a"][:comp_count] or ["r"]

            # Unpack into per-channel float lists.
            pixel_count = mip_w * mip_h

            if comp_bytes == 1 and ct_name in ("UNorm", "UNormSRGB"):
                # 8-bit UNORM: normalize 0..255 -> 0..1
                vals = list(raw[: pixel_count * comp_count])
                per_chan = [[vals[i + c] / 255.0 for i in range(0, len(vals), comp_count)]
                            for c in range(comp_count)]
            elif ct_name == "Float" and comp_bytes in (2, 4):
                code  = "e" if comp_bytes == 2 else "f"
                total = pixel_count * comp_count
                expected = total * comp_bytes
                if len(raw) < expected:
                    return {"error": "short read: got {} bytes, expected {}".format(
                        len(raw), expected)}
                floats = _struct.unpack("<{}{}".format(total, code), raw[:expected])
                per_chan = [list(floats[c::comp_count]) for c in range(comp_count)]
            else:
                return {
                    "error" : "unsupported format for summary: {} ({}, {}c, {}b)".format(
                        fmt_name, ct_name, comp_count, comp_bytes
                    ),
                    "hint"  : "block-compressed and depth/stencil aren't decoded; "
                              "use Get-Texture for visual inspection",
                }

            # Optional single-channel filter.
            if channel is not None:
                if not (0 <= channel < comp_count):
                    return {"error": "channel {} out of range for {} channels".format(
                        channel, comp_count)}
                per_chan = [per_chan[channel]]
                channel_names = [channel_names[channel]]

            stats = {name: summarize_data(vals)
                     for name, vals in zip(channel_names, per_chan)}

            return {
                "resource"   : serialize.resource_id(tex.resourceId),
                "format"     : fmt_name,
                "mip_width"  : mip_w,
                "mip_height" : mip_h,
                "channels"   : channel_names,
                "stats"      : stats,
            }

        if controller is not None:
            return _do(controller)
        active = ctx._replay_controller
        if active is not None:
            return _do(active)
        return ctx.replay(_do)

    return summarize_texture


# --- Action Flags ---

def action_flags(flags: Any) -> List[str]:
    """Decode an ActionDescription flags bitmask into human-readable names.

    Introspects rd.ActionFlags to discover all known flag members, then
    tests each bit against the provided value.

    flags -- Integer bitmask from ActionDescription.flags.

    Returns a list of flag name strings that are set in the value.
    """
    if rd is None:
        return []

    af = rd.ActionFlags

    # Build the member list by introspection. Prefer __members__ if the
    # SWIG wrapper exposes it, otherwise fall back to scanning class
    # attributes for int-valued constants.
    if hasattr(af, "__members__"):
        members = [(name, int(val)) for name, val in af.__members__.items()]
    else:
        members = []
        for name in dir(af):
            if name.startswith("_"):
                continue
            val = getattr(af, name, None)
            if isinstance(val, int):
                members.append((name, int(val)))

    flags = int(flags)
    return [name for name, bit in members if bit != 0 and (flags & bit) == bit]


# --- Push Constants ---

def decode_push_constants(controller: Any, stage: Any) -> dict:
    """Decode Vulkan push constant bytes against shader reflection.

    Reads the raw push constant data from the Vulkan pipeline state and
    attempts to decode it using the shader reflection for the given stage.
    Falls back to a hex dump if reflection is unavailable or the capture
    is not Vulkan.

    Must be called inside a ctx.replay() callback with a live controller.

    controller -- ReplayController (or TrackedController proxy).
    stage      -- rd.ShaderStage value (e.g., rd.ShaderStage.Vertex).

    Returns a dict with stage name, raw hex string, and decoded variables
    (if reflection was available).
    """
    from . import serialize

    stage_name = stage.name if hasattr(stage, "name") else str(stage)
    result     = {"stage": stage_name, "raw_hex": None, "decoded": None}

    # Read push constant bytes from the Vulkan-specific state.
    try:
        vk_state = controller.GetVulkanPipelineState()
        data     = vk_state.pushconsts
    except Exception:
        # Not a Vulkan capture or API unavailable.
        return result

    if not data:
        return result

    result["raw_hex"] = data.hex()

    # Attempt reflection-based decode.
    try:
        state = controller.GetPipelineState()
        refl  = state.GetShaderReflection(stage)
        if refl and refl.constantBlocks:
            result["decoded"] = serialize.cbuffer_variables(
                refl.constantBlocks[0].variables, data
            )
    except Exception:
        pass

    return result


def make_pixel_history(ctx: Any) -> Callable[..., dict]:
    """Create a pixel_history function bound to the given HandlerContext.

    "Which draws wrote to this pixel and how?" is the canonical first
    question when chasing a wrong-colour bug. RenderDoc's PixelHistory
    API answers it: every event that touched the pixel, with pre / post
    values, whether the fragment was culled / discarded / depth-failed,
    and the primitive ID.

    Wrapping it removes the ResourceId + Subresource + CompType
    boilerplate every cold-start agent writes from scratch.
    """
    def pixel_history(resource_id : Any,
                      x           : int,
                      y           : int,
                      mip         : int           = 0,
                      slice_index : int           = 0,
                      sample      : int           = 0,
                      event_id    : Optional[int] = None,
                      controller  : Any           = None) -> dict:
        """Trace every event that modified the pixel at (x, y).

        Coords are top-left convention regardless of API (RenderDoc
        normalises GL). Returns one entry per modifying event, with
        pre/shaderOut/post values and per-test culling flags.

        Safe to call both inside and outside a ctx.replay() callback.

        resource_id -- Texture to query (int, decimal str, or rd.ResourceId).
        x, y        -- Pixel coordinates (top-left origin).
        mip         -- Mip level (default 0).
        slice_index -- Array slice / cube face (default 0).
        sample      -- Multisample sample index (default 0).
        event_id    -- Optional event to SetFrameEvent before the query.
                       Most textures are content-independent of cursor
                       position, but render targets are not.
        controller  -- Optional ReplayController if already inside
                       ctx.replay(); auto-dispatched otherwise.

        Returns:
            {
              "resource"      : "ResourceId(…)",
              "x"             : 512,
              "y"             : 384,
              "events"        : [ { event_id, primitiveID, fragIndex,
                                    pre, shaderOut, post,
                                    failed_tests: [...] }, … ]
            }
        """
        if hasattr(resource_id, "__int__"):
            try:
                target_id = int(resource_id)
            except Exception:
                target_id = None
        elif isinstance(resource_id, str):
            try:
                target_id = int(resource_id)
            except ValueError:
                target_id = None
        else:
            target_id = None
        if target_id is None:
            return {"error": "resource_id must be an int, decimal str, or rd.ResourceId"}

        from . import serialize

        def _do(ctrl: Any) -> dict:
            if event_id is not None:
                ctrl.SetFrameEvent(int(event_id), True)

            tex = None
            for t in ctrl.GetTextures():
                if int(t.resourceId) == target_id:
                    tex = t
                    break
            if tex is None:
                return {"error": "no texture with resource id {}".format(target_id)}

            sub = rd.Subresource(mip, slice_index, sample)
            try:
                hist = ctrl.PixelHistory(tex.resourceId, int(x), int(y),
                                         sub, rd.CompType.Typeless)
            except Exception as e:
                return {"error": "PixelHistory failed: {}".format(e)}

            def _mod_to_dict(mv):
                # ModificationValue has col[4], depth, stencil.
                try:
                    col = [float(c) for c in mv.col]
                except Exception:
                    col = None
                return {
                    "col"     : col,
                    "depth"   : getattr(mv, "depth",   None),
                    "stencil" : getattr(mv, "stencil", None),
                }

            test_fields = (
                "sampleMasked", "backfaceCulled", "depthClipped",
                "depthBoundsFailed", "viewClipped", "scissorClipped",
                "shaderDiscarded", "depthTestFailed", "stencilTestFailed",
            )

            events = []
            for pm in hist:
                failed = [t for t in test_fields if getattr(pm, t, False)]
                events.append({
                    "event_id"          : int(pm.eventId),
                    "primitive_id"      : int(getattr(pm, "primitiveID", -1)),
                    "frag_index"        : int(getattr(pm, "fragIndex", -1)),
                    "direct_write"      : bool(getattr(pm, "directShaderWrite", False)),
                    "unbound_ps"        : bool(getattr(pm, "unboundPS", False)),
                    "pre"               : _mod_to_dict(pm.preMod),
                    "shader_out"        : _mod_to_dict(pm.shaderOut),
                    "post"              : _mod_to_dict(pm.postMod),
                    "failed_tests"      : failed,
                })

            return {
                "resource" : serialize.resource_id(tex.resourceId),
                "x"        : int(x),
                "y"        : int(y),
                "events"   : events,
            }

        if controller is not None:
            return _do(controller)
        active = ctx._replay_controller
        if active is not None:
            return _do(active)
        return ctx.replay(_do)

    return pixel_history


def make_debug_pixel(ctx: Any) -> Callable[..., dict]:
    """Create a debug_pixel function bound to the given HandlerContext.

    Runs RenderDoc's pixel-shader debugger at (x, y) for a specific
    draw, returns a summary of the resulting ShaderDebugTrace. Manages
    FreeTrace lifetime so callers can't leak.

    Full step-by-step debugging (ContinueDebug + per-state inspection)
    is stateful and rare enough that wrapping it would earn less than
    its weight. The summary surfaces what the agent usually needs:
    whether the trace exists at all, which inputs were sampled, the
    bindings the shader saw, and whether source-level mappings are
    available.
    """
    def debug_pixel(event_id   : int,
                    x          : int,
                    y          : int,
                    sample     : int = -1,   # -1 = NoPreference
                    primitive  : int = -1,
                    view       : int = -1,
                    controller : Any = None) -> dict:
        """Debug the pixel shader at (event_id, x, y).

        Reports a summary of the resulting ShaderDebugTrace. The trace
        itself is freed before returning — the dict is JSON-safe and
        no SWIG handles leak across the bridge.

        sample / primitive / view default to NoPreference (random
        fragment writing to the coord, any primitive, any view).

        Returns:
            {
              "event_id"    : 412,
              "x" / "y"     : 512 / 384,
              "had_trace"   : True,
              "num_inputs"  : 8,
              "num_steps"   : 124,   # if traceable, else None
              "has_source"  : True,
              "inputs"      : [ {name, value}, … ],
              "constant_blocks" : [ {name, resource}, … ],
              "readonly_resources" : [ {name, resource}, … ]
            }
        """
        from . import serialize

        def _do(ctrl: Any) -> dict:
            ctrl.SetFrameEvent(int(event_id), True)

            inputs = rd.DebugPixelInputs()
            try:
                inputs.sample    = int(sample)
                inputs.primitive = int(primitive)
                inputs.view      = int(view)
            except Exception:
                # Older RenderDoc builds had a slightly different shape.
                pass

            trace = None
            try:
                trace = ctrl.DebugPixel(int(x), int(y), inputs)
            except Exception as e:
                return {"error": "DebugPixel failed: {}".format(e)}

            if trace is None:
                return {
                    "event_id"  : int(event_id),
                    "x"         : int(x),
                    "y"         : int(y),
                    "had_trace" : False,
                    "note"      : "no pixel-shader fragment writes to this coord at this event",
                }

            try:
                num_inputs = len(getattr(trace, "inputs", []) or [])
                num_steps  = None
                has_source = bool(getattr(trace, "sourceVars", []) or [])

                input_summary = []
                try:
                    for v in trace.inputs:
                        input_summary.append({
                            "name"  : v.name,
                            "type"  : getattr(v.type, "name", str(v.type)),
                            "value" : _shader_var_summary(v),
                        })
                except Exception:
                    pass

                cbs = []
                try:
                    for cb in trace.constantBlocks:
                        cbs.append({
                            "name"     : cb.name,
                            "value"    : _shader_var_summary(cb),
                        })
                except Exception:
                    pass

                ros = []
                try:
                    for r in getattr(trace, "readOnlyResources", []) or []:
                        ros.append({
                            "name"     : getattr(r, "name", None),
                            "resource" : (serialize.resource_id(r.resourceResourceId)
                                          if hasattr(r, "resourceResourceId") else None),
                        })
                except Exception:
                    pass

                return {
                    "event_id"           : int(event_id),
                    "x"                  : int(x),
                    "y"                  : int(y),
                    "had_trace"          : True,
                    "num_inputs"         : num_inputs,
                    "num_steps"          : num_steps,
                    "has_source"         : has_source,
                    "inputs"             : input_summary,
                    "constant_blocks"    : cbs,
                    "readonly_resources" : ros,
                }
            finally:
                try:
                    ctrl.FreeTrace(trace)
                except Exception:
                    pass

        if controller is not None:
            return _do(controller)
        active = ctx._replay_controller
        if active is not None:
            return _do(active)
        return ctx.replay(_do)

    return debug_pixel


def _shader_var_summary(var: Any) -> Any:
    """Best-effort summary of a ShaderVariable's value.

    Returns either a flat list of floats (for simple scalars/vectors/
    matrices), a recursive list (for compound types), or None.
    """
    try:
        members = list(getattr(var, "members", []) or [])
        if members:
            return [_shader_var_summary(m) for m in members]
        # Try float column, falling back to int, then None.
        for attr in ("value", ):
            v = getattr(var, attr, None)
            if v is not None:
                # ShaderValue exposes f32v, u32v, s32v, etc.
                for vec in ("f32v", "u32v", "s32v"):
                    arr = getattr(v, vec, None)
                    if arr is not None:
                        return [float(x) for x in arr[:16]]
    except Exception:
        pass
    return None


_STAGE_NAMES = {
    "vs"       : "Vertex",
    "vertex"   : "Vertex",
    "hs"       : "Hull",
    "hull"     : "Hull",
    "ds"       : "Domain",
    "domain"   : "Domain",
    "gs"       : "Geometry",
    "geometry" : "Geometry",
    "ps"       : "Pixel",
    "pixel"    : "Pixel",
    "fragment" : "Pixel",
    "fs"       : "Pixel",
    "cs"       : "Compute",
    "compute"  : "Compute",
}


def _resolve_stage(stage: Any) -> Any:
    """Map a string alias to an rd.ShaderStage enum; pass enums through."""
    if isinstance(stage, str):
        canonical = _STAGE_NAMES.get(stage.lower())
        if canonical is None:
            raise ValueError(
                "unknown stage {!r}; expected one of {}".format(
                    stage, sorted(set(_STAGE_NAMES.values()))
                )
            )
        return getattr(rd.ShaderStage, canonical)
    return stage


def make_auto_decode_cb(ctx: Any) -> Callable[..., dict]:
    """Create an auto_decode_cb function bound to the given HandlerContext.

    Reading a constant buffer in agentic-renderdoc today is six lines
    of boilerplate (SetFrameEvent → GetConstantBlocks → byteOffset/Size
    → GetBufferData → GetShaderReflection → cbuffer_variables) every
    time. This utility wraps the chain.
    """
    def auto_decode_cb(stage          : Any,
                       slot           : int           = 0,
                       eventId        : Optional[int] = None,
                       controller     : Any           = None) -> dict:
        """Read + decode a constant buffer at a stage/slot in one call.

        Looks up the constant block at ``slot`` on the given stage,
        reads its bound buffer range (handling Vulkan's VK_WHOLE_SIZE
        sentinel correctly), and decodes the bytes against the shader
        reflection's variables list.

        Safe to call both inside and outside a ctx.replay() callback.

        stage      -- rd.ShaderStage enum or a string alias: "vs", "ps",
                      "cs", "vertex", "pixel", "fragment", "compute", …
        slot       -- Index into GetConstantBlocks(stage). Default 0.
        eventId    -- Event ID to SetFrameEvent to before reading. If
                      None, uses whatever event the cursor was last on.
        controller -- Optional ReplayController if already inside
                      ctx.replay(); auto-dispatched otherwise.

        Returns:
            {
              "stage"      : "Pixel",
              "slot"       : 0,
              "name"       : "PerFrame",      # cbuffer name if available
              "resource"   : "ResourceId(…)",
              "byte_offset": 0,
              "byte_size"  : 256,
              "decoded"    : [ {name, type, value}, … ]
            }
        On any failure, returns a dict with an "error" key.
        """
        from . import serialize

        try:
            stage_enum = _resolve_stage(stage)
        except ValueError as e:
            return {"error": str(e)}
        stage_name = stage_enum.name if hasattr(stage_enum, "name") else str(stage_enum)

        # u64::MAX — Vulkan's VK_WHOLE_SIZE encoded into byteSize.
        VK_WHOLE_SIZE = 0xFFFFFFFFFFFFFFFF

        def _do(ctrl: Any) -> dict:
            if eventId is not None:
                ctrl.SetFrameEvent(int(eventId), True)

            state = ctrl.GetPipelineState()

            try:
                blocks = state.GetConstantBlocks(stage_enum)
            except Exception as e:
                return {"error": "GetConstantBlocks failed: {}".format(e)}

            if slot < 0 or slot >= len(blocks):
                return {
                    "error" : "slot {} out of range; stage {} has {} constant block(s)".format(
                        slot, stage_name, len(blocks)
                    ),
                }

            ud   = blocks[slot]
            desc = ud.descriptor
            resource = desc.resource

            if int(resource) == 0:
                return {
                    "stage"   : stage_name,
                    "slot"    : slot,
                    "name"    : None,
                    "decoded" : None,
                    "note"    : "no buffer bound at this slot",
                }

            byte_offset = int(desc.byteOffset)
            byte_size   = int(desc.byteSize)

            # VK_WHOLE_SIZE means "to end of buffer" — look up the actual length.
            if byte_size == VK_WHOLE_SIZE:
                buf_len = None
                try:
                    for buf in ctrl.GetBuffers():
                        if int(buf.resourceId) == int(resource):
                            buf_len = int(buf.length)
                            break
                except Exception:
                    pass
                if buf_len is None:
                    return {
                        "error" : "buffer length unknown (VK_WHOLE_SIZE) and "
                                  "GetBuffers did not return the resource",
                    }
                byte_size = max(0, buf_len - byte_offset)

            data = ctrl.GetBufferData(resource, byte_offset, byte_size)
            if not data:
                return {
                    "stage"   : stage_name,
                    "slot"    : slot,
                    "decoded" : None,
                    "note"    : "GetBufferData returned empty",
                }

            cb_name = None
            decoded = None
            try:
                refl = state.GetShaderReflection(stage_enum)
                if refl and len(refl.constantBlocks) > slot:
                    cb     = refl.constantBlocks[slot]
                    cb_name = cb.name
                    decoded = serialize.cbuffer_variables(cb.variables, data)
            except Exception:
                pass

            return {
                "stage"       : stage_name,
                "slot"        : slot,
                "name"        : cb_name,
                "resource"    : serialize.resource_id(resource),
                "byte_offset" : byte_offset,
                "byte_size"   : byte_size,
                "decoded"     : decoded,
            }

        if controller is not None:
            return _do(controller)
        active = ctx._replay_controller
        if active is not None:
            return _do(active)
        return ctx.replay(_do)

    return auto_decode_cb


# --- Action Tree ---

def make_get_draw_calls(ctx: Any) -> Callable[..., List[dict]]:
    """Create a get_draw_calls function bound to the given HandlerContext.

    The returned closure walks the action tree and collects all leaf draw
    calls. This is the most commonly needed boilerplate when exploring a
    capture.

    ctx -- HandlerContext with replay() access and structured_file.
    """
    def get_draw_calls(controller: Any = None) -> List[dict]:
        """Collect all leaf draw calls in the frame.

        Recursively walks the action tree from GetRootActions(), filtering
        for actions with the Drawcall flag set. Returns a flat list of
        dicts with eventId and name.

        Safe to call both inside and outside ctx.replay() callbacks.

        controller -- Optional ReplayController. If None, dispatches via
                      ctx.replay() automatically.

        Returns a list of {"eventId": int, "name": str}.
        """
        def _collect(ctrl: Any) -> List[dict]:
            def _recurse(actions: list) -> List[dict]:
                draws = []
                for action in actions:
                    if action.flags & rd.ActionFlags.Drawcall:
                        draws.append({
                            "eventId" : action.eventId,
                            "name"    : action.GetName(ctx.structured_file),
                        })
                    draws.extend(_recurse(action.children))
                return draws
            return _recurse(ctrl.GetRootActions())

        # If a controller was passed explicitly, use it directly.
        if controller is not None:
            return _collect(controller)

        # Re-entrant check: if already on the replay thread, use the
        # active controller to avoid deadlocking.
        active = ctx._replay_controller
        if active is not None:
            return _collect(active)

        return ctx.replay(_collect)

    return get_draw_calls


def make_get_all_actions(ctx: Any) -> Callable[..., List[dict]]:
    """Create a get_all_actions function bound to the given HandlerContext.

    The returned closure walks the entire action tree and returns every
    node (markers, draws, dispatches, clears, copies, etc.) as a flat
    list. Useful for frame structure exploration and finding non-draw
    events like clears or copies.

    ctx -- HandlerContext with replay() access and structured_file.
    """
    def get_all_actions(controller: Any = None) -> List[dict]:
        """Collect all actions in the frame as a flat list.

        Recursively walks the action tree from GetRootActions(), emitting
        every node (not just draw calls). Each entry includes the event
        ID, name, and decoded flags.

        Safe to call both inside and outside ctx.replay() callbacks.

        controller -- Optional ReplayController. If None, dispatches via
                      ctx.replay() automatically.

        Returns a list of {"eventId": int, "name": str, "flags": [str]}.
        """
        def _collect(ctrl: Any) -> List[dict]:
            def _recurse(actions: list) -> List[dict]:
                result = []
                for a in actions:
                    result.append({
                        "eventId" : a.eventId,
                        "name"    : a.GetName(ctx.structured_file),
                        "flags"   : action_flags(a.flags),
                    })
                    result.extend(_recurse(a.children))
                return result
            return _recurse(ctrl.GetRootActions())

        # If a controller was passed explicitly, use it directly.
        if controller is not None:
            return _collect(controller)

        # Re-entrant check: if already on the replay thread, use the
        # active controller to avoid deadlocking.
        active = ctx._replay_controller
        if active is not None:
            return _collect(active)

        return ctx.replay(_collect)

    return get_all_actions


# --- Draw Call Summary ---

def _describe_one(ctrl: Any, structured_file: Any, eventId: int) -> dict:
    """Snapshot pipeline state and draw params at one event.

    Shared body used by both ``describe_draw`` and ``describe_draws``.
    Caller must already hold an active controller (inside a
    ctx.replay() callback). SetFrameEvent is the expensive op here;
    everything after it is cheap lookups.
    """
    from . import serialize

    ctrl.SetFrameEvent(eventId, True)
    state = ctrl.GetPipelineState()

    action = _find_action(ctrl.GetRootActions(), eventId)
    name   = action.GetName(structured_file) if action else None

    # Record query failures instead of swallowing them: an empty
    # render_targets/depth_target must mean "nothing bound", never "the
    # query raised and we hid it" — otherwise a wrong-output diagnosis (or
    # a find_first_divergence comparison) silently anchors on bad data.
    errors = {}

    stages = [
        ("vs", rd.ShaderStage.Vertex),
        ("hs", rd.ShaderStage.Hull),
        ("ds", rd.ShaderStage.Domain),
        ("gs", rd.ShaderStage.Geometry),
        ("ps", rd.ShaderStage.Pixel),
        ("cs", rd.ShaderStage.Compute),
    ]
    shaders = {}
    for label, stage in stages:
        shader = state.GetShader(stage)
        if int(shader) != 0:
            shaders[label] = serialize.resource_id(shader)

    render_targets = []
    try:
        for rt in state.GetOutputTargets():
            if int(rt.resource) != 0:
                render_targets.append(serialize.resource_id(rt.resource))
    except Exception as e:
        errors["render_targets"] = "{}: {}".format(type(e).__name__, e)

    depth_target = None
    try:
        depth = state.GetDepthTarget()
        if depth and int(depth.resource) != 0:
            depth_target = serialize.resource_id(depth.resource)
    except Exception as e:
        errors["depth_target"] = "{}: {}".format(type(e).__name__, e)

    draw_params = None
    if action and (action.flags & rd.ActionFlags.Drawcall):
        draw_params = {
            "numIndices"     : action.numIndices,
            "numInstances"   : action.numInstances,
            "indexOffset"    : action.indexOffset,
            "baseVertex"     : action.baseVertex,
            "instanceOffset" : action.instanceOffset,
        }

    vertex_buffers = []
    try:
        for vb in state.GetVBuffers():
            if int(vb.resourceId) != 0:
                vertex_buffers.append({
                    "resource" : serialize.resource_id(vb.resourceId),
                    "offset"   : vb.byteOffset,
                    "stride"   : vb.byteStride,
                })
    except Exception:
        pass

    index_buffer = None
    try:
        ib = state.GetIBuffer()
        if int(ib.resourceId) != 0:
            index_buffer = {
                "resource" : serialize.resource_id(ib.resourceId),
                "offset"   : ib.byteOffset,
                "stride"   : ib.byteStride,
            }
    except Exception:
        pass

    push_constants = None
    try:
        vk_state = ctrl.GetVulkanPipelineState()
        data     = vk_state.pushconsts
        if data:
            push_constants = data.hex()
    except Exception:
        pass

    result = {
        "event_id"       : eventId,
        "name"           : name,
        "shaders"        : shaders,
        "render_targets" : render_targets,
        "depth_target"   : depth_target,
        "draw_params"    : draw_params,
        "vertex_buffers" : vertex_buffers,
        "index_buffer"   : index_buffer,
        "push_constants" : push_constants,
    }
    if errors:
        result["errors"] = errors
    return result


def make_describe_draws(ctx: Any) -> Callable[..., List[dict]]:
    """Batched describe_draw — snapshots N events in ONE ctx.replay().

    Each SetFrameEvent forces a full GPU frame replay from event 0 to
    the target event (per Eval's PERFORMANCE AND STABILITY warning).
    Naively calling describe_draw(N times) issues N separate
    BlockInvoke trips; this utility collapses them into one outer
    callback so the replay-thread overhead is paid once, then the
    per-event SetFrameEvent+state-snapshot sequence runs back-to-back.

    Suggested upper bound: ~20 events per call. The serial cost of
    SetFrameEvent is still O(N), and any single hung replay will
    block the whole batch.
    """
    def describe_draws(event_ids: List[int],
                       controller: Any = None) -> List[dict]:
        """Summarize pipeline state at each event in event_ids.

        Returns a list of describe_draw results in the same order as
        event_ids. Events that can't be located in the action tree
        still get a SetFrameEvent + state snapshot but with name=None.

        event_ids  -- List of integer event IDs to snapshot.
        controller -- Optional ReplayController if already inside
                      ctx.replay(); auto-dispatched otherwise.
        """
        if not event_ids:
            return []

        def _batch(ctrl: Any) -> List[dict]:
            return [_describe_one(ctrl, ctx.structured_file, int(eid))
                    for eid in event_ids]

        if controller is not None:
            return _batch(controller)
        active = ctx._replay_controller
        if active is not None:
            return _batch(active)
        return ctx.replay(_batch)

    return describe_draws


def make_describe_draw(ctx: Any) -> Callable[..., dict]:
    """Create a describe_draw function bound to the given HandlerContext.

    The returned closure provides a one-shot comprehensive summary of a
    single draw call, gathering pipeline state, bound resources, and draw
    parameters into one dict.

    ctx -- HandlerContext with replay() access and structured_file.
    """
    def describe_draw(controller: Any = None, eventId: Optional[int] = None) -> dict:
        """Summarize pipeline state and draw parameters at an event.

        Moves the replay cursor to the given event, snapshots the full
        pipeline state, and assembles a comprehensive summary including
        bound shaders, render targets, vertex/index buffers, draw
        parameters, and push constants (Vulkan).

        Safe to call both inside and outside ctx.replay() callbacks.

        controller -- Optional ReplayController. If None, dispatches via
                      ctx.replay() automatically.
        eventId    -- Event ID to inspect. Required.

        Returns a dict with event_id, name, shaders, render_targets,
        depth_target, draw_params, vertex_buffers, index_buffer, and
        push_constants.
        """
        from . import serialize

        if eventId is None:
            return {"error": "eventId is required"}

        def _describe(ctrl: Any) -> dict:
            return _describe_one(ctrl, ctx.structured_file, eventId)

        # If a controller was passed explicitly, use it directly.
        if controller is not None:
            return _describe(controller)

        # Re-entrant check: if already on the replay thread, use the
        # active controller to avoid deadlocking.
        active = ctx._replay_controller
        if active is not None:
            return _describe(active)

        return ctx.replay(_describe)

    return describe_draw


def make_find_marker(ctx: Any) -> Callable[..., List[dict]]:
    """Create a find_marker function bound to the given HandlerContext.

    Skyrim and most engines emit a deep PushMarker tree
    (RenderImageSpaceEffect → BloomBlur → …). Walking it manually every
    session to locate "the shadow pass" is repetitive boilerplate. This
    utility wraps the recursive walk with name matching plus an optional
    parent scope.
    """
    def find_marker(name             : str,
                    regex            : bool          = False,
                    case_sensitive   : bool          = False,
                    markers_only     : bool          = False,
                    parent           : Optional[int] = None,
                    controller       : Any           = None) -> List[dict]:
        """Search the action tree for actions whose name matches.

        Matches against both ``customName`` (the PushMarker label) and
        ``GetName()`` (the formatted display name, e.g.
        ``vkCmdDrawIndexed(36, 1, 0, 0, 0)`` or
        ``RenderImageSpaceEffect``), so the same call finds either a
        marker scope or a draw call with the name in its display.

        Safe to call both inside and outside a ctx.replay() callback.

        name           -- Substring (default) or regex pattern to match.
        regex          -- If True, treat ``name`` as a regular expression.
        case_sensitive -- Default False (case-insensitive substring or
                          re.IGNORECASE).
        markers_only   -- If True, restrict matches to PushMarker /
                          SetMarker actions (skip leaf draws/dispatches).
        parent         -- Event ID of a marker whose subtree to limit
                          the search to. None = search the whole frame.
        controller     -- Optional ReplayController if already inside
                          ctx.replay(); auto-dispatched otherwise.

        Returns a list of dicts:
            eventId    -- Action event ID (use with SetFrameEvent or goto_event)
            name       -- GetName() formatted display name
            path       -- "/"-joined ancestor marker scopes
            is_marker  -- True if the matched action is a PushMarker/SetMarker
            customName -- Raw customName field (may be empty for non-markers)
        """
        import re as _re

        marker_flags = (rd.ActionFlags.PushMarker | rd.ActionFlags.SetMarker)

        pattern = None  # type: Optional[Any]
        needle  = ""
        if regex:
            pattern = _re.compile(name, 0 if case_sensitive else _re.IGNORECASE)
        else:
            needle = name if case_sensitive else name.lower()

        def _matches(text: str) -> bool:
            if not text:
                return False
            if pattern is not None:
                return pattern.search(text) is not None
            hay = text if case_sensitive else text.lower()
            return needle in hay

        def _collect(ctrl: Any) -> List[dict]:
            results = []   # type: List[dict]
            sf = ctx.structured_file

            def _recurse(actions: list, path_segments: List[str]) -> None:
                for action in actions:
                    is_marker = bool(action.flags & marker_flags)
                    custom    = getattr(action, "customName", "") or ""
                    display   = action.GetName(sf)

                    # Extend the path on entering a named marker scope.
                    if is_marker and custom:
                        new_path = path_segments + [custom]
                    else:
                        new_path = path_segments

                    skip = markers_only and not is_marker
                    if not skip and (_matches(custom) or _matches(display)):
                        results.append({
                            "eventId"    : action.eventId,
                            "name"       : display,
                            "path"       : "/".join(new_path),
                            "is_marker"  : is_marker,
                            "customName" : custom,
                        })

                    _recurse(action.children, new_path)

            if parent is not None:
                root_actions  = ctrl.GetRootActions()
                parent_action = _find_action(root_actions, parent)
                if parent_action is None:
                    return []
                seed = [parent_action.customName] if getattr(
                    parent_action, "customName", "") else []
                _recurse(parent_action.children, seed)
            else:
                _recurse(ctrl.GetRootActions(), [])

            return results

        if controller is not None:
            return _collect(controller)
        active = ctx._replay_controller
        if active is not None:
            return _collect(active)
        return ctx.replay(_collect)

    return find_marker


def _find_action(actions: list, eventId: int) -> Any:
    """Recursively search the action tree for an action by event ID.

    actions -- List of ActionDescription from GetRootActions() or .children.
    eventId -- Target event ID.

    Returns the matching ActionDescription, or None if not found.
    """
    for action in actions:
        if action.eventId == eventId:
            return action
        found = _find_action(action.children, eventId)
        if found is not None:
            return found
    return None


# --- Resource Lookup ---

def make_get_resource_name(ctx: Any) -> Callable[..., str]:
    """Create a get_resource_name function bound to the given HandlerContext.

    The returned closure looks up the human-readable name of a resource
    by its ResourceId. Results are cached because the resource list is
    fixed within a single capture.

    ctx -- HandlerContext with replay() access.
    """
    cache: Dict[Any, str] = {}

    def _build_cache(controller: Any) -> None:
        """Fetch all resources and populate the name cache."""
        for res in controller.GetResources():
            cache[res.resourceId] = res.name

    def get_resource_name(resource_id: Any) -> str:
        """Return the human-readable name of a RenderDoc resource.

        Safe to call both inside and outside ctx.replay() callbacks.
        If already on the replay thread (inside a callback), uses the
        active controller directly. Otherwise dispatches via ctx.replay().

        resource_id -- ResourceId to look up.
        """
        if not cache:
            # If we're already inside a BlockInvoke callback, use the
            # active controller directly to avoid deadlocking.
            controller = ctx._replay_controller
            if controller is not None:
                _build_cache(controller)
            else:
                ctx.replay(_build_cache)

        return cache.get(resource_id, f"<unknown {resource_id}>")

    return get_resource_name


# --- UI Helpers ---

def _headless_no_ui() -> dict:
    """Build a fresh structured error for UI calls in a headless worker.

    Returned by reference would be unsafe — callers may mutate the
    response dict before the bridge serializes it.
    """
    return {
        "ok"    : False,
        "error" : "headless: UI navigation is unavailable in this worker",
    }


def make_goto_event(ctx: Any) -> Callable[..., dict]:
    """Create a goto_event function bound to the given HandlerContext.

    ctx -- HandlerContext with invoke_ui() access.
    """
    def goto_event(eid: int) -> dict:
        """Navigate the RenderDoc UI to the specified event.

        eid -- Event ID to navigate to.
        Returns a dict confirming the navigation. In headless workers,
        returns a structured "no UI" error instead of raising.
        """
        if getattr(ctx, "headless", False):
            return _headless_no_ui()

        def _nav() -> None:
            ctx.ctx.SetEventID([], eid, eid)

        ctx.invoke_ui(_nav)
        return {"navigated_to": eid}

    return goto_event


def make_view_texture(ctx: Any) -> Callable[..., dict]:
    """Create a view_texture function bound to the given HandlerContext.

    ctx -- HandlerContext with invoke_ui() access.
    """
    def view_texture(resource_id: Any) -> dict:
        """Open the texture viewer for the given resource.

        resource_id -- ResourceId to display.
        """
        if getattr(ctx, "headless", False):
            return _headless_no_ui()

        def _view() -> None:
            pyrenderdoc = ctx.ctx
            if hasattr(pyrenderdoc, "ViewTextureDisplay"):
                pyrenderdoc.ViewTextureDisplay(resource_id)
            elif hasattr(pyrenderdoc, "ShowTextureViewer"):
                pyrenderdoc.ShowTextureViewer()

        ctx.invoke_ui(_view)
        return {"viewing_texture": True}

    return view_texture


def make_save_texture(ctx):
    """Create a save_texture function bound to the given HandlerContext.

    ctx -- HandlerContext with replay() access.
    """
    def save_texture(resource_id, path, mip=0, slice_index=0, event_id=None):
        """Save a texture or render target to a PNG file on disk.

        Runs on the replay thread via ctx.replay(). If event_id is given the
        replay cursor is moved to that event first so pipeline-bound resources
        reflect the correct state.

        Returns the absolute path that was written so the caller (or an MCP
        client with file-read access) can open the image directly.

        resource_id  -- rd.ResourceId of the texture to save.
        path         -- Destination file path (must end in .png).
        mip          -- Mip level to export (default 0).
        slice_index  -- Array slice / cube face to export (default 0).
        event_id     -- If set, seek to this event before saving.
        """
        import os

        def _save(controller):
            if event_id is not None:
                controller.SetFrameEvent(event_id, True)

            save_data = rd.TextureSave()
            save_data.resourceId = resource_id
            save_data.destType = rd.FileType.PNG
            save_data.mip = mip
            save_data.slice.sliceIndex = slice_index
            # Export all components; caller can channel-extract via channelExtract
            # if they need a single-channel greyscale view.
            save_data.channelExtract = -1

            result = controller.SaveTexture(save_data, path)
            return {"ok": result.OK(), "path": os.path.abspath(path)}

        return ctx.replay(_save)

    return save_texture


def make_highlight_drawcall(ctx: Any) -> Callable[..., dict]:
    """Create a highlight_drawcall function bound to the given HandlerContext.

    ctx -- HandlerContext with invoke_ui() access.
    """
    def highlight_drawcall(eid: int) -> dict:
        """Navigate the RenderDoc UI to highlight a draw call.

        Equivalent to goto_event. Navigates the event browser to the
        specified event ID so the draw call is selected and visible.

        eid -- Event ID of the draw call.
        """
        if getattr(ctx, "headless", False):
            return _headless_no_ui()

        def _nav() -> None:
            ctx.ctx.SetEventID([], eid, eid)

        ctx.invoke_ui(_nav)
        return {"highlighted": eid}

    return highlight_drawcall


# --- Convenience accessors (int-friendly, plain-dict returns) ---

def _run_replay(ctx: Any, fn: Callable[[Any], Any], controller: Any) -> Any:
    """Run fn(controller) on the replay thread, reusing an active one.

    Mirrors the dispatch idiom in get_resource_name / describe_draws so
    these helpers work both inside and outside a ctx.replay() callback.
    """
    if controller is not None:
        return fn(controller)
    active = ctx._replay_controller
    if active is not None:
        return fn(active)
    return ctx.replay(fn)


def _format_name(fmt: Any) -> Optional[str]:
    """ResourceFormat name via .Name() (a method, not a .name property)."""
    try:
        return fmt.Name()
    except Exception:
        return None


def _coerce_rid_int(ident: Any) -> Optional[int]:
    """Extract the integer handle from an int or a string id.

    Accepts a plain int, an all-digit string ("123"), or a
    "ResourceId(123)" string. Returns None otherwise — deliberately
    strict so a stray-digit string (e.g. a format name like
    "R16G16B16A16_FLOAT") is rejected instead of silently resolving to
    an unrelated resource via the embedded "16".
    """
    if isinstance(ident, bool):
        return None
    if isinstance(ident, int):
        return ident
    if isinstance(ident, str):
        import re as _re
        s = ident.strip()
        if s.isdigit():
            return int(s)
        m = _re.fullmatch(r"ResourceId\((\d+)\)", s)
        if m:
            return int(m.group(1))
    return None


def _resolve_resource_id(ctrl: Any, ident: Any) -> Any:
    """Resolve an int / string / ResourceId into a live ResourceId.

    GetUsage and friends need the opaque ResourceId object, not an int.
    Scans GetResources() for a matching integer handle. Returns None if
    unresolvable.
    """
    if rd is not None and isinstance(ident, rd.ResourceId):
        return ident
    target = _coerce_rid_int(ident)
    if target is None:
        return None
    for res in ctrl.GetResources():
        if int(res.resourceId) == target:
            return res.resourceId
    return None


def make_get_outputs(ctx: Any) -> Callable[..., dict]:
    """Create a get_outputs accessor bound to the given HandlerContext.

    Returns the bound color render targets and depth target at an event
    as plain dicts — the common "what is this draw writing to?" lookup
    without hand-rolling GetOutputTargets()/GetDepthTarget().
    """
    def get_outputs(eventId: Optional[int] = None,
                    controller: Any = None) -> dict:
        """Color + depth output targets at an event.

        eventId    -- Seek the replay cursor here first (omit to use the
                      current cursor). Required for an accurate answer at
                      a specific draw.
        controller -- Optional ReplayController if already inside replay.
        """
        from . import serialize

        def _work(ctrl: Any) -> dict:
            if eventId is not None:
                ctrl.SetFrameEvent(int(eventId), True)
            state = ctrl.GetPipelineState()
            errors = {}
            color = []
            try:
                for rt in state.GetOutputTargets():
                    if int(rt.resource) != 0:
                        color.append({
                            "resource"   : serialize.resource_id(rt.resource),
                            "format"     : _format_name(rt.format),
                            "firstMip"   : int(getattr(rt, "firstMip", 0)),
                            "firstSlice" : int(getattr(rt, "firstSlice", 0)),
                        })
            except Exception as e:
                # Record, don't hide: empty color must mean "none bound".
                errors["color"] = "{}: {}".format(type(e).__name__, e)
            depth = None
            try:
                d = state.GetDepthTarget()
                if d and int(d.resource) != 0:
                    depth = {
                        "resource" : serialize.resource_id(d.resource),
                        "format"   : _format_name(d.format),
                    }
            except Exception as e:
                errors["depth"] = "{}: {}".format(type(e).__name__, e)
            out = {"event_id": eventId, "color": color, "depth": depth}
            if errors:
                out["errors"] = errors
            return out

        return _run_replay(ctx, _work, controller)

    return get_outputs


def make_get_viewport(ctx: Any) -> Callable[..., dict]:
    """Create a get_viewport accessor bound to the given HandlerContext."""
    def get_viewport(eventId: Optional[int] = None, index: int = 0,
                     controller: Any = None) -> dict:
        """Viewport rectangle at an event as a plain dict.

        eventId    -- Seek the replay cursor here first (omit for current).
        index      -- Viewport index (default 0).
        controller -- Optional ReplayController if already inside replay.
        """
        def _work(ctrl: Any) -> dict:
            if eventId is not None:
                ctrl.SetFrameEvent(int(eventId), True)
            state = ctrl.GetPipelineState()
            vp = state.GetViewport(int(index))
            return {
                "index"    : int(index),
                "x"        : vp.x,
                "y"        : vp.y,
                "width"    : vp.width,
                "height"   : vp.height,
                "minDepth" : vp.minDepth,
                "maxDepth" : vp.maxDepth,
            }

        return _run_replay(ctx, _work, controller)

    return get_viewport


def _coerce_state_value(val: Any, depth: int) -> Any:
    """Convert a pipeline-state attribute value to a JSON-friendly form.

    Enums (RenderDoc enums subclass int but carry a str ``.name``) become
    their name; primitives pass through; small nested structs recurse one
    level; everything else falls back to str(). Defensive — never raises.
    """
    # Enum first: RenderDoc enums are int subclasses, so check .name before
    # the int branch or we'd lose the readable name.
    nm = getattr(val, "name", None)
    if isinstance(nm, str) and not isinstance(val, str):
        return nm
    if val is None or isinstance(val, (bool, int, float, str)):
        return val
    if isinstance(val, (list, tuple)):
        return [_coerce_state_value(v, depth - 1) for v in val][:32]
    if depth > 0:
        try:
            return _dump_state_struct(val, depth - 1)
        except Exception:
            return str(val)
    return str(val)


def _dump_state_struct(obj: Any, depth: int = 1) -> dict:
    """Shallow-dump a SWIG state struct's data attributes to a plain dict.

    Reflects whatever fields the object actually exposes rather than
    hardcoding API-specific names (which differ across D3D11/12/Vulkan/GL
    and are a known round-trip trap). Methods and SWIG internals are
    skipped; values go through _coerce_state_value.
    """
    out = {}
    for name in dir(obj):
        if name.startswith("_") or name in _SWIG_INTERNAL:
            continue
        try:
            val = getattr(obj, name)
        except Exception:
            continue
        if callable(val):
            continue
        out[name] = _coerce_state_value(val, depth)
    return out


def make_depth_stencil(ctx: Any) -> Callable[..., dict]:
    """Create a depth_stencil accessor bound to the given HandlerContext.

    Depth/stencil TEST state (enable, write, compare func, stencil ops) is
    NOT on the API-agnostic PipeState — it lives on the API-specific
    object (D3D11/12 outputMerger.depthStencilState, Vulkan depthStencil).
    This helper finds the right one and dumps it as a plain dict.
    """
    # (api, accessor name, function locating the DS struct on that state)
    _ATTEMPTS = [
        ("D3D11",  "GetD3D11PipelineState",
         lambda s: getattr(getattr(s, "outputMerger", None), "depthStencilState", None)),
        ("D3D12",  "GetD3D12PipelineState",
         lambda s: getattr(getattr(s, "outputMerger", None), "depthStencilState", None)),
        ("Vulkan", "GetVulkanPipelineState",
         lambda s: getattr(s, "depthStencil", None)),
    ]

    def depth_stencil(eventId: Optional[int] = None,
                      controller: Any = None) -> dict:
        """Depth/stencil test state at an event as a plain dict.

        eventId    -- Seek the replay cursor here first (omit for current).
        controller -- Optional ReplayController if already inside replay.

        Returns {event_id, api, depth_stencil: {...}} or a structured
        error if no API-specific pipeline state could be located.
        """
        def _work(ctrl: Any) -> dict:
            if eventId is not None:
                ctrl.SetFrameEvent(int(eventId), True)

            # Each GetXxxPipelineState() returns None unless the capture is
            # that API, so the first non-None hit identifies the API.
            for api, getter, ds_path in _ATTEMPTS:
                fn = getattr(ctrl, getter, None)
                if fn is None:
                    continue
                try:
                    state = fn()
                except Exception:
                    state = None
                if state is None:
                    continue
                ds_obj = ds_path(state)
                if ds_obj is None:
                    return {"event_id": eventId, "api": api,
                            "depth_stencil": None,
                            "note": "no depth-stencil state object on this pipeline"}
                return {"event_id": eventId, "api": api,
                        "depth_stencil": _dump_state_struct(ds_obj)}

            # OpenGL (and anything else) — dump whatever depth/stencil
            # sub-state the GL pipeline exposes, defensively.
            gl = getattr(ctrl, "GetGLPipelineState", None)
            if gl is not None:
                try:
                    s = gl()
                except Exception:
                    s = None
                if s is not None:
                    ds = {}
                    for fld in ("depthState", "stencilState"):
                        sub = getattr(s, fld, None)
                        if sub is not None:
                            ds[fld] = _dump_state_struct(sub)
                    return {"event_id": eventId, "api": "OpenGL",
                            "depth_stencil": ds or None}

            return {
                "ok"    : False,
                "error" : "could not locate an API-specific pipeline state; "
                          "use inspect(controller.GetPipelineState()) to explore",
            }

        return _run_replay(ctx, _work, controller)

    return depth_stencil


def make_usage(ctx: Any) -> Callable[..., dict]:
    """Create a usage accessor bound to the given HandlerContext.

    Wraps ReplayController.GetUsage, which requires a ResourceId object
    (not an int) — a documented round-trip trap. This accepts an int,
    a string id, or a ResourceId and returns plain dicts.
    """
    def usage(resource: Any, controller: Any = None) -> dict:
        """Every event that used a resource, with the usage kind.

        resource   -- ResourceId, int handle, or string id (as returned
                      by describe_draw / serialize.resource_id).
        controller -- Optional ReplayController if already inside replay.

        Returns {"resource": str, "usage": [{"eventId", "usage"}, ...]}
        or a structured error if the id can't be resolved.
        """
        from . import serialize

        def _work(ctrl: Any) -> dict:
            rid = _resolve_resource_id(ctrl, resource)
            if rid is None:
                return {
                    "ok"    : False,
                    "error" : "could not resolve {!r} to a ResourceId; "
                              "pass an id from describe_draw / "
                              "GetResources()".format(resource),
                }
            out = []
            for eu in ctrl.GetUsage(rid):
                out.append({
                    "eventId" : int(eu.eventId),
                    "usage"   : getattr(eu.usage, "name", str(eu.usage)),
                })
            return {"resource": serialize.resource_id(rid), "usage": out}

        return _run_replay(ctx, _work, controller)

    return usage


# --- Binding ---

def bind_utilities(ctx: Any) -> Dict[str, Any]:
    """Create all utility functions bound to the given HandlerContext.

    Returns a dict suitable for merging into the eval handler's
    execution namespace. Functions that need the replay controller or
    UI thread are pre-bound to ctx via closures. Stateless helpers are
    included directly.

    ctx -- HandlerContext providing replay() and invoke_ui().
    """
    return {
        "inspect"              : inspect_obj,
        "diff_state"           : make_diff_state(ctx),
        "get_resource_name"    : make_get_resource_name(ctx),
        "get_draw_calls"       : make_get_draw_calls(ctx),
        "get_all_actions"      : make_get_all_actions(ctx),
        "find_marker"          : make_find_marker(ctx),
        "describe_draw"        : make_describe_draw(ctx),
        "describe_draws"       : make_describe_draws(ctx),
        "get_outputs"          : make_get_outputs(ctx),
        "get_viewport"         : make_get_viewport(ctx),
        "depth_stencil"        : make_depth_stencil(ctx),
        "usage"                : make_usage(ctx),
        "goto_event"           : make_goto_event(ctx),
        "view_texture"         : make_view_texture(ctx),
        "save_texture"         : make_save_texture(ctx),
        "highlight_drawcall"   : make_highlight_drawcall(ctx),
        "auto_decode_cb"       : make_auto_decode_cb(ctx),
        "interpret_buffer"     : interpret_buffer,
        "summarize_data"       : summarize_data,
        "summarize_texture"    : make_summarize_texture(ctx),
        "pixel_history"        : make_pixel_history(ctx),
        "debug_pixel"          : make_debug_pixel(ctx),
        "action_flags"         : action_flags,
        "decode_push_constants" : decode_push_constants,
    }
