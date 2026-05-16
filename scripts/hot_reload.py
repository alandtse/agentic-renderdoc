"""Trigger a live module reload in every running RenderDoc bridge.

Probes the bridge port range, sends {"cmd": "reload"} to each listener,
and prints what each instance reports. The bridge socket stays open and
all connected MCP sessions keep their connections.

Only reloads handlers.py, serialize.py, api_index.py, utilities.py.
Does NOT reload bridge.py or __init__.py — restart RenderDoc for those.

Usage:
    python scripts/hot_reload.py
"""
from __future__ import annotations

import json
import socket
import sys

PORT_RANGE = range(19876, 19886)


def reload_port(port: int, timeout: float = 5.0) -> dict | None:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout) as s:
            s.settimeout(timeout)
            s.sendall(json.dumps({"cmd": "reload", "params": {}}).encode() + b"\n")
            buf = b""
            while b"\n" not in buf:
                chunk = s.recv(65536)
                if not chunk:
                    break
                buf += chunk
            line = buf.split(b"\n", 1)[0]
            return json.loads(line.decode("utf-8")) if line else None
    except (ConnectionRefusedError, OSError):
        return None


def main() -> int:
    hits = 0
    for port in PORT_RANGE:
        resp = reload_port(port)
        if resp is None:
            continue
        hits += 1
        ok = resp.get("ok")
        handlers = resp.get("data", {}).get("handlers") if ok else None
        if ok:
            print(f"[{port}] reloaded — handlers: {sorted(handlers or [])}")
        else:
            print(f"[{port}] error: {resp.get('error')}")
    if hits == 0:
        print("no running RenderDoc bridges found on", list(PORT_RANGE))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
