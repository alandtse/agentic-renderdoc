"""TCP client + connection pool for the RenderDoc bridge extension.

Two layers:

  RenderDocClient -- One JSON-lines TCP connection to one bridge port.
                     Owns the socket and the send/retry/timeout logic.
                     May hold an optional worker Popen reference so its
                     send-side liveness checks can report whether the
                     port belongs to a spawned worker.

  ConnectionPool  -- Named registry of RenderDocClient instances. Owns
                     headless-worker subprocess lifecycle (spawn / close
                     / reap) so workers and their bridge connections are
                     managed in lockstep under one alias. Routes send()
                     by alias; auto-resolves when exactly one connection
                     is active.
"""

import json
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing  import Dict, List, Optional


# Port range matching the extension's BridgeServer.
_PORT_RANGE         = range(19876, 19886)
_REMOTE_PORT_RANGE  = range(39920, 39930)
_PROBE_TIMEOUT      = 0.2
_ENRICH_TIMEOUT     = 1.0
_CONNECT_TIMEOUT    = 2.0
_WRITE_TIMEOUT      = 5.0
_WORKER_BIND_WAIT   = 30.0
_WORKER_GRACE_SECS  = 5.0

# Per-command read deadlines. Anything not in this map gets the default.
# eval and get_texture can drive SetFrameEvent which triggers a full
# frame replay — capture-size dependent, but rarely longer than ~60s in
# practice. Cheap metadata calls finish in milliseconds; 10s is plenty.
_DEFAULT_READ_TIMEOUT = 10.0
_READ_TIMEOUTS: Dict[str, float] = {
    "eval"        : 90.0,
    "get_texture" : 90.0,
}


def _format_timeout_error(e: "WorkerTimeoutError") -> dict:
    """Convert a WorkerTimeoutError into the structured response shape."""
    if e.is_spawned:
        hints = [
            f"the headless worker on port {e.port} (pid {e.pid}) "
            f"did not respond within {e.timeout:.0f}s — likely a hung "
            "SetFrameEvent or replay-driver wait that cannot be cancelled",
            f"force-terminate it with Instance(action='close', "
            f"alias=..., force=True), then re-open the capture with "
            "Instance(action='open', file=...)",
        ]
    else:
        hints = [
            f"the RenderDoc instance on port {e.port} did not respond "
            f"within {e.timeout:.0f}s",
            "this is a live RenderDoc UI, not a worker spawned by this "
            "server; restart RenderDoc or use Instance(action='disconnect')",
        ]
    return {
        "ok"    : False,
        "error" : {
            "kind"     : "worker_timeout",
            "message"  : str(e),
            "port"     : e.port,
            "cmd"      : e.cmd,
            "timeout"  : e.timeout,
            "headless" : e.is_spawned,
            "pid"      : e.pid,
            "hints"    : hints,
        },
    }


def _format_dead_error(e: "WorkerDeadError") -> dict:
    """Convert a WorkerDeadError into the structured response shape."""
    if e.is_spawned:
        hints = [
            f"the headless worker on port {e.port} (pid {e.pid}) is gone "
            "— it crashed, was killed, or its renderdoccmd remote-server "
            "child exited",
            "spawn a fresh one with Instance(action='open', file=...)",
        ]
    else:
        hints = [
            f"the RenderDoc instance on port {e.port} is unreachable",
            "use Instance(action='list') to see what's currently running",
        ]
    return {
        "ok"    : False,
        "error" : {
            "kind"     : "worker_dead",
            "message"  : str(e),
            "port"     : e.port,
            "cmd"      : e.cmd,
            "headless" : e.is_spawned,
            "pid"      : e.pid,
            "hints"    : hints,
        },
    }


def _find_qrenderdoc() -> str | None:
    """Locate ``qrenderdoc.exe`` (or ``qrenderdoc`` on POSIX) for spawning.

    Tries PATH first via ``shutil.which``, then the standard Windows
    install location. Returns the resolved absolute path or None if not
    found.
    """
    import shutil
    name = "qrenderdoc.exe" if sys.platform == "win32" else "qrenderdoc"
    found = shutil.which(name)
    if found:
        return found
    if sys.platform == "win32":
        candidates = [
            r"C:\Program Files\RenderDoc\qrenderdoc.exe",
            os.path.join(
                os.environ.get("LOCALAPPDATA", ""),
                "Programs", "RenderDoc", "qrenderdoc.exe",
            ),
        ]
        for c in candidates:
            if c and os.path.isfile(c):
                return c
    return None


def _die_with_parent() -> None:
    """preexec_fn: ask the kernel to SIGKILL us when our parent dies.

    Same protection as in extension.headless — guards against the case
    where this MCP server itself dies while its workers are running. The
    workers would then continue running indefinitely with no controller.
    Linux-only.
    """
    import ctypes
    import signal as _sig
    PR_SET_PDEATHSIG = 1
    try:
        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        libc.prctl(PR_SET_PDEATHSIG, _sig.SIGKILL, 0, 0, 0)
    except Exception:
        pass


class WorkerTimeoutError(TimeoutError):
    """A worker did not respond to a command before its deadline.

    Distinct from ``WorkerDeadError`` — the connection is fine, but
    something on the worker side is taking too long (often a hung
    SetFrameEvent that cannot be cancelled).
    """

    def __init__(
        self,
        port       : int,
        cmd        : str,
        timeout    : float,
        is_spawned : bool,
        pid        : Optional[int],
    ) -> None:
        super().__init__(
            f"worker on port {port} did not respond to {cmd!r} within {timeout}s"
        )
        self.port       = port
        self.cmd        = cmd
        self.timeout    = timeout
        self.is_spawned = is_spawned
        self.pid        = pid


class WorkerDeadError(ConnectionError):
    """A worker is unreachable: refused the connection, dropped EOF,
    or its process exited.
    """

    def __init__(
        self,
        port       : int,
        cmd        : str,
        is_spawned : bool,
        pid        : Optional[int],
        original   : Optional[Exception],
    ) -> None:
        super().__init__(
            f"worker on port {port} is unreachable for {cmd!r}: {original}"
        )
        self.port       = port
        self.cmd        = cmd
        self.is_spawned = is_spawned
        self.pid        = pid
        self.original   = original


class RenderDocClient:
    """JSON-lines TCP client that talks to one RenderDoc bridge port.

    Single-connection abstraction: one client holds at most one open
    socket to one port. Auto-discovery and worker lifecycle live on
    ConnectionPool — this class only handles the wire protocol, retries,
    and per-command timeouts for its own socket.

    A client may be associated with a Popen handle (set by ConnectionPool
    when the connection points at a worker the pool spawned) so that
    timeout/dead errors carry liveness info without the client having to
    know about the pool's full bookkeeping.
    """

    def __init__(self) -> None:
        self._sock          = None       # type: Optional[socket.socket]
        self._port          = None       # type: Optional[int]
        self._buffer        = ""
        self._worker_proc   = None       # type: Optional[subprocess.Popen]
        # Cached instance_info from the most recent send(). Populated by
        # ConnectionPool after a successful connect/open so callers can
        # surface capture_path / api_type without re-querying.
        self._info          = None       # type: Optional[dict]

    # --- Properties ---

    @property
    def connected_port(self) -> Optional[int]:
        """Return the port of the currently connected instance, or None."""
        return self._port

    @property
    def is_connected(self) -> bool:
        """Return True if a connection is currently established."""
        return self._sock is not None

    @property
    def is_headless(self) -> bool:
        """True if this client points at a worker spawned by the pool."""
        return self._worker_proc is not None

    # --- Connection management ---

    def connect(self, port: int, worker_proc: Optional[subprocess.Popen] = None) -> None:
        """Connect to a RenderDoc instance on the given port.

        Closes any existing connection first, then opens a new TCP socket
        to 127.0.0.1 on the specified port.

        port        -- Bridge port to connect to.
        worker_proc -- Optional Popen handle if the pool spawned this
                       worker. Lets liveness checks during send-retry
                       distinguish spawned-worker death from live-UI death.
        """
        self.disconnect()

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(_CONNECT_TIMEOUT)
        sock.connect(("127.0.0.1", port))
        sock.settimeout(None)

        self._sock        = sock
        self._port        = port
        self._worker_proc = worker_proc

    def disconnect(self) -> None:
        """Close the current connection, if any.

        Does NOT terminate any associated worker process — that lives on
        ConnectionPool and is intentionally orthogonal to dropping the
        socket. Use pool.close(alias) to stop the worker.
        """
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
        self._sock        = None
        self._port        = None
        self._buffer      = ""
        # Keep _worker_proc — the pool still owns it; we just dropped the socket.

    # --- Request / response ---

    def send(self, cmd: str, params: dict,
             read_timeout: Optional[float] = None) -> dict:
        """Send a command and return the parsed response.

        Connection-level failures during send produce a structured error
        dict (``{"ok": False, "error": {...}}``) rather than raising, so
        the agent can react without a try/except in every tool.

        Behaviors by failure mode:
          * Read timeout — single attempt, no retry. Returns a
            ``worker_timeout`` error.
          * Connection refused / EOF — checks subprocess liveness for
            spawned workers. If dead, returns ``worker_dead``. Otherwise
            reconnects once and retries.

        cmd          -- Command name (e.g. "eval", "instance_info").
        params       -- Command parameters dict.
        read_timeout -- Override the per-command default deadline.
        """
        if self._sock is None:
            raise ConnectionError(
                "not connected; pool must call connect() before send()"
            )

        try:
            return self._send_with_retry(cmd, params, read_timeout)
        except WorkerTimeoutError as e:
            return _format_timeout_error(e)
        except WorkerDeadError as e:
            return _format_dead_error(e)

    def _read_response(self) -> dict:
        """Read a newline-delimited JSON response."""
        assert self._sock is not None

        while "\n" not in self._buffer:
            chunk = self._sock.recv(65536)
            if not chunk:
                raise ConnectionError("connection closed by RenderDoc")
            self._buffer += chunk.decode("utf-8")

        line, self._buffer = self._buffer.split("\n", 1)
        return json.loads(line)

    # --- Internal helpers ---

    def _send_with_retry(self, cmd: str, params: dict,
                         read_timeout: Optional[float]) -> dict:
        """Execute a single send/recv with deadline + liveness-aware retry."""
        assert self._sock is not None
        assert self._port is not None

        port    = self._port
        timeout = read_timeout if read_timeout is not None \
                  else _READ_TIMEOUTS.get(cmd, _DEFAULT_READ_TIMEOUT)

        try:
            return self._do_send(cmd, params, read_timeout=timeout)
        except TimeoutError:
            raise WorkerTimeoutError(
                port       = port,
                cmd        = cmd,
                timeout    = timeout,
                is_spawned = self.is_headless,
                pid        = self._worker_pid(),
            )
        except (ConnectionError, BrokenPipeError, OSError) as first_err:
            # Connection-level failure. If our worker process is dead,
            # don't bother retrying.
            if not self._is_worker_alive():
                pid = self._worker_pid()
                self.disconnect()
                raise WorkerDeadError(
                    port       = port,
                    cmd        = cmd,
                    is_spawned = pid is not None,
                    pid        = pid,
                    original   = first_err,
                )

            # Worker still appears alive (or unknown live UI); reconnect once.
            saved_proc = self._worker_proc
            self.disconnect()
            try:
                self.connect(port, worker_proc=saved_proc)
                return self._do_send(cmd, params, read_timeout=timeout)
            except (ConnectionError, BrokenPipeError, OSError) as second_err:
                pid = self._worker_pid()
                raise WorkerDeadError(
                    port       = port,
                    cmd        = cmd,
                    is_spawned = pid is not None,
                    pid        = pid,
                    original   = second_err,
                )
            except TimeoutError:
                raise WorkerTimeoutError(
                    port       = port,
                    cmd        = cmd,
                    timeout    = timeout,
                    is_spawned = self.is_headless,
                    pid        = self._worker_pid(),
                )

    def _do_send(self, cmd: str, params: dict, read_timeout: float) -> dict:
        """Raw send and receive on the current socket."""
        assert self._sock is not None

        request = json.dumps({"cmd": cmd, "params": params}) + "\n"

        self._sock.settimeout(_WRITE_TIMEOUT)
        self._sock.sendall(request.encode("utf-8"))

        self._sock.settimeout(read_timeout)
        return self._read_response()

    def _is_worker_alive(self) -> bool:
        """True if the associated worker is alive or unknown (live UI)."""
        proc = self._worker_proc
        if proc is None:
            return True
        if proc.poll() is not None:
            return False
        try:
            os.kill(proc.pid, 0)
        except OSError:
            return False
        return True

    def _worker_pid(self) -> Optional[int]:
        """PID of the associated worker, or None if not headless."""
        return self._worker_proc.pid if self._worker_proc is not None else None


# ---------------------------------------------------------------------------
# Port discovery (module-level — pool and probes share this)
# ---------------------------------------------------------------------------

def _probe_port(port: int, enrich: bool = False) -> Optional[dict]:
    """Probe a single port for a running RenderDoc bridge.

    Attempts a TCP connection to 127.0.0.1 on the given port. If the
    connection succeeds and enrich is True, sends an instance_info
    command and merges the response into the returned dict.

    Returns a dict with at least {"port": port} on success, or None if
    the port is not reachable.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(_PROBE_TIMEOUT)

    try:
        sock.connect(("127.0.0.1", port))
    except (ConnectionRefusedError, TimeoutError, OSError):
        sock.close()
        return None

    result = {"port": port}
    if enrich:
        result = _enrich_instance(sock, result)
    sock.close()
    return result


def _enrich_instance(sock: socket.socket, instance: dict) -> dict:
    """Query instance_info over an already-connected probe socket."""
    try:
        request = json.dumps({"cmd": "instance_info", "params": {}}) + "\n"

        sock.settimeout(_ENRICH_TIMEOUT)
        sock.sendall(request.encode("utf-8"))

        buf = ""
        while "\n" not in buf:
            chunk = sock.recv(65536)
            if not chunk:
                return instance
            buf += chunk.decode("utf-8")

        line = buf.split("\n", 1)[0]
        resp = json.loads(line)

        if resp.get("ok") and isinstance(resp.get("data"), dict):
            merged = {**resp["data"], **instance}
            return merged
    except (ConnectionError, BrokenPipeError, OSError,
            json.JSONDecodeError, UnicodeDecodeError):
        pass

    return instance


def _first_free_port(port_range: range,
                     exclude: Optional[set] = None) -> Optional[int]:
    """First locally-bindable port in range, skipping exclude. None if all taken.

    A bind probe with SO_REUSEADDR is not enough on Windows: the
    extension's bridge listener also sets SO_REUSEADDR (upstream
    ``bridge.py``), so two listeners can coexist on the same port and
    bind() would silently succeed against a port that already has a
    running bridge. Connect-probe first to detect a live bridge.
    """
    excl = exclude or set()
    for port in port_range:
        if port in excl:
            continue
        if _probe_port(port) is not None:
            # Something is already listening — could be a GUI bridge on
            # the same port we'd otherwise hijack via SO_REUSEADDR.
            continue
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("127.0.0.1", port))
        except OSError:
            continue
        finally:
            s.close()
        return port
    return None


def _wait_for_bridge(proc: subprocess.Popen, port: int,
                     timeout: float) -> Optional[dict]:
    """Block until the worker binds on port and answers instance_info."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            return None
        info = _probe_port(port, enrich=True)
        if info is not None and info.get("capture_loaded"):
            return info
        time.sleep(0.25)
    return None


def _send_shutdown_to(port: int) -> None:
    """One-shot shutdown command to a bridge on a specific port."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(_CONNECT_TIMEOUT)
    s.connect(("127.0.0.1", port))
    s.settimeout(_WRITE_TIMEOUT)
    s.sendall(b'{"cmd":"shutdown","params":{}}\n')
    s.settimeout(2.0)
    try:
        s.recv(1024)
    except OSError:
        pass
    finally:
        s.close()


def _reap_proc(proc: subprocess.Popen) -> None:
    """Politely terminate a Popen we don't intend to track."""
    if proc.poll() is not None:
        return
    try:
        proc.terminate()
    except OSError:
        pass
    try:
        proc.wait(timeout=_WORKER_GRACE_SECS)
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
        except OSError:
            pass
        try:
            proc.wait(timeout=_WORKER_GRACE_SECS)
        except subprocess.TimeoutExpired:
            pass


# ---------------------------------------------------------------------------
# ConnectionPool — alias-routed registry of clients + spawned workers
# ---------------------------------------------------------------------------

def _alias_from_info(info: Optional[dict], port: int) -> str:
    """Derive a default alias from instance_info data.

    Uses the capture filename stem if a capture is loaded, otherwise
    falls back to "port_<N>".
    """
    path = info.get("capture_path") if info else None
    if path:
        return os.path.splitext(os.path.basename(path))[0]
    return "port_{}".format(port)


class ConnectionPool:
    """Named registry of RenderDoc connections and the workers they own.

    Each connection is identified by a user-chosen alias. Commands are
    routed to the appropriate instance by alias. When only one connection
    is active it is used automatically.

    Headless workers (spawned via ``open()``) are tracked alongside the
    connection: closing an alias's connection does NOT kill its worker;
    use ``close()`` for that. The connection and worker share an alias.
    """

    def __init__(self) -> None:
        # alias -> RenderDocClient
        self._connections = {}   # type: Dict[str, RenderDocClient]
        # alias -> Popen for headless workers we spawned
        self._workers     = {}   # type: Dict[str, subprocess.Popen]
        # Default alias; None means auto-select if exactly one connection exists.
        self._default     = None # type: Optional[str]

    # --- Connection lifecycle (external bridge ports) ---

    def connect(self, port: int,
                alias: Optional[str] = None) -> dict:
        """Connect to an existing bridge (live GUI or already-running worker).

        port  -- TCP port to connect to.
        alias -- Name to register under. Auto-derived from the connected
                 capture's filename stem if omitted.

        Returns the instance_info dict for the connection, with the
        chosen alias added.
        """
        client = RenderDocClient()
        client.connect(port)

        info = self._fetch_info(client)
        client._info = info

        if alias is None:
            alias = self._unique_alias(_alias_from_info(info, port))

        self._replace_alias(alias, client)
        result = dict(info or {})
        result["alias"] = alias
        result["port"]  = port
        return result

    def disconnect(self, alias: Optional[str] = None) -> str:
        """Close the named connection without killing any associated worker.

        If alias is omitted, disconnects the sole active connection;
        raises if more than one is active.

        Returns the alias that was disconnected.
        """
        target = self._only_alias(alias)
        client = self._connections.pop(target, None)
        if client is not None:
            try:
                client.disconnect()
            except Exception:
                pass

        if self._default == target:
            self._default = None
        return target

    # --- Worker lifecycle (spawn + close) ---

    def open(self, capture_path: str,
             alias: Optional[str] = None) -> dict:
        """Spawn a headless worker for a .rdc file and connect to it.

        capture_path -- Absolute path to the .rdc capture file.
        alias        -- Name to register under. Auto-derived from the
                        capture filename stem if omitted.

        Returns the worker metadata (alias, port, remote_port, pid, info).
        Raises RuntimeError on any spawn failure; the subprocess is
        reaped if it was started.
        """
        capture = Path(capture_path)
        if not capture.exists():
            raise RuntimeError("capture not found: {}".format(capture))

        used_ports = {c.connected_port for c in self._connections.values()
                      if c.connected_port is not None}
        bridge_port = _first_free_port(_PORT_RANGE, exclude=used_ports)
        if bridge_port is None:
            raise RuntimeError(
                "no free port in agentic range "
                "{}-{}".format(_PORT_RANGE.start, _PORT_RANGE.stop - 1)
            )

        # On Windows the standard RenderDoc distribution does not ship
        # ``renderdoc.pyd`` for external Python — the SWIG bindings are
        # compiled into ``qrenderdoc.exe``. The Windows branch of
        # ``_launch_worker`` runs headless logic inside qrenderdoc's
        # embedded Python via ``--script`` and uses in-process
        # ``rd.OpenCaptureFile`` rather than ``renderdoccmd
        # remoteserver``. No remote port needed there.
        windows_embedded = sys.platform == "win32"

        if windows_embedded:
            remote_port = None
        else:
            remote_port = _first_free_port(_REMOTE_PORT_RANGE)
            if remote_port is None:
                raise RuntimeError(
                    "no free port in remote range "
                    "{}-{}".format(_REMOTE_PORT_RANGE.start, _REMOTE_PORT_RANGE.stop - 1)
                )

        proc, stderr_drain, stderr_buffer = _launch_worker(
            capture.resolve(), bridge_port, remote_port,
        )

        info = _wait_for_bridge(proc, bridge_port, timeout=_WORKER_BIND_WAIT)
        if info is None:
            _reap_proc(proc)
            err_bytes = b"".join(stderr_buffer)
            err_text  = err_bytes.decode("utf-8", errors="replace").strip()

            # Embedded-headless mode (qrenderdoc --script) doesn't write
            # diagnostics to stderr — qrenderdoc is a GUI subprocess. Read
            # the per-port log file the script writes instead. The path
            # must match the one embedded_headless.py writes.
            log_text = ""
            if windows_embedded:
                base = os.environ.get("TEMP") or os.environ.get("TMP") or "."
                log_path = os.path.join(
                    base, f"agentic-renderdoc-embedded-{bridge_port}.log"
                )
                try:
                    with open(log_path, "r", encoding="utf-8", errors="replace") as f:
                        log_text = f.read().strip()
                except OSError:
                    pass

            if windows_embedded:
                diag  = log_text or err_text or "<empty>"
                label = "embedded-log"
            else:
                diag  = err_text or "<empty>"
                label = "stderr"
            raise RuntimeError(
                "headless worker did not bind on {} within {:.0f}s; "
                "{}: {}".format(bridge_port, _WORKER_BIND_WAIT, label, diag)
            )

        # Healthy — stop buffering stderr but keep the drain thread running.
        stderr_drain.set()
        stderr_buffer.clear()

        if alias is None:
            alias = self._unique_alias(_alias_from_info(info, bridge_port))

        client = RenderDocClient()
        client.connect(bridge_port, worker_proc=proc)
        client._info = info

        self._workers[alias] = proc
        self._replace_alias(alias, client)

        return {
            "alias"       : alias,
            "port"        : bridge_port,
            "remote_port" : remote_port,
            "pid"         : proc.pid,
            "info"        : info,
        }

    def close(self, alias: Optional[str] = None,
              force: bool = False) -> dict:
        """Stop a headless worker and drop its connection.

        Graceful path: ``shutdown`` command, SIGTERM, SIGKILL. With
        force=True jumps straight to SIGKILL.

        Returns a status dict. Raises KeyError if the alias has no
        associated worker (i.e. it was an external connect()).
        """
        target = self._only_alias(alias)
        proc = self._workers.get(target)
        if proc is None:
            return {
                "closed" : False,
                "alias"  : target,
                "reason" : (
                    "alias {!r} has no worker (external connection); "
                    "use Instance(action='disconnect') instead".format(target)
                ),
            }

        port = self._connections[target].connected_port

        if not force:
            try:
                if port is not None:
                    _send_shutdown_to(port)
            except Exception:
                pass
            try:
                proc.wait(timeout=_WORKER_GRACE_SECS)
            except subprocess.TimeoutExpired:
                pass

        if proc.poll() is None and not force:
            try:
                proc.terminate()
            except OSError:
                pass
            try:
                proc.wait(timeout=_WORKER_GRACE_SECS)
            except subprocess.TimeoutExpired:
                pass

        if proc.poll() is None:
            try:
                proc.kill()
            except OSError:
                pass
            try:
                proc.wait(timeout=_WORKER_GRACE_SECS)
            except subprocess.TimeoutExpired:
                pass

        rc = proc.returncode

        # Drop the connection and worker entry.
        client = self._connections.pop(target, None)
        if client is not None:
            try:
                client.disconnect()
            except Exception:
                pass
        self._workers.pop(target, None)
        if self._default == target:
            self._default = None

        return {
            "closed"    : True,
            "alias"     : target,
            "port"      : port,
            "exit_code" : rc,
            "force"     : force,
        }

    def reap_dead(self) -> List[str]:
        """Clean up workers whose process exited unexpectedly.

        Returns a list of aliases whose workers had died. Useful for
        liveness checks before reporting instance lists.
        """
        dead = []
        for alias, proc in list(self._workers.items()):
            if proc.poll() is not None:
                dead.append(alias)
                client = self._connections.pop(alias, None)
                if client is not None:
                    try:
                        client.disconnect()
                    except Exception:
                        pass
                self._workers.pop(alias, None)
                if self._default == alias:
                    self._default = None
        return dead

    # --- Routing ---

    def set_default(self, alias: str) -> None:
        """Set the alias used when send() is called without one."""
        if alias not in self._connections:
            raise KeyError("no connection named {!r}".format(alias))
        self._default = alias

    def send(self, cmd: str, params: dict,
             alias: Optional[str] = None,
             read_timeout: Optional[float] = None) -> dict:
        """Send a command to a named (or default) connection."""
        client = self._resolve(alias)
        return client.send(cmd, params, read_timeout=read_timeout)

    def ensure_connected(self) -> dict:
        """If no connections exist, discover and connect to the first port.

        Backwards-compatible with the no-config workflow upstream had:
        a tool called on a fresh pool will find any running RenderDoc
        instance and connect to it as the default alias.

        Returns a dict with port + alias + info for the (newly or
        previously) default connection. Raises ConnectionError if no
        running instance is found.
        """
        if self._connections:
            alias = self.default_alias
            client = self._connections[alias]
            return {
                "alias" : alias,
                "port"  : client.connected_port,
                "info"  : client._info,
            }

        for port in _PORT_RANGE:
            if _probe_port(port) is not None:
                result = self.connect(port)
                return {
                    "alias" : result["alias"],
                    "port"  : port,
                    "info"  : result,
                }
        raise ConnectionError("no RenderDoc instances found")

    # --- Discovery ---

    def discover_instances(self, enrich: bool = False) -> List[dict]:
        """Probe the port range and return all running bridges.

        Annotates each entry with its alias if it is already in the pool,
        and with a ``headless`` flag if the alias was spawned by us.
        """
        port_to_alias = {
            c.connected_port: a
            for a, c in self._connections.items()
            if c.connected_port is not None
        }

        results = []
        for port in _PORT_RANGE:
            info = _probe_port(port, enrich=enrich)
            if info is None:
                continue
            alias = port_to_alias.get(port)
            if alias is not None:
                info["alias"]    = alias
                info["headless"] = alias in self._workers
            results.append(info)
        return results

    # --- Status ---

    @property
    def aliases(self) -> List[str]:
        """All active connection aliases."""
        return list(self._connections.keys())

    @property
    def default_alias(self) -> Optional[str]:
        """Default alias for send(); auto-selects if exactly one connection."""
        if self._default is None and len(self._connections) == 1:
            return next(iter(self._connections))
        return self._default

    def connection_info(self) -> List[dict]:
        """Summary of all active connections: alias, port, info, headless."""
        result = []
        for alias, client in self._connections.items():
            entry = {
                "alias"    : alias,
                "port"     : client.connected_port,
                "headless" : alias in self._workers,
            }
            if client._info:
                entry["info"] = client._info
            result.append(entry)
        return result

    # --- Internal helpers ---

    def _resolve(self, alias: Optional[str]) -> RenderDocClient:
        """Return the client for the given alias, falling back to the default."""
        if not self._connections:
            raise ConnectionError(
                "no RenderDoc connections; "
                "use Instance(action='connect') or Instance(action='open')"
            )

        target = alias or self.default_alias
        if target is None:
            names = ", ".join(repr(a) for a in self._connections)
            raise ConnectionError(
                "multiple instances connected ({}); "
                "specify alias= or use Instance(action='set_default')"
                .format(names)
            )

        if target not in self._connections:
            raise KeyError(
                "no connection named {!r}; available: {}"
                .format(target, list(self._connections))
            )

        return self._connections[target]

    def _only_alias(self, alias: Optional[str]) -> str:
        """Resolve a sole-active alias when none was passed explicitly."""
        if alias is not None:
            return alias
        if len(self._connections) == 1:
            return next(iter(self._connections))
        if not self._connections:
            raise KeyError("no active connections")
        raise KeyError(
            "multiple connections active; specify alias= to choose one"
        )

    def _unique_alias(self, candidate: str) -> str:
        """Append a numeric suffix if needed to avoid clobbering."""
        if candidate not in self._connections:
            return candidate
        i = 2
        while "{}_{}".format(candidate, i) in self._connections:
            i += 1
        return "{}_{}".format(candidate, i)

    def _replace_alias(self, alias: str, client: RenderDocClient) -> None:
        """Register client under alias, evicting any prior occupant."""
        existing = self._connections.get(alias)
        if existing is not None:
            try:
                existing.disconnect()
            except Exception:
                pass
        self._connections[alias] = client
        if self._default is None:
            self._default = alias

    @staticmethod
    def _fetch_info(client: RenderDocClient) -> Optional[dict]:
        """Best-effort instance_info fetch right after connect."""
        try:
            resp = client.send("instance_info", {})
        except (ConnectionError, OSError):
            return None
        if resp.get("ok") and isinstance(resp.get("data"), dict):
            return resp["data"]
        return None


# ---------------------------------------------------------------------------
# Worker subprocess launch — module-level so the pool can stay focused
# on lifecycle bookkeeping rather than process plumbing.
# ---------------------------------------------------------------------------

def _launch_worker(capture_path: Path, bridge_port: int, remote_port: Optional[int]):
    """Spawn the headless worker subprocess.

    Returns (Popen, stderr_drain_event, stderr_buffer_list). The pool is
    responsible for ``_wait_for_bridge`` afterwards and for setting the
    drain event once the worker is healthy.

    On Windows ``remote_port`` is ignored — the worker runs inside
    ``qrenderdoc.exe --script`` and uses in-process
    ``rd.OpenCaptureFile`` rather than ``renderdoccmd remoteserver``.
    """
    src_dir = Path(__file__).resolve().parent.parent
    env = os.environ.copy()
    existing_pp = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = (
        "{}{}{}".format(src_dir, os.pathsep, existing_pp)
        if existing_pp else str(src_dir)
    )

    if sys.platform == "win32":
        qrd_path = _find_qrenderdoc()
        if qrd_path is None:
            raise RuntimeError(
                "could not locate qrenderdoc.exe (looked on PATH and "
                "%ProgramFiles%\\RenderDoc); install RenderDoc system-wide"
            )
        embedded_script = src_dir / "extension" / "embedded_headless.py"
        cmd = [qrd_path, "--script", str(embedded_script)]
        # qrenderdoc does not populate sys.argv inside --script, so the
        # embedded script reads config from env vars. PKG_PARENT is the
        # directory the script prepends to sys.path so it can ``from
        # extension.context import ...`` etc.
        env["AGENTIC_EMBEDDED_CAPTURE"]    = str(capture_path)
        env["AGENTIC_EMBEDDED_PORT_MIN"]   = str(bridge_port)
        env["AGENTIC_EMBEDDED_PORT_MAX"]   = str(bridge_port)
        env["AGENTIC_EMBEDDED_PKG_PARENT"] = str(src_dir)
        # Prevent qrenderdoc's AlwaysLoad_Extensions auto-load from
        # binding a competing bridge on the same port. With Windows'
        # SO_REUSEADDR semantics both bridges would coexist as listeners
        # and incoming connections would routinely hit the
        # GuiHandlerContext-backed one instead of ours.
        env["AGENTIC_DISABLE_AUTOLOAD"]    = "1"
    else:
        cmd = [
            sys.executable,
            "-u",
            "-m", "extension.headless",
            str(capture_path),
            "--port-min",        str(bridge_port),
            "--port-max",        str(bridge_port),
            "--remote-port-min", str(remote_port),
            "--remote-port-max", str(remote_port),
        ]
        # Disable Vulkan implicit layers in the worker. The system has the
        # RenderDoc capture layer registered globally and the Vulkan loader
        # will auto-inject it into any process that initialises Vulkan. That
        # conflicts with the replay role of the same library — librenderdoc.so
        # loaded twice with different roles asserts and eventually drops the
        # proxy socket with EBADF. Set both the all-layers disable and the
        # specific layer disable for belt-and-suspenders coverage.
        env["VK_LOADER_LAYERS_DISABLE"]             = "*"
        env["DISABLE_VK_LAYER_RENDERDOC_Capture_1"] = "1"

    proc = subprocess.Popen(
        cmd,
        stdin      = subprocess.DEVNULL,
        stdout     = subprocess.DEVNULL,
        stderr     = subprocess.PIPE,
        env        = env,
        preexec_fn = _die_with_parent if sys.platform.startswith("linux") else None,
    )

    # Drain stderr into a buffer until the worker is healthy, then
    # discard. Avoids the pipe filling and blocking the worker.
    stderr_buffer = []
    stderr_drain  = threading.Event()

    def _drain() -> None:
        stream = proc.stderr
        if stream is None:
            return
        try:
            while True:
                chunk = stream.read(4096)
                if not chunk:
                    return
                if not stderr_drain.is_set():
                    stderr_buffer.append(chunk)
        except Exception:
            return

    threading.Thread(
        target = _drain,
        name   = "agentic-stderr-{}".format(bridge_port),
        daemon = True,
    ).start()

    return proc, stderr_drain, stderr_buffer
