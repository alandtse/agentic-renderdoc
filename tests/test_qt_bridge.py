"""RenderDoc- and Qt-independent tests for the Qt bridge boundaries."""

import json

from extension import bridge


class FakeSignal:
    def __init__(self):
        self.callbacks = []

    def connect(self, callback):
        self.callbacks.append(callback)

    def emit(self):
        for callback in list(self.callbacks):
            callback()


class FakeTimer:
    def __init__(self, parent=None):
        self.parent = parent
        self.timeout = FakeSignal()
        self.single_shot = False
        self.active = False
        self.interval = None

    def setSingleShot(self, enabled):
        self.single_shot = enabled

    def start(self, interval):
        self.active = True
        self.interval = interval

    def stop(self):
        self.active = False


class FakeSocket:
    def __init__(self):
        self.readyRead = FakeSignal()
        self.disconnected = FakeSignal()
        self.incoming = bytearray()
        self.writes = []
        self.read_buffer_size = None
        self.disconnect_called = False
        self.deleted = False

    def setReadBufferSize(self, size):
        self.read_buffer_size = size

    def readAll(self):
        data = bytes(self.incoming)
        self.incoming.clear()
        return data

    def write(self, data):
        self.writes.append(bytes(data))
        return len(data)

    def flush(self):
        return True

    def disconnectFromHost(self):
        self.disconnect_called = True

    def deleteLater(self):
        self.deleted = True


class FakeServer:
    def __init__(self, sockets):
        self.sockets = list(sockets)

    def hasPendingConnections(self):
        return bool(self.sockets)

    def nextPendingConnection(self):
        return self.sockets.pop(0)


def _accept(qt_bridge, *sockets):
    qt_bridge._server = FakeServer(sockets)
    qt_bridge._timer_type = FakeTimer
    qt_bridge._on_new_connection()


def _responses(sock):
    return [json.loads(item.decode("utf-8")) for item in sock.writes]


def test_qt_connection_bounds_buffer_and_resets_idle_timer(monkeypatch):
    qt_bridge = bridge._QtBridge(object(), range(19876, 19877))
    sock = FakeSocket()
    monkeypatch.setattr(
        bridge,
        "_dispatch",
        lambda _ctx, request: {"ok": True, "cmd": request["cmd"]},
    )
    _accept(qt_bridge, sock)

    state = qt_bridge._connections[sock]
    assert sock.read_buffer_size == bridge.MAX_REQUEST_BYTES
    assert state.timer.single_shot is True

    sock.incoming.extend(b'{"cmd":"ev')
    sock.readyRead.emit()
    assert state.timer.active is True
    assert state.timer.interval == 5000

    sock.incoming.extend(b'al"}\n{]\n{"cmd":"texture"}\n')
    sock.readyRead.emit()

    assert state.timer.active is False
    assert [response.get("error_code") for response in _responses(sock)] == [
        None,
        "invalid_json",
        None,
    ]
    assert _responses(sock)[0]["cmd"] == "eval"
    assert _responses(sock)[2]["cmd"] == "texture"
    assert sock in qt_bridge._connections
    assert sock.disconnect_called is False


def test_qt_oversized_request_reports_error_and_closes():
    qt_bridge = bridge._QtBridge(object(), range(19876, 19877))
    sock = FakeSocket()
    _accept(qt_bridge, sock)
    state = qt_bridge._connections[sock]
    state.framer = bridge.RequestFramer(max_request_bytes=16)

    sock.incoming.extend(b"x" * 16)
    sock.readyRead.emit()

    assert _responses(sock)[0]["error_code"] == "request_too_large"
    assert sock.disconnect_called is True
    assert sock not in qt_bridge._connections
    assert state.timer.active is False


def test_qt_partial_request_timeout_reports_error_and_closes():
    qt_bridge = bridge._QtBridge(object(), range(19876, 19877))
    sock = FakeSocket()
    _accept(qt_bridge, sock)
    state = qt_bridge._connections[sock]

    sock.incoming.extend(b'{"cmd":"eval"')
    sock.readyRead.emit()
    state.timer.timeout.emit()

    assert _responses(sock)[0]["error_code"] == "request_timeout"
    assert sock.disconnect_called is True
    assert sock not in qt_bridge._connections


def test_qt_rejects_connection_above_active_limit():
    qt_bridge = bridge._QtBridge(object(), range(19876, 19877))
    for _index in range(bridge.MAX_ACTIVE_CONNECTIONS):
        _accept(qt_bridge, FakeSocket())
    extra = FakeSocket()

    _accept(qt_bridge, extra)

    assert len(qt_bridge._connections) == bridge.MAX_ACTIVE_CONNECTIONS
    assert _responses(extra)[0]["error_code"] == "server_busy"
    assert extra.disconnect_called is True
    assert extra not in qt_bridge._connections


def test_qt_buffered_eof_attempts_error_and_releases_state():
    qt_bridge = bridge._QtBridge(object(), range(19876, 19877))
    sock = FakeSocket()
    _accept(qt_bridge, sock)

    sock.incoming.extend(b'{"cmd":"eval"')
    sock.readyRead.emit()
    sock.disconnected.emit()

    assert _responses(sock)[0]["error_code"] == "incomplete_request"
    assert sock not in qt_bridge._connections
    assert sock.deleted is True
