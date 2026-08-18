"""Credential transport and safe retry tests for RenderDocClient.

Ported from superbrian commit 06a1627 (branch
005-local-bridge-authentication, T003). Three cases there exercised
superbrian's INSTANCE-level ``_credentials_for_port`` / ``_probe_port`` /
``_send_shutdown_to`` on its flattened RenderDocClient. This repo keeps
the alias-routed ConnectionPool and holds those helpers at MODULE level
(see docs/SYNC_SUPERBRIAN.md Stage 5), so those three cases are adapted
to monkeypatch and call the module-level functions instead. The
credential transport, unauthorized-refresh, and no-post-send-retry cases
exercise instance methods and run unchanged.
"""

import json
from types import SimpleNamespace

import server.client as client_module
from server.client import RenderDocClient


AUTH_TOKEN = "a" * 64


def _credential(token=AUTH_TOKEN, credential_id="credential-old"):
    return SimpleNamespace(
        token=token,
        port=19876,
        pid=4242,
        credential_id=credential_id,
    )


class FakeSocket:
    def __init__(self, response=None, connect_error=None):
        self.response = response
        self.connect_error = connect_error
        self.payloads = []
        self.timeouts = []
        self.closed = False

    def settimeout(self, value):
        self.timeouts.append(value)

    def connect(self, address):
        self.address = address
        if self.connect_error is not None:
            raise self.connect_error

    def sendall(self, payload):
        self.payloads.append(bytes(payload))

    def recv(self, _size):
        if self.response is None:
            return b""
        response = self.response
        self.response = None
        return response

    def close(self):
        self.closed = True


def test_do_send_adds_authentication_without_mutating_params():
    response = b'{"ok":true,"data":{"result":2}}\n'
    sock = FakeSocket(response=response)
    credential = _credential()
    client = RenderDocClient()
    client._sock = sock
    client._port = 19876
    client._credential = credential
    params = {"code": "1 + 1"}

    result = client._do_send("eval", params, read_timeout=9.0)

    assert result == {"ok": True, "data": {"result": 2}}
    assert params == {"code": "1 + 1"}
    assert json.loads(sock.payloads[0].decode("utf-8")) == {
        "auth": credential.token,
        "cmd": "eval",
        "params": params,
    }
    assert sock.timeouts == [client_module._WRITE_TIMEOUT, 9.0]


def test_unauthorized_response_refreshes_and_retries_once(monkeypatch):
    client = RenderDocClient()
    client._sock = FakeSocket()
    client._port = 19876
    client._credential = _credential()
    responses = iter([
        {"ok": False, "error": "unauthorized", "error_code": "unauthorized"},
        {"ok": True, "data": {"result": 2}},
    ])
    sends = []
    refreshes = []

    def do_send(cmd, params, read_timeout):
        sends.append((cmd, params, read_timeout))
        return next(responses)

    def refresh(port):
        refreshes.append(port)
        client._credential = _credential("b" * 64, "credential-new")
        return True

    monkeypatch.setattr(client, "_do_send", do_send)
    monkeypatch.setattr(client, "_refresh_credential_connection", refresh)

    result = client.send("eval", {"code": "1 + 1"})

    assert result == {"ok": True, "data": {"result": 2}}
    assert refreshes == [19876]
    assert len(sends) == 2


def test_post_send_connection_failure_is_not_retried(monkeypatch):
    client = RenderDocClient()
    client._sock = FakeSocket()
    client._port = 19876
    client._credential = _credential()
    attempts = []

    def fail_send(cmd, params, read_timeout):
        attempts.append((cmd, params, read_timeout))
        raise ConnectionError("response connection closed")

    monkeypatch.setattr(client, "_do_send", fail_send)
    monkeypatch.setattr(client, "_is_worker_alive", lambda _port: True)

    result = client.send("eval", {"code": "mutate()"})

    assert len(attempts) == 1
    assert result["ok"] is False
    assert result["error"]["kind"] == "worker_dead"
    assert client._sock is None


def test_connect_refreshes_credential_once_before_any_request(monkeypatch):
    old = _credential(credential_id="old")
    new = _credential("b" * 64, credential_id="new")
    discoveries = iter([[old], [old], [new]])
    sockets = [
        FakeSocket(connect_error=ConnectionRefusedError("not ready")),
        FakeSocket(),
    ]
    client = RenderDocClient()
    # Adapted from superbrian: _credentials_for_port is module-level
    # here, not an instance method, so monkeypatch the module function
    # that connect() calls.
    monkeypatch.setattr(
        client_module,
        "_credentials_for_port",
        lambda _port: next(discoveries),
    )
    monkeypatch.setattr(
        client_module.socket,
        "socket",
        lambda *_args: sockets.pop(0),
    )
    monkeypatch.setattr(
        client_module.credentials,
        "remove_credential",
        lambda _credential: True,
    )

    client.connect(19876)

    assert client._credential is new
    assert client.connected_port == 19876
    assert len(sockets) == 0


def test_probe_port_authenticates_instance_info(monkeypatch):
    credential = _credential()
    response = b'{"ok":true,"data":{"worker_id":"worker-1"}}\n'
    sock = FakeSocket(response=response)
    # Adapted from superbrian: _probe_port and _credentials_for_port are
    # module-level here, so monkeypatch and call them at module level.
    monkeypatch.setattr(
        client_module,
        "_credentials_for_port",
        lambda _port: [credential],
    )
    monkeypatch.setattr(client_module.socket, "socket", lambda *_args: sock)

    result = client_module._probe_port(19876, enrich=True)

    assert result == {"worker_id": "worker-1", "port": 19876}
    assert json.loads(sock.payloads[0].decode("utf-8")) == {
        "auth": credential.token,
        "cmd": "instance_info",
        "params": {},
    }
    assert sock.closed is True


def test_worker_shutdown_uses_discovered_credential(monkeypatch):
    credential = _credential()
    sock = FakeSocket(response=b'{"ok":true}\n')
    # Adapted from superbrian: _send_shutdown_to and
    # _credentials_for_port are module-level here.
    monkeypatch.setattr(
        client_module,
        "_credentials_for_port",
        lambda _port: [credential],
    )
    monkeypatch.setattr(client_module.socket, "socket", lambda *_args: sock)

    client_module._send_shutdown_to(19876)

    assert json.loads(sock.payloads[0].decode("utf-8")) == {
        "auth": credential.token,
        "cmd": "shutdown",
        "params": {},
    }
    assert sock.closed is True