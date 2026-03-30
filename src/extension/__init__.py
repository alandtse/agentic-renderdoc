"""Agentic RenderDoc extension entry point.

Registers the CaptureViewer and starts the TCP bridge server.
RenderDoc calls register() on load and unregister() on shutdown.
"""
from __future__ import annotations

import threading
from collections.abc import Callable
from typing          import Any

import qrenderdoc as qrd
import renderdoc as rd

from .bridge    import BridgeServer
from .api_index import build_index


class _TrackedController:
    """Thin proxy around a ReplayController that tracks API call ordering.

    Delegates all attribute access to the wrapped controller. Watches for
    SetFrameEvent and GetPipelineState to detect the common mistake of
    querying pipeline state without first selecting an event.
    """

    def __init__(self, controller: Any, warnings: list[str]) -> None:
        """Wrap a ReplayController and record warnings into the given list.

        controller -- The real rd.ReplayController instance.
        warnings   -- Mutable list to append warning strings to.
        """
        self._controller         = controller
        self._warnings           = warnings
        self._set_frame_called   = False
        # Standard Python proxy convention. Lets introspection tools
        # (like our inspect() utility) discover the real object.
        self.__wrapped__         = controller

    def SetFrameEvent(self, *args: Any, **kwargs: Any) -> Any:
        """Record that an event was selected, then forward the call."""
        self._set_frame_called = True
        return self._controller.SetFrameEvent(*args, **kwargs)

    def GetPipelineState(self, *args: Any, **kwargs: Any) -> Any:
        """Warn if no event was selected, then forward the call."""
        if not self._set_frame_called:
            self._warnings.append(
                "GetPipelineState() called without a prior "
                "SetFrameEvent() in this replay callback. "
                "The returned state may be stale or empty."
            )
        return self._controller.GetPipelineState(*args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        """Forward everything else to the real controller."""
        return getattr(self._controller, name)

    def __dir__(self) -> list[str]:
        """Expose the wrapped controller's attributes for introspection."""
        names = set(dir(self._controller))
        names.update(super().__dir__())
        return sorted(names)


class HandlerContext:
    """Shared context passed to every handler invocation.

    Provides thread-safe access to the replay controller and UI thread,
    and tracks capture lifecycle state. The bridge server sets
    _server_port after binding so handlers can report it.
    """

    def __init__(self, ctx: Any) -> None:
        """Create a handler context.

        ctx -- qrenderdoc.CaptureContext from the host.
        """
        self.ctx                              = ctx
        self._server_port       : int         = 0
        self._capture_loaded    : bool        = False
        self._api_type          : Any         = None
        self._capture_path      : str | None  = None
        self._event_count       : int         = 0
        self._api_index         : dict | None = None
        self._replay_controller : Any         = None
        self._replay_warnings   : list[str]   = []

    @property
    def capture_loaded(self) -> bool:
        """Whether a capture file is currently open."""
        return self._capture_loaded

    @property
    def api_index(self) -> dict | None:
        """The pre-built API reference index, or None."""
        return self._api_index

    @property
    def structured_file(self) -> Any:
        """The capture's SDFile, needed for ActionDescription.GetName()."""
        return self.ctx.GetStructuredFile()

    def on_capture_loaded(self) -> None:
        """Update state when a capture is opened.

        Reads API type, capture path, and event count from the live
        context. Builds the API index on first load.
        """
        self._capture_loaded = True

        # Pull metadata from the capture context.
        try:
            self._api_type     = self.ctx.APIProps().pipelineType
            self._capture_path = self.ctx.GetCaptureFilename()
            self._event_count  = self.ctx.GetLastAction().eventId + 1
        except Exception:
            pass

        # Build the API index once (it doesn't change between captures).
        if self._api_index is None:
            self._api_index = build_index()

    def on_capture_closed(self) -> None:
        """Reset capture-dependent state when a capture is closed."""
        self._capture_loaded = False
        self._api_type       = None
        self._capture_path   = None
        self._event_count    = 0

    def replay(self, callback: Callable[[Any], Any]) -> Any:
        """Execute callback on the replay thread with the ReplayController.

        Calls BlockInvoke from the bridge handler thread. The UI thread
        remains free to process events.

        Blocks until the callback completes and returns its result.
        Raises if no capture is loaded or the callback throws.

        callback -- Callable[[rd.ReplayController], Any].
        """
        if not self._capture_loaded:
            raise RuntimeError("no capture loaded")

        # Reset warnings at the start of each top-level replay call.
        self._replay_warnings = []

        # Detect re-entrant calls. Nesting BlockInvoke deadlocks the
        # replay thread. If we're already inside a callback, reuse the
        # active controller directly. The proxy is already in place from
        # the outer call.
        if self._replay_controller is not None:
            return callback(self._replay_controller)

        result    = [None]
        exception = [None]

        def wrapper(controller):
            tracked = _TrackedController(controller, self._replay_warnings)
            self._replay_controller = tracked
            try:
                result[0] = callback(tracked)
            except Exception as e:
                exception[0] = e
            finally:
                self._replay_controller = None

        self.ctx.Replay().BlockInvoke(wrapper)

        if exception[0]:
            raise exception[0]
        return result[0]

    def invoke_ui(self, callback: Callable[[], None]) -> None:
        """Execute callback on the UI thread.

        Use for operations that touch the UI: SetEventID, opening
        windows, etc. Blocks until the callback completes (5s timeout).

        callback -- Callable[[], None].
        """
        exception = [None]
        done      = threading.Event()

        def wrapper():
            try:
                callback()
            except Exception as e:
                exception[0] = e
            finally:
                done.set()

        helper = self.ctx.Extensions().GetMiniQtHelper()
        helper.InvokeOntoUIThread(wrapper)
        done.wait(timeout=5.0)

        if exception[0]:
            raise exception[0]


class AgenticExtension(qrd.CaptureViewer):
    """CaptureViewer that receives lifecycle callbacks from RenderDoc.

    Creates a HandlerContext and BridgeServer. Forwards capture open/close
    events to the context.
    """

    def __init__(self, ctx: Any, handler_ctx: HandlerContext) -> None:
        """Create the extension viewer.

        ctx         -- qrenderdoc.CaptureContext from the host.
        handler_ctx -- HandlerContext shared with the bridge server.
        """
        super().__init__()
        self._ctx         = ctx
        self._handler_ctx = handler_ctx

    def OnCaptureLoaded(self) -> None:
        """Called by RenderDoc when a capture file is opened."""
        self._handler_ctx.on_capture_loaded()
        print("[Agentic] Capture loaded")

    def OnCaptureClosed(self) -> None:
        """Called by RenderDoc when the capture is closed."""
        self._handler_ctx.on_capture_closed()
        print("[Agentic] Capture closed")

    def OnSelectedEventChanged(self, event: int) -> None:
        """Called when the user selects a different event."""
        pass

    def OnEventChanged(self, event: int) -> None:
        """Called when the viewed event changes."""
        pass


# --- Module state ---

_extension : AgenticExtension | None = None
_server    : BridgeServer | None    = None


def register(version: str, ctx: Any) -> None:
    """Called by RenderDoc when the extension is loaded.

    Sets up the handler context, registers the CaptureViewer, and
    starts the TCP bridge server.

    version -- RenderDoc version string.
    ctx     -- qrenderdoc.CaptureContext.
    """
    global _extension, _server

    print(f"[Agentic] Registering (RenderDoc {version})")

    handler_ctx = HandlerContext(ctx)

    _extension = AgenticExtension(ctx, handler_ctx)
    ctx.AddCaptureViewer(_extension)

    _server = BridgeServer(handler_ctx)
    _server.start()

    # Propagate the bound port so instance_info can report it.
    if _server.port is not None:
        handler_ctx._server_port = _server.port


def unregister() -> None:
    """Called by RenderDoc when the extension is unloaded."""
    global _extension, _server

    print("[Agentic] Unregistering")

    if _server is not None:
        _server.stop()
        _server = None

    _extension = None
