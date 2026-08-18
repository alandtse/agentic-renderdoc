"""Tests for bridge authentication and credential publication lifecycle."""

from types import SimpleNamespace

import pytest

from extension import bridge


AUTH_TOKEN = "a" * 64


class FakeBackend:
    def __init__(self, events, port=19876):
        self.events = events
        self.port = port
        self.auth_token = None

    def set_auth_token(self, token):
        self.auth_token = token
        self.events.append(("auth", token))

    def start(self):
        self.events.append(("start", self.auth_token))

    def stop(self):
        self.events.append(("stop", self.auth_token))


def test_request_authentication_uses_constant_time_comparison(monkeypatch):
    calls = []

    def compare_digest(supplied, expected):
        calls.append((supplied, expected))
        return supplied == expected

    monkeypatch.setattr(bridge.hmac, "compare_digest", compare_digest)

    assert bridge._request_is_authorized({"auth": AUTH_TOKEN}, AUTH_TOKEN)
    assert calls == [(AUTH_TOKEN, AUTH_TOKEN)]
    assert not bridge._request_is_authorized({}, AUTH_TOKEN)
    assert calls == [(AUTH_TOKEN, AUTH_TOKEN)]


def test_bridge_publishes_and_removes_listener_credential(monkeypatch):
    events = []
    backend = FakeBackend(events)
    credential = object()
    published = []
    removed = []
    monkeypatch.setattr(bridge, "_ThreadedBridge", lambda *_args: backend)
    monkeypatch.setattr(bridge.credentials, "generate_token", lambda: AUTH_TOKEN)

    def publish_credential(**kwargs):
        published.append(kwargs)
        events.append(("publish", kwargs["token"]))
        return credential

    monkeypatch.setattr(
        bridge.credentials,
        "publish_credential",
        publish_credential,
    )
    monkeypatch.setattr(
        bridge.credentials,
        "remove_credential",
        lambda value: removed.append(value),
    )
    ctx = SimpleNamespace(_worker_id="worker-test")
    server = bridge.BridgeServer(ctx, force_threaded=True)

    server.start()
    server.start()

    assert published == [{
        "port": 19876,
        "instance_id": "worker-test",
        "token": AUTH_TOKEN,
    }]
    assert events[:3] == [
        ("auth", AUTH_TOKEN),
        ("start", AUTH_TOKEN),
        ("publish", AUTH_TOKEN),
    ]

    server.stop()

    assert removed == [credential]
    assert backend.auth_token is None
    assert events[-2:] == [("stop", AUTH_TOKEN), ("auth", None)]


def test_bridge_stops_if_credential_publication_fails(monkeypatch):
    events = []
    backend = FakeBackend(events)
    monkeypatch.setattr(bridge, "_ThreadedBridge", lambda *_args: backend)
    monkeypatch.setattr(bridge.credentials, "generate_token", lambda: AUTH_TOKEN)

    def fail_publish(**_kwargs):
        raise bridge.credentials.CredentialError("publication failed")

    monkeypatch.setattr(bridge.credentials, "publish_credential", fail_publish)
    server = bridge.BridgeServer(object(), force_threaded=True)

    with pytest.raises(bridge.credentials.CredentialError):
        server.start()

    assert events == [
        ("auth", AUTH_TOKEN),
        ("start", AUTH_TOKEN),
        ("stop", AUTH_TOKEN),
        ("auth", None),
    ]
    assert backend.auth_token is None
    assert server._credential is None


def test_bridge_does_not_publish_when_backend_did_not_bind(monkeypatch):
    events = []
    backend = FakeBackend(events, port=None)
    monkeypatch.setattr(bridge, "_ThreadedBridge", lambda *_args: backend)
    monkeypatch.setattr(bridge.credentials, "generate_token", lambda: AUTH_TOKEN)
    published = []
    monkeypatch.setattr(
        bridge.credentials,
        "publish_credential",
        lambda **kwargs: published.append(kwargs),
    )
    server = bridge.BridgeServer(object(), force_threaded=True)

    server.start()

    assert published == []
    assert backend.auth_token is None
