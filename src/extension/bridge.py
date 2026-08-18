"""TCP bridge server that runs inside RenderDoc's Python environment.

Accepts JSON-lines requests over TCP and dispatches to handlers.

Two implementations share a common dispatch:

  _QtBridge       -- Event-driven QTcpServer. All Python execution stays
                     on the UI thread (matching qrenderdoc's console
                     pattern), so there is no second Python thread
                     allocating objects concurrently with the replay
                     thread during BlockInvoke. Preferred path.

  _ThreadedBridge -- Socket + threads fallback. Has a known Python 3.14
                     freelist race against the replay thread (see
                     Python/generated_cases.c.h tuple_alloc). Used only
                     when no Qt bindings are importable.
"""
import _thread
import json
import threading
import traceback
from typing import Any, Dict, Optional
from .handlers import HANDLERS
from .          import winsock

BUFFER_SIZE = 65536
MAX_REQUEST_BYTES = 8 * 1024 * 1024
REQUEST_IDLE_TIMEOUT_SECONDS = 5.0
MAX_ACTIVE_CONNECTIONS = 4


def protocol_error(error_code: str, message: str) -> Dict[str, Any]:
    """Build a backwards-compatible structured protocol error response."""
    return {
        "ok": False,
        "error": message,
        "error_code": error_code,
    }


class FrameResult:
    """One complete request or protocol error emitted by RequestFramer."""

    def __init__(
        self,
        request: Optional[Dict[str, Any]] = None,
        response: Optional[Dict[str, Any]] = None,
        close: bool = False,
    ) -> None:
        self.request = request
        self.response = response
        self.close = close


class RequestFramer:
    """Bounded state machine for newline-delimited JSON object requests.

    ``max_request_bytes`` includes the terminating newline. Bytes are examined
    before they are copied into the retained buffer, so the buffer never grows
    to the configured limit unless the next byte can validly be a newline.
    """

    def __init__(self, max_request_bytes: int = MAX_REQUEST_BYTES) -> None:
        if max_request_bytes < 1:
            raise ValueError("max_request_bytes must be positive")
        self._max_request_bytes = max_request_bytes
        self._buffer = bytearray()
        self._fatal = False

    @property
    def buffered_bytes(self) -> int:
        return len(self._buffer)

    def feed(self, data: bytes) -> list:
        """Consume bytes and return zero or more FrameResult instances."""
        if self._fatal or not data:
            return []

        results = []
        offset = 0

        while offset < len(data):
            newline = data.find(b"\n", offset)

            if newline < 0:
                remaining = len(data) - offset
                # The line still needs a trailing newline, which is included
                # in the request-size limit.
                if len(self._buffer) + remaining >= self._max_request_bytes:
                    results.append(self._fatal_result(
                        "request_too_large",
                        "request exceeds the maximum size of %d bytes"
                        % self._max_request_bytes,
                    ))
                    break
                self._buffer.extend(data[offset:])
                break

            segment_length = newline - offset + 1
            if len(self._buffer) + segment_length > self._max_request_bytes:
                results.append(self._fatal_result(
                    "request_too_large",
                    "request exceeds the maximum size of %d bytes"
                    % self._max_request_bytes,
                ))
                break

            self._buffer.extend(data[offset:newline])
            line = bytes(self._buffer)
            self._buffer.clear()
            results.append(self._parse_line(line))
            offset = newline + 1

        return results

    def finish(self) -> list:
        """Finish the stream, reporting a buffered partial request as fatal."""
        if self._fatal or not self._buffer:
            return []

        return [self._fatal_result(
            "incomplete_request",
            "connection closed before the request newline was received",
        )]

    def fatal_error(self, error_code: str, message: str) -> FrameResult:
        """Discard retained input and emit a fatal backend-generated error."""
        return self._fatal_result(error_code, message)

    def _fatal_result(self, error_code: str, message: str) -> FrameResult:
        self._buffer.clear()
        self._fatal = True
        return FrameResult(
            response=protocol_error(error_code, message),
            close=True,
        )

    def _parse_line(self, line: bytes) -> FrameResult:
        try:
            text = line.decode("utf-8")
        except UnicodeDecodeError:
            return FrameResult(response=protocol_error(
                "invalid_utf8",
                "invalid request: line is not valid UTF-8",
            ))

        try:
            request = json.loads(text)
        except (TypeError, ValueError):
            return FrameResult(response=protocol_error(
                "invalid_json",
                "invalid request: line is not valid JSON",
            ))

        if not isinstance(request, dict):
            return FrameResult(response=protocol_error(
                "request_not_object",
                "invalid request: top-level JSON value must be an object",
            ))

        return FrameResult(request=request)


# --- Qt bindings discovery ---

def _try_import_qt():
    """Import the first available Qt-Network binding.

    Returns (QTcpServer, QTcpSocket, QHostAddress, QTimer) or None.

    Importing Qt for the first time pulls in libQt6Core, libQt6Network,
    etc., which transitively makes the Vulkan loader register
    librenderdoc.so as an implicit capture layer (via
    /etc/vulkan/implicit_layer.d/renderdoc_capture.json on Linux). That
    conflicts with our own use of the same .so for replay. Callers that
    don't actually need Qt (i.e. force_threaded=True for headless
    workers) MUST avoid this function.
    """
    for mod in ("PyQt6", "PySide6", "PySide2", "PyQt5"):
        try:
            net = __import__(f"{mod}.QtNetwork", fromlist=["QTcpServer"])
            core = __import__(f"{mod}.QtCore", fromlist=["QTimer"])
            return (net.QTcpServer, net.QTcpSocket, net.QHostAddress, core.QTimer)
        except ImportError:
            continue
    return None


# --- Shared dispatch ---

def _dispatch(ctx: Any, request: Dict[str, Any]) -> Dict[str, Any]:
    """Run a single request's handler.

    Caller is responsible for serializing dispatches -- the replay API
    is single-threaded.
    """
    cmd    = request.get("cmd", "")
    params = request.get("params", {})

    handler_entry = HANDLERS.get(cmd)
    if handler_entry is None:
        return {"ok": False, "error": f"unknown command: {cmd}"}

    try:
        return handler_entry["func"](ctx, params)
    except Exception as e:
        traceback.print_exc()
        return {"ok": False, "error": str(e)}


# --- Qt event-driven bridge ---

class _QtConnectionState:
    """Bounded input and idle timer owned by one Qt client socket."""

    def __init__(self, framer: RequestFramer, timer: Any) -> None:
        self.framer = framer
        self.timer = timer


class _QtBridge:
    """TCP server driven by Qt's event loop on the UI thread.

    newConnection/readyRead/disconnected signals all fire on the UI
    thread. Handlers run inline on those signals; BlockInvoke hops to
    the replay thread exactly the way the Python console does.

    No Python threads are created by this class.
    """

    def __init__(self, ctx: Any, port_range: range) -> None:
        self._ctx        = ctx
        self._port_range = port_range
        self._port       : Optional[int]         = None
        self._server     : Any                = None
        self._timer_type : Any                = None
        self._connections: Dict[Any, _QtConnectionState] = {}

    @property
    def port(self) -> Optional[int]:
        return self._port

    def start(self) -> None:
        """Bind to the first available port and start listening."""
        qt = _try_import_qt()
        if qt is None:
            raise RuntimeError("_QtBridge.start called but no Qt binding available")
        QTcpServer, _QTcpSocket, QHostAddress, QTimer = qt
        server = QTcpServer()
        self._timer_type = QTimer

        for port in self._port_range:
            if server.listen(QHostAddress("127.0.0.1"), port):
                self._port   = port
                self._server = server
                server.newConnection.connect(self._on_new_connection)
                print(f"[Agentic] Listening on localhost:{port} (Qt bridge)")
                return

        start = self._port_range[0]
        end   = self._port_range[-1]
        print(f"[Agentic] Failed to start: all ports {start}-{end} in use")

    def stop(self) -> None:
        """Shut down the server and drop all connections."""
        if self._server is not None:
            self._server.close()
            self._server = None

        for sock in list(self._connections.keys()):
            try:
                sock.disconnectFromHost()
            except Exception:
                pass
        for state in self._connections.values():
            state.timer.stop()
        self._connections.clear()
        print("[Agentic] Server stopped")

    def _on_new_connection(self) -> None:
        """Accept every pending connection and wire up its slots."""
        while self._server and self._server.hasPendingConnections():
            sock = self._server.nextPendingConnection()

            if len(self._connections) >= MAX_ACTIVE_CONNECTIONS:
                self._write_response(sock, protocol_error(
                    "server_busy",
                    "server has reached the maximum of %d active connections"
                    % MAX_ACTIVE_CONNECTIONS,
                ))
                try:
                    sock.disconnected.connect(sock.deleteLater)
                    sock.disconnectFromHost()
                except Exception:
                    pass
                continue

            sock.setReadBufferSize(MAX_REQUEST_BYTES)
            timer = self._timer_type(sock)
            timer.setSingleShot(True)
            timer.timeout.connect(lambda s=sock: self._on_request_timeout(s))
            self._connections[sock] = _QtConnectionState(
                RequestFramer(),
                timer,
            )
            sock.readyRead.connect(lambda s=sock: self._on_ready_read(s))
            sock.disconnected.connect(lambda s=sock: self._on_disconnected(s))
            print(f"[Agentic] Connection accepted ({len(self._connections)} active)")

    def _on_ready_read(self, sock: Any) -> None:
        """Drain newly-arrived bytes and dispatch every complete line."""
        state = self._connections.get(sock)
        if state is None:
            return

        data = bytes(sock.readAll())
        if not data:
            return

        # QTcpSocket can return its entire bounded read buffer at once. Feed
        # fixed-size pieces so a burst of tiny lines cannot create an
        # arbitrarily large temporary FrameResult list.
        for offset in range(0, len(data), BUFFER_SIZE):
            for result in state.framer.feed(data[offset:offset + BUFFER_SIZE]):
                if result.request is not None:
                    self._write_response(sock, _dispatch(self._ctx, result.request))
                elif result.response is not None:
                    self._write_response(sock, result.response)

                if result.close:
                    self._close_connection(sock)
                    return

        if state.framer.buffered_bytes:
            state.timer.start(int(REQUEST_IDLE_TIMEOUT_SECONDS * 1000))
        else:
            state.timer.stop()

    def _on_request_timeout(self, sock: Any) -> None:
        """Reject a partial request after five seconds without new bytes."""
        state = self._connections.get(sock)
        if state is None or not state.framer.buffered_bytes:
            return

        result = state.framer.fatal_error(
            "request_timeout",
            "request did not receive a newline within %.1f seconds"
            % REQUEST_IDLE_TIMEOUT_SECONDS,
        )
        self._write_response(sock, result.response)
        self._close_connection(sock)

    @staticmethod
    def _write_response(sock: Any, response: Dict[str, Any]) -> None:
        """Attempt to serialize and flush one response to a Qt socket."""
        try:
            out = (json.dumps(response, separators=(",", ":")) + "\n").encode("utf-8")
            sock.write(out)
            sock.flush()
        except Exception:
            traceback.print_exc()

    def _close_connection(self, sock: Any) -> None:
        """Release tracked state and ask Qt to close the client socket."""
        state = self._connections.pop(sock, None)
        if state is not None:
            state.timer.stop()
        try:
            sock.disconnectFromHost()
        except Exception:
            pass

    def _on_disconnected(self, sock: Any) -> None:
        """Forget the socket and schedule it for deletion."""
        state = self._connections.get(sock)
        if state is not None:
            for result in state.framer.finish():
                if result.response is not None:
                    self._write_response(sock, result.response)
            state.timer.stop()
            self._connections.pop(sock, None)
        try:
            sock.deleteLater()
        except Exception:
            pass
        print(f"[Agentic] Connection closed ({len(self._connections)} active)")


# --- Threaded fallback ---

class JsonSocket:
    """JSON-lines protocol over a raw winsock connection."""

    def __init__(self, conn: Any) -> None:
        self._conn = conn
        self._framer = RequestFramer()
        self._pending = []

    def read_result(self) -> Optional[FrameResult]:
        """Read one framed request or protocol error. Blocks."""
        while True:
            if self._pending:
                return self._pending.pop(0)

            try:
                data = self._conn.recv(BUFFER_SIZE)
            except (winsock.SocketError, OSError) as error:
                if winsock.is_timeout_error(error):
                    if not self._framer.buffered_bytes:
                        continue
                    return self._framer.fatal_error(
                        "request_timeout",
                        "request did not receive a newline within %.1f seconds"
                        % REQUEST_IDLE_TIMEOUT_SECONDS,
                    )
                return None

            if not data:
                self._pending.extend(self._framer.finish())
                if self._pending:
                    return self._pending.pop(0)
                return None

            self._pending.extend(self._framer.feed(data))

    def write_response(self, response: Dict[str, Any]) -> None:
        """Write a JSON response followed by a newline."""
        data = json.dumps(response, separators=(",", ":")) + "\n"
        self._conn.sendall(data.encode("utf-8"))


class _ThreadedBridge:
    """Socket + threads fallback. Racy on Python 3.14 -- see module docstring."""

    def __init__(self, ctx: Any, port_range: range) -> None:
        self._ctx           = ctx
        self._port_range    = port_range
        self._port          : Optional[int]            = None
        self._server_socket : Any                   = None
        self._running       : bool                  = False
        self._thread        : Optional[threading.Thread] = None
        self._active_conns  : int                   = 0
        self._conn_lock                             = threading.Lock()
        self._dispatch_lock                         = threading.Lock()

    @property
    def port(self) -> Optional[int]:
        return self._port

    def start(self) -> None:
        if self._running:
            return
        self._running = True

        for port in self._port_range:
            try:
                self._server_socket = winsock.Socket()
                # POSIX enables SO_REUSEADDR for prompt rebinding. Windows
                # uses SO_EXCLUSIVEADDRUSE so a live GUI and worker cannot
                # become competing listeners on the same port.
                self._server_socket.configure_server_bind()
                self._server_socket.bind("127.0.0.1", port)
                self._server_socket.listen(5)
                self._port = port
                print(f"[Agentic] Listening on localhost:{port} (threaded fallback)")
                break
            except (winsock.SocketError, OSError):
                if self._server_socket:
                    try:
                        self._server_socket.close()
                    except Exception:
                        pass
                    self._server_socket = None

                if port == self._port_range[-1]:
                    start = self._port_range[0]
                    end   = self._port_range[-1]
                    print(f"[Agentic] Failed to start: all ports {start}-{end} in use")
                    self._running = False
                    return

        self._thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False

        if self._server_socket is not None:
            try:
                self._server_socket.close()
            except Exception:
                pass
            self._server_socket = None

        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

        print("[Agentic] Server stopped")

    def _accept_loop(self) -> None:
        while self._running:
            try:
                conn = self._server_socket.accept()

                with self._conn_lock:
                    if self._active_conns >= MAX_ACTIVE_CONNECTIONS:
                        count = None
                    else:
                        self._active_conns += 1
                        count = self._active_conns

                if count is None:
                    try:
                        try:
                            JsonSocket(conn).write_response(protocol_error(
                                "server_busy",
                                "server has reached the maximum of %d active connections"
                                % MAX_ACTIVE_CONNECTIONS,
                            ))
                        except (winsock.SocketError, OSError):
                            pass
                    finally:
                        try:
                            conn.close()
                        except Exception:
                            pass
                    continue

                print(f"[Agentic] Connection accepted ({count} active)")

                # See module docstring re: Python 3.14 threading bug.
                try:
                    _thread.start_new_thread(self._handle_connection, (conn,))
                except Exception:
                    conn.close()
                    with self._conn_lock:
                        self._active_conns -= 1
                    traceback.print_exc()
            except (winsock.SocketError, OSError):
                if self._running:
                    traceback.print_exc()
                break

    def _handle_connection(self, sock: Any) -> None:
        js = JsonSocket(sock)

        try:
            sock.settimeout(REQUEST_IDLE_TIMEOUT_SECONDS)
            while self._running:
                result = js.read_result()
                if result is None:
                    break

                if result.request is not None:
                    with self._dispatch_lock:
                        response = _dispatch(self._ctx, result.request)
                    js.write_response(response)
                elif result.response is not None:
                    js.write_response(result.response)

                if result.close:
                    break
        except Exception:
            traceback.print_exc()
        finally:
            sock.close()

            with self._conn_lock:
                self._active_conns -= 1
                count = self._active_conns

            print(f"[Agentic] Connection closed ({count} active)")


# --- Public facade ---

class BridgeServer:
    """TCP bridge server facade.

    Prefers the Qt event-driven backend. Falls back to the threaded
    backend if no Qt bindings are importable. A startup print identifies
    which path is active.
    """

    def __init__(
        self,
        ctx        : Any,
        port_range : range = range(19876, 19886),
        force_threaded: bool = False,
    ) -> None:
        # Only probe for Qt when we'd actually use it. Importing Qt
        # transitively pulls librenderdoc.so in as a Vulkan capture
        # layer, which conflicts with the replay role of the same .so
        # in headless workers.
        qt = None if force_threaded else _try_import_qt()

        if qt is not None:
            self._impl : Any = _QtBridge(ctx, port_range)
        else:
            if not force_threaded:
                print("[Agentic] Warning: no Qt bindings found (PyQt6/PySide6/"
                      "PySide2/PyQt5) -- using threaded fallback. This path has "
                      "a known Python 3.14 crash risk; install PySide6 or PyQt6 "
                      "for the stable path.")
            self._impl = _ThreadedBridge(ctx, port_range)

    @property
    def port(self) -> Optional[int]:
        return self._impl.port

    def start(self) -> None:
        self._impl.start()

    def stop(self) -> None:
        self._impl.stop()
