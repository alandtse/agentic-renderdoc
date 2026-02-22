"""API index builder for the RenderDoc Python module.

Introspects the live `renderdoc` module at startup to build a searchable
index of all classes, methods, enums, and their docstrings.
"""


def build_index():
    """Build the API reference index by introspecting the renderdoc module.

    Returns a list of entries, each with:
    - name:      Fully qualified name (e.g., "ReplayController.SetFrameEvent")
    - kind:      "class", "method", "property", "enum", "enum_value"
    - doc:       Full docstring (RST-formatted)
    - signature: Method signature string, if applicable
    """
    try:
        import renderdoc as rd
    except ImportError:
        return []

    entries = []
    _walk_module(rd, "renderdoc", entries, visited=set())
    return entries


def search_index(index, query):
    """Search the index for entries matching the query string.

    Matches against name and docstring content. Case-insensitive.
    """
    query_lower = query.lower()
    results     = []

    for entry in index:
        if query_lower in entry["name"].lower():
            results.append(entry)
        elif entry["doc"] and query_lower in entry["doc"].lower():
            results.append(entry)

    return results


def _walk_module(obj, prefix, entries, visited):
    """Recursively enumerate members of a module or class."""
    obj_id = id(obj)
    if obj_id in visited:
        return
    visited.add(obj_id)

    for name in sorted(dir(obj)):
        if name.startswith("_"):
            continue

        try:
            member = getattr(obj, name)
        except Exception:
            continue

        qualified = f"{prefix}.{name}"
        doc       = getattr(member, "__doc__", None) or ""

        kind = _classify(member)
        if kind is not None:
            entries.append({
                "name":      qualified,
                "kind":      kind,
                "doc":       doc,
                "signature": _get_signature(member),
            })

        # Recurse into classes and enum types.
        if kind in ("class", "enum"):
            _walk_module(member, qualified, entries, visited)


def _classify(obj):
    """Classify a Python object as class, method, property, enum, or None."""
    # TODO: Distinguish RenderDoc enums (IntEnum-like SWIG types) from classes.
    if isinstance(obj, type):
        return "class"
    elif callable(obj):
        return "method"
    elif isinstance(obj, property):
        return "property"
    else:
        return None


def _get_signature(obj):
    """Try to extract a signature string from a callable."""
    import inspect

    if not callable(obj):
        return None

    try:
        return str(inspect.signature(obj))
    except (ValueError, TypeError):
        return None
