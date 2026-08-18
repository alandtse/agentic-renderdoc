"""Agentic RenderDoc extension entry point.

Registers the CaptureViewer and starts the TCP bridge server.
RenderDoc calls register() on load and unregister() on shutdown.
"""
from typing import Any, Optional

# qrenderdoc / renderdoc are only available when this package is loaded
# inside the RenderDoc GUI. The headless worker entry point imports
# this package's submodules (context, bridge, handlers, ...) without
# needing the GUI bindings, so those imports must stay at function
# scope rather than module scope.

from .bridge  import BridgeServer
from .context import GuiHandlerContext, HandlerContext


# --- Module state ---

_extension : Any                    = None
_extension_ctx : Any                = None
_server    : Optional[BridgeServer] = None


def _teardown() -> None:
    """Stop active extension resources without leaving stale module state."""
    global _extension, _extension_ctx, _server

    server        = _server
    extension     = _extension
    extension_ctx = _extension_ctx

    try:
        if server is not None:
            server.stop()
    except Exception as exc:
        print("[Agentic] Bridge stop failed: {}".format(exc))

    try:
        if extension is not None and extension_ctx is not None:
            extension_ctx.RemoveCaptureViewer(extension)
    except Exception as exc:
        print("[Agentic] CaptureViewer removal failed: {}".format(exc))
    finally:
        _server        = None
        _extension     = None
        _extension_ctx = None


def register(version: str, ctx: Any) -> None:
    """Called by RenderDoc when the extension is loaded.

    Sets up the handler context, registers the CaptureViewer, and
    starts the TCP bridge server.

    Skipped entirely when ``AGENTIC_DISABLE_AUTOLOAD`` is set in the
    environment. This is used by the Windows embedded-headless entry
    point (see ``embedded_headless.py``), which runs as
    ``qrenderdoc --script`` and provides its own ``EmbeddedHeadlessContext``
    plus bridge. Without this skip, qrenderdoc's ``AlwaysLoad_Extensions``
    auto-load would race the embedded script and bind a second bridge
    on the same port (Windows' SO_REUSEADDR allows hijacking-style
    coexistence), with incoming connections being routed to the
    GUI-context bridge instead of the embedded one.
    """
    global _extension, _extension_ctx, _server

    import os
    if os.environ.get("AGENTIC_DISABLE_AUTOLOAD"):
        print("[Agentic] AGENTIC_DISABLE_AUTOLOAD set; skipping auto-load")
        return

    print(f"[Agentic] Registering (RenderDoc {version})")

    if _server is not None or _extension is not None or _extension_ctx is not None:
        print("[Agentic] Existing registration found; cleaning up")
        _teardown()

    import qrenderdoc as qrd

    class AgenticExtension(qrd.CaptureViewer):
        """CaptureViewer that forwards capture open/close events to the context."""

        def __init__(self, ctx: Any, handler_ctx: HandlerContext) -> None:
            super().__init__()
            self._ctx         = ctx
            self._handler_ctx = handler_ctx

        def OnCaptureLoaded(self) -> None:
            self._handler_ctx.on_capture_loaded()
            print("[Agentic] Capture loaded")

        def OnCaptureClosed(self) -> None:
            self._handler_ctx.on_capture_closed()
            print("[Agentic] Capture closed")

        def OnSelectedEventChanged(self, event: int) -> None:
            pass

        def OnEventChanged(self, event: int) -> None:
            pass

    handler_ctx = GuiHandlerContext(ctx)

    try:
        _extension     = AgenticExtension(ctx, handler_ctx)
        _extension_ctx = ctx
        ctx.AddCaptureViewer(_extension)

        _server = BridgeServer(handler_ctx)
        _server.start()

        if _server.port is not None:
            handler_ctx._server_port = _server.port

        handler_ctx._bridge = _server
    except Exception:
        _teardown()
        raise


def unregister() -> None:
    """Called by RenderDoc when the extension is unloaded."""
    print("[Agentic] Unregistering")
    _teardown()