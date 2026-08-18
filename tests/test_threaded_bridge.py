"""Deterministic tests for the threaded bridge protocol boundaries."""

import json
import socket
import sys

import pytest

from extension import bridge


AUTH_TOKEN = "a" * 64


class FakeSocket:
    def __init__(self, reads=()):
        self.reads = list(reads)
        self.writes = []
        self.timeouts = []
        self.closed = False

    def recv(self, _size):
        item = self.reads.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    def settimeout(self, seconds):
        self.timeouts.append(seconds)

    def sendall(self, data):
        self.writes.append(bytes(data))

    def close(self):
        self.closed = True


class FakeSignal:
    def __init__(self):
        self.callbacks = []

    def connect(self, callback):
        self.callbacks.append(callback)

    def emit(self):
        for callback in list(self.callbacks):
            callback()


class FakeQtTimer:
    def __init__(self, _parent=None):
        self.timeout = FakeSignal()

    def setSingleShot(self, _enabled):
        pass

    def start(self, _interval):
        pass

    def stop(self):
        pass


class FakeQtSocket:
    def __init__(self):
        self.readyRead = FakeSignal()
        self.disconnected = FakeSignal()
        self.incoming = bytearray()
        self.writes = []

    def setReadBufferSize(self, _size):
        pass

    def readAll(self):
        data = bytes(self.incoming)
        self.incoming.clear()
        return data

    def write(self, data):
        self.writes.append(bytes(data))

    def flush(self):
        pass


class FakeQtServer:
    def __init__(self, sock):
        self.sock = sock

    def hasPendingConnections(self):
        return self.sock is not None

    def nextPendingConnection(self):
        sock = self.sock
        self.sock = None
        return sock


def _timeout_error():
    if sys.platform == "win32":
        error = OSError("timed out")
        error.wsa_error = bridge.winsock.WSAETIMEDOUT
        return error
    return socket.timeout()


def _responses(sock):
    return [json.loads(item.decode("utf-8")) for item in sock.writes]


def _run_connection(sock, monkeypatch, dispatch=None):
    threaded = bridge._ThreadedBridge(
        object(),
        range(19876, 19877),
        auth_token=AUTH_TOKEN,
    )
    threaded._running = True
    threaded._active_conns = 1
    if dispatch is not None:
        monkeypatch.setattr(bridge, "_dispatch", dispatch)
    threaded._handle_connection(sock)
    assert threaded._active_conns == 0
    return threaded


def test_threaded_timeout_after_partial_request_is_deterministic(monkeypatch):
    sock = FakeSocket([b'{"cmd":"eval"', _timeout_error()])

    _run_connection(sock, monkeypatch)

    assert sock.timeouts == [bridge.REQUEST_IDLE_TIMEOUT_SECONDS]
    assert _responses(sock)[0]["error_code"] == "request_timeout"
    assert sock.closed is True


def test_threaded_timeout_without_partial_request_keeps_waiting(monkeypatch):
    request = json.dumps({"auth": AUTH_TOKEN, "cmd": "eval"}).encode("utf-8") + b"\n"
    sock = FakeSocket([_timeout_error(), request, b""])
    monkeypatch.setattr(
        bridge,
        "_dispatch",
        lambda _ctx, request: {"ok": True, "cmd": request["cmd"]},
    )

    _run_connection(sock, monkeypatch)

    assert _responses(sock) == [{"ok": True, "cmd": "eval"}]


def test_threaded_buffered_eof_attempts_error_and_closes(monkeypatch):
    sock = FakeSocket([b'{"cmd":"eval"', b""])

    _run_connection(sock, monkeypatch)

    assert _responses(sock)[0]["error_code"] == "incomplete_request"
    assert sock.closed is True


def test_threaded_oversized_request_reports_error_and_closes(monkeypatch):
    original_framer = bridge.RequestFramer
    monkeypatch.setattr(
        bridge,
        "RequestFramer",
        lambda: original_framer(max_request_bytes=16),
    )
    sock = FakeSocket([b"x" * 16])

    _run_connection(sock, monkeypatch)

    assert _responses(sock)[0]["error_code"] == "request_too_large"
    assert sock.closed is True


def test_threaded_rejects_connection_above_active_limit(monkeypatch):
    extra = FakeSocket()
    threaded = bridge._ThreadedBridge(
        object(),
        range(19876, 19877),
        auth_token=AUTH_TOKEN,
    )
    threaded._running = True
    threaded._active_conns = bridge.MAX_ACTIVE_CONNECTIONS

    class OneConnectionServer:
        def accept(self):
            threaded._running = False
            return extra

    started = []
    threaded._server_socket = OneConnectionServer()
    monkeypatch.setattr(
        bridge._thread,
        "start_new_thread",
        lambda *args: started.append(args),
    )

    threaded._accept_loop()

    assert threaded._active_conns == bridge.MAX_ACTIVE_CONNECTIONS
    assert _responses(extra)[0]["error_code"] == "server_busy"
    assert extra.closed is True
    assert started == []


def test_large_eval_and_texture_requests_still_dispatch(monkeypatch):
    code = "x" * (1024 * 1024)
    eval_request = {
        "auth": AUTH_TOKEN,
        "cmd": "eval",
        "params": {"code": code},
    }
    texture_request = {
        "auth": AUTH_TOKEN,
        "cmd": "get_texture",
        "params": {"resource_id": "ResourceId::1", "mip": 0},
    }
    payload = (
        json.dumps(eval_request, separators=(",", ":"))
        + "\n"
        + json.dumps(texture_request, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")
    sock = FakeSocket([payload[:65536], payload[65536:], b""])
    seen = []

    def dispatch(_ctx, request):
        seen.append(request)
        return {"ok": True, "cmd": request["cmd"]}

    _run_connection(sock, monkeypatch, dispatch)

    assert len(seen[0]["params"]["code"]) == 1024 * 1024
    assert seen[1] == texture_request
    assert _responses(sock) == [
        {"ok": True, "cmd": "eval"},
        {"ok": True, "cmd": "get_texture"},
    ]


def test_qt_and_threaded_backends_emit_matching_protocol(monkeypatch):
    payload = (
        b"\xff\n{]\n[]\n"
        + json.dumps({
            "auth": AUTH_TOKEN,
            "cmd": "eval",
            "params": {},
        }).encode("utf-8")
        + b"\n"
    )
    dispatch = lambda _ctx, request: {"ok": True, "cmd": request["cmd"]}
    monkeypatch.setattr(bridge, "_dispatch", dispatch)

    threaded_sock = FakeSocket([payload, b""])
    _run_connection(threaded_sock, monkeypatch)

    qt_backend = bridge._QtBridge(
        object(),
        range(19876, 19877),
        auth_token=AUTH_TOKEN,
    )
    qt_sock = FakeQtSocket()
    qt_backend._server = FakeQtServer(qt_sock)
    qt_backend._timer_type = FakeQtTimer
    qt_backend._on_new_connection()
    qt_sock.incoming.extend(payload)
    qt_sock.readyRead.emit()

    assert _responses(threaded_sock) == _responses(qt_sock)
    assert [item.get("error_code") for item in _responses(threaded_sock)] == [
        "invalid_utf8",
        "invalid_json",
        "request_not_object",
        None,
    ]


@pytest.mark.parametrize("auth", [None, "b" * 64])
def test_threaded_rejects_unauthorized_request_before_dispatch(
    monkeypatch,
    auth,
):
    request = {"cmd": "eval", "params": {}}
    if auth is not None:
        request["auth"] = auth
    sock = FakeSocket([
        (json.dumps(request) + "\n").encode("utf-8"),
        b"",
    ])
    dispatched = []

    _run_connection(
        sock,
        monkeypatch,
        lambda _ctx, value: dispatched.append(value),
    )

    assert _responses(sock) == [{
        "ok": False,
        "error": "unauthorized",
        "error_code": "unauthorized",
    }]
    assert dispatched == []
    assert sock.closed is True
