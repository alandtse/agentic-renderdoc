"""API index builder for the RenderDoc Python module.

Introspects the live `renderdoc` module at startup to build a searchable
index of all classes, methods, enums, and their docstrings.
"""

import inspect
import re


# --- Public API ---

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
    Results are ranked by relevance:
    - Exact unqualified name match scores highest.
    - Unqualified name prefix match next.
    - Substring match on qualified name next.
    - Doc-body-only matches score lowest.
    """
    query_lower = query.lower()
    scored      = []

    for entry in index:
        score = _score_entry(entry, query_lower)
        if score > 0:
            scored.append((score, entry))

    # Sort by score descending, then by name for stability.
    scored.sort(key=lambda pair: (-pair[0], pair[1]["name"]))
    return [entry for _, entry in scored]


# --- Scoring ---

def _score_entry(entry, query_lower):
    """Score a single index entry against a lowercased query string.

    Returns an integer score. Zero means no match.

    Scoring tiers:
    - 100: exact match on unqualified name
    -  80: unqualified name starts with query
    -  60: qualified name contains query
    -  20: doc body contains query
    """
    name_lower  = entry["name"].lower()
    unqualified = name_lower.rsplit(".", 1)[-1]

    if unqualified == query_lower:
        return 100
    if unqualified.startswith(query_lower):
        return 80
    if query_lower in name_lower:
        return 60
    if entry["doc"] and query_lower in entry["doc"].lower():
        return 20

    return 0


# --- Module Walker ---

def _walk_module(obj, prefix, entries, visited):
    """Recursively enumerate members of a module or class.

    Walks public attributes of obj, classifying each as a class, enum,
    method, or property and appending index entries. Recurses into classes
    and enums.
    """
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

        if kind == "enum":
            # Record the enum type itself.
            entries.append({
                "name":      qualified,
                "kind":      "enum",
                "doc":       doc,
                "signature": None,
            })
            # Extract individual enum values from __members__.
            _walk_enum_members(member, qualified, entries)
            continue

        if kind is not None:
            entries.append({
                "name":      qualified,
                "kind":      kind,
                "doc":       doc,
                "signature": _get_signature(member) if kind == "method" else None,
            })

        # Recurse into class types to pick up methods and properties.
        if kind == "class":
            _walk_class(member, qualified, entries, visited)


def _walk_class(cls, prefix, entries, visited):
    """Walk the members of a class, emitting methods and properties.

    Unlike _walk_module, this checks for property descriptors using
    inspect.getattr_static to avoid invoking descriptors.
    """
    cls_id = id(cls)
    if cls_id in visited:
        return
    visited.add(cls_id)

    for name in sorted(dir(cls)):
        if name.startswith("_"):
            continue

        # Use getattr_static to inspect descriptors without triggering them.
        try:
            static_attr = inspect.getattr_static(cls, name)
        except Exception:
            continue

        qualified = f"{prefix}.{name}"

        # Check for property descriptors (both builtin property and
        # SWIG-style descriptors that have fget or __doc__).
        if _is_property_descriptor(static_attr):
            doc = getattr(static_attr, "__doc__", None) or ""
            entries.append({
                "name":      qualified,
                "kind":      "property",
                "doc":       doc,
                "signature": None,
            })
            continue

        # Fall back to regular attribute access for non-descriptors.
        try:
            member = getattr(cls, name)
        except Exception:
            continue

        doc  = getattr(member, "__doc__", None) or ""
        kind = _classify(member)

        if kind == "enum":
            entries.append({
                "name":      qualified,
                "kind":      "enum",
                "doc":       doc,
                "signature": None,
            })
            _walk_enum_members(member, qualified, entries)
        elif kind == "class":
            entries.append({
                "name":      qualified,
                "kind":      "class",
                "doc":       doc,
                "signature": None,
            })
            _walk_class(member, qualified, entries, visited)
        elif kind == "method":
            entries.append({
                "name":      qualified,
                "kind":      "method",
                "doc":       doc,
                "signature": _get_signature(member),
            })


def _walk_enum_members(enum_cls, prefix, entries):
    """Extract individual values from a SWIG-generated enum type.

    SWIG enums expose their members via a __members__ dict mapping
    name strings to enum values. Each member becomes an "enum_value"
    entry with the integer value included in the doc field.
    """
    members = getattr(enum_cls, "__members__", None)
    if not isinstance(members, dict):
        return

    for member_name, member_value in sorted(members.items()):
        qualified  = f"{prefix}.{member_name}"
        member_doc = getattr(member_value, "__doc__", None) or ""

        # Include the integer value for quick reference.
        try:
            int_val   = int(member_value)
            value_str = f"Integer value: {int_val}"
            if member_doc:
                doc = f"{member_doc}\n\n{value_str}"
            else:
                doc = value_str
        except (ValueError, TypeError):
            doc = member_doc

        entries.append({
            "name":      qualified,
            "kind":      "enum_value",
            "doc":       doc,
            "signature": None,
        })


# --- Classification ---

def _classify(obj):
    """Classify a Python object as class, method, enum, or None.

    RenderDoc enums are SWIG-generated IntEnum-like types. They are
    distinguished from regular classes by the presence of a __members__
    attribute (SWIG enum pattern) or by inheriting from int.

    Properties are not detected here. They require descriptor-level
    inspection via getattr_static, which is handled in _walk_class.
    """
    if isinstance(obj, type):
        if _is_swig_enum(obj):
            return "enum"
        return "class"
    elif callable(obj):
        return "method"
    else:
        return None


def _is_swig_enum(cls):
    """Check if a type is a SWIG-generated enum.

    SWIG enums are identified by either:
    - Having a __members__ dict attribute (SWIG enum convention).
    - Inheriting from int (IntEnum-like pattern).
    """
    has_members    = isinstance(getattr(cls, "__members__", None), dict)
    inherits_int   = issubclass(cls, int)
    return has_members or inherits_int


def _is_property_descriptor(obj):
    """Check if an object is a property descriptor.

    Detects both builtin property objects and SWIG-style descriptors
    that expose fget or are otherwise non-callable descriptors with
    __doc__.
    """
    if isinstance(obj, property):
        return True

    # SWIG property descriptors: have __get__ (descriptor protocol) but
    # are not regular functions or types.
    obj_type = type(obj)
    if obj_type.__name__ in ("getset_descriptor", "member_descriptor"):
        return True

    # Generic descriptor with fget (some SWIG versions).
    if hasattr(obj, "fget") and not isinstance(obj, type) and not callable(obj):
        return True

    return False


# --- Signature Extraction ---

def _get_signature(obj):
    """Extract a signature string from a callable.

    Tries inspect.signature first. When that fails (common for
    SWIG-wrapped methods), falls back to parsing the first line
    of the docstring, which SWIG conventionally formats as:
        method_name(arg1, arg2) -> ReturnType
    """
    if not callable(obj):
        return None

    # Preferred path: inspect.signature works on native Python callables.
    try:
        return str(inspect.signature(obj))
    except (ValueError, TypeError):
        pass

    # Fallback: parse SWIG-style docstring signature.
    return _parse_docstring_signature(obj)


# Pattern for SWIG docstring signatures.
# Matches lines like: "method_name(arg1, arg2) -> ReturnType"
# or just: "method_name(arg1, arg2)"
_SWIG_SIG_RE = re.compile(
    r"^\s*\w+\s*\(([^)]*)\)(?:\s*->\s*(.+))?\s*$"
)


def _parse_docstring_signature(obj):
    """Parse a signature from the first line of a SWIG-style docstring.

    Returns a signature string like "(arg1, arg2) -> ReturnType", or
    None if the docstring does not contain a recognizable signature.
    """
    doc = getattr(obj, "__doc__", None)
    if not doc:
        return None

    first_line = doc.strip().split("\n", 1)[0]
    match      = _SWIG_SIG_RE.match(first_line)
    if not match:
        return None

    args       = match.group(1).strip()
    ret        = match.group(2)
    sig        = f"({args})"
    if ret:
        sig = f"{sig} -> {ret.strip()}"

    return sig
