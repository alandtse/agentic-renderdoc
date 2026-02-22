"""TCP bridge server that runs inside RenderDoc's Python environment.

Accepts JSON-lines requests over TCP and dispatches to handlers.
Ported from orb-renderdoc v1.
"""

import json
import threading

from extension.handlers import HANDLERS


class BridgeServer:
    """Multi-threaded TCP server for the RenderDoc extension.

    Runs in a background thread. Serializes RenderDoc API access with a
    dispatch lock (the replay API is single-threaded).
    """

    def __init__(self, ctx, port_range=range(19876, 19886)):
        self._ctx           = ctx
        self._port_range    = port_range
        self._port          = None
        self._server_socket = None
        self._running       = False
        self._dispatch_lock = threading.Lock()

    @property
    def port(self):
        return self._port

    def start(self):
        """Bind to the first available port and start listening."""
        # TODO: Port from v1's BridgeServer using winsock.py.
        pass

    def stop(self):
        """Shut down the server and close all connections."""
        self._running = False
        if self._server_socket is not None:
            try:
                self._server_socket.close()
            except Exception:
                pass

    def _handle_connection(self, sock):
        """Handle a single client connection (runs in its own thread)."""
        # TODO: JSON-lines read/dispatch/respond loop.
        pass

    def _dispatch(self, request):
        """Dispatch a request to the appropriate handler."""
        cmd    = request.get("cmd", "")
        params = request.get("params", {})

        handler_entry = HANDLERS.get(cmd)
        if handler_entry is None:
            return {"ok": False, "error": f"unknown command: {cmd}"}

        with self._dispatch_lock:
            try:
                return handler_entry["func"](self._ctx, params)
            except Exception as e:
                return {"ok": False, "error": str(e)}
