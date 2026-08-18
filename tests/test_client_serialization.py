"""Concurrency regressions for the per-connection RenderDoc client lock.

Ported from superbrian commit 6fa4c7a (branch
001-mcp2-migration-and-regression-tests, T002). That commit serialized
operations on its single shared RenderDocClient via a
``_serialized_operation`` decorator. This repo keeps the alias-routed
ConnectionPool and instead gives each pooled RenderDocClient its own
``_operation_lock`` (Item 2, docs/SYNC_SUPERBRIAN.md Stage 5) — a
per-connection lock rather than a single global one, so different
aliases never block each other. These cases verify the lock exists on
each client and serializes concurrent operations on THAT client.
"""

import threading
import time

from server.client import RenderDocClient


def _connected_client() -> RenderDocClient:
    client = RenderDocClient()
    client._sock = object()
    client._port = 19876
    return client


def test_send_allows_nested_locked_operations():
    client = _connected_client()
    result = []

    client._do_send = lambda cmd, params, read_timeout: {"ok": True}
    thread = threading.Thread(
        target=lambda: result.append(client.send("instance_info", {})),
        daemon=True,
    )

    thread.start()
    thread.join(timeout=1)

    assert not thread.is_alive(), "send deadlocked while calling ensure_connected"
    assert result == [{"ok": True}]


def test_long_send_blocks_concurrent_operations():
    client = _connected_client()
    entered = []
    entered_lock = threading.Lock()
    eval_started = threading.Event()
    release_eval = threading.Event()
    second_done = threading.Event()
    results = []

    def fake_send(cmd, params, read_timeout):
        with entered_lock:
            entered.append(cmd)
        if cmd == "eval":
            eval_started.set()
            assert release_eval.wait(timeout=2)
        return {"ok": True, "cmd": cmd}

    client._do_send = fake_send
    first = threading.Thread(
        target=lambda: results.append(client.send("eval", {})),
        daemon=True,
    )
    second = threading.Thread(
        target=lambda: (
            results.append(client.send("instance_info", {})),
            second_done.set(),
        ),
        daemon=True,
    )

    first.start()
    assert eval_started.wait(timeout=1)
    second.start()
    time.sleep(0.1)

    assert entered == ["eval"]
    assert not second_done.is_set()

    release_eval.set()
    first.join(timeout=1)
    second.join(timeout=1)

    assert not first.is_alive()
    assert not second.is_alive()
    assert entered == ["eval", "instance_info"]
    assert len(results) == 2