"""TCP client for communicating with the RenderDoc extension."""

import json
import socket


# Port range matching the extension's BridgeServer.
_PORT_RANGE = range(19876, 19886)
_PROBE_TIMEOUT = 0.2
_CONNECT_TIMEOUT = 2.0
_READ_TIMEOUT = 30.0
_WRITE_TIMEOUT = 5.0


class RenderDocClient:
    """JSON-lines TCP client that talks to the RenderDoc bridge extension."""

    def __init__(self):
        self._sock: socket.socket | None = None
        self._port: int | None             = None
        self._buffer: str                  = ""

    # --- Connection management ---

    def connect(self, port: int):
        """Connect to a RenderDoc instance on the given port."""
        self.disconnect()

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(_CONNECT_TIMEOUT)
        sock.connect(("127.0.0.1", port))
        sock.settimeout(None)

        self._sock = sock
        self._port = port

    def disconnect(self):
        """Close the current connection, if any."""
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
            self._port = None
            self._buffer = ""

    def discover_instances(self) -> list[dict]:
        """Probe the port range for running RenderDoc instances."""
        instances = []

        for port in _PORT_RANGE:
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(_PROBE_TIMEOUT)
                sock.connect(("127.0.0.1", port))
                sock.close()
                instances.append({"port": port})
            except (ConnectionRefusedError, TimeoutError, OSError):
                continue

        return instances

    def ensure_connected(self):
        """Auto-connect to the first available instance if not connected."""
        if self._sock is not None:
            return

        instances = self.discover_instances()
        if not instances:
            raise ConnectionError("no RenderDoc instances found")

        self.connect(instances[0]["port"])

    # --- Request / response ---

    def send(self, cmd: str, params: dict) -> dict:
        """Send a command and return the parsed response."""
        self.ensure_connected()
        assert self._sock is not None

        request = json.dumps({"cmd": cmd, "params": params}) + "\n"

        self._sock.settimeout(_WRITE_TIMEOUT)
        self._sock.sendall(request.encode("utf-8"))

        self._sock.settimeout(_READ_TIMEOUT)
        return self._read_response()

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
