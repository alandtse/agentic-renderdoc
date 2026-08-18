"""Cross-alias locking discipline for ConnectionPool (Stage 5, Item 3).

Verifies the load-bearing invariant from docs/SYNC_SUPERBRIAN.md Stage 5:
a slow operation on one alias must NOT block a different alias. The pool
lock (``ConnectionPool._lock``) is released before any
``client.send()`` / ``connect()`` / ``disconnect()`` call, so
``ConnectionPool._lock`` and ``RenderDocClient._operation_lock`` are
never held simultaneously by the same thread. Per-connection
serialization therefore never leaks across aliases.
"""

import threading
import time

from server.client import ConnectionPool, RenderDocClient


def _stub_client(port: int) -> RenderDocClient:
    """A RenderDocClient wired for in-process send (no socket I/O)."""
    client = RenderDocClient()
    client._sock = object()
    client._port = port
    return client


def test_slow_send_on_one_alias_does_not_block_a_different_alias():
    """A blocking send on alias 'a' must not stall a send on alias 'b'.

    If the pool held self._lock across client.send(), thread_a's slow
    send would hold the pool lock while waiting on a_release, and
    thread_b's pool.send() would block in _resolve() under the lock.
    The lock is released before client.send(), so thread_b resolves
    'b' and completes on its own client's _operation_lock.
    """
    pool = ConnectionPool()

    client_a = _stub_client(19876)
    a_started = threading.Event()
    a_release = threading.Event()

    def slow_send_a(cmd, params, read_timeout):
        a_started.set()
        assert a_release.wait(timeout=2)
        return {"ok": True, "cmd": cmd, "from": "a"}

    client_a._do_send = slow_send_a

    client_b = _stub_client(19877)
    b_calls = []

    def fast_send_b(cmd, params, read_timeout):
        b_calls.append(cmd)
        return {"ok": True, "cmd": cmd, "from": "b"}

    client_b._do_send = fast_send_b

    pool._connections["a"] = client_a
    pool._connections["b"] = client_b
    pool._default = "a"

    results = {}
    b_done = threading.Event()

    thread_a = threading.Thread(
        target=lambda: results.__setitem__(
            "a", pool.send("eval", {}, alias="a")
        ),
        daemon=True,
    )
    thread_b = threading.Thread(
        target=lambda: (
            results.__setitem__(
                "b", pool.send("instance_info", {}, alias="b")
            ),
            b_done.set(),
        ),
        daemon=True,
    )

    thread_a.start()
    assert a_started.wait(timeout=1), "alias 'a' send never started"

    thread_b.start()
    assert b_done.wait(timeout=1), (
        "send on alias 'b' was blocked by a slow send on alias 'a'"
    )

    a_release.set()
    thread_a.join(timeout=1)
    thread_b.join(timeout=1)

    assert not thread_a.is_alive()
    assert not thread_b.is_alive()
    assert results["a"]["from"] == "a"
    assert results["b"]["from"] == "b"
    assert b_calls == ["instance_info"]


def test_disconnect_on_one_alias_does_not_block_send_on_another():
    """disconnect('a') waiting on client_a's lock must not stall alias 'b'.

    pool.disconnect() pops the registry entry under the lock, then calls
    client.disconnect() OUTSIDE the lock. Here client_a.disconnect()
    blocks on client_a._operation_lock (held by an in-flight send), so
    disconnect is stuck -- but on the client's lock, not the pool lock.
    A concurrent send on alias 'b' must still proceed because the pool
    lock was released before client_a.disconnect() ran.
    """
    pool = ConnectionPool()

    client_a = _stub_client(19876)
    a_send_started = threading.Event()
    a_release = threading.Event()

    def slow_send_a(cmd, params, read_timeout):
        a_send_started.set()
        assert a_release.wait(timeout=2)
        return {"ok": True, "cmd": cmd}

    client_a._do_send = slow_send_a

    client_b = _stub_client(19877)

    def fast_send_b(cmd, params, read_timeout):
        return {"ok": True, "cmd": cmd, "from": "b"}

    client_b._do_send = fast_send_b

    pool._connections["a"] = client_a
    pool._connections["b"] = client_b
    pool._default = "a"

    # An in-flight send on 'a' holds client_a._operation_lock.
    send_a_done = threading.Event()
    thread_send_a = threading.Thread(
        target=lambda: (pool.send("eval", {}, alias="a"), send_a_done.set()),
        daemon=True,
    )
    thread_send_a.start()
    assert a_send_started.wait(timeout=1)

    # disconnect('a') pops under the pool lock, then calls
    # client_a.disconnect() outside it; that blocks on client_a's lock.
    disc_a_done = threading.Event()
    thread_disc_a = threading.Thread(
        target=lambda: (pool.disconnect(alias="a"), disc_a_done.set()),
        daemon=True,
    )
    thread_disc_a.start()
    time.sleep(0.1)
    assert not disc_a_done.is_set(), (
        "disconnect('a') finished while a send still holds client_a's lock"
    )

    # send on 'b' must complete while disconnect('a') is still blocked.
    b_result = pool.send("instance_info", {}, alias="b")
    assert b_result["from"] == "b"

    a_release.set()
    thread_send_a.join(timeout=1)
    thread_disc_a.join(timeout=1)
    assert send_a_done.is_set()
    assert disc_a_done.is_set()
    assert "a" not in pool._connections
    assert "b" in pool._connections