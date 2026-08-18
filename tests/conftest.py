"""Shared fixtures for RenderDoc-independent MCP tests."""

import base64
from copy import deepcopy

import pytest


@pytest.fixture
def anyio_backend():
    """Run MCP's async tests on the asyncio backend used by the server."""
    return "asyncio"


@pytest.fixture
def representative_responses() -> dict[str, dict]:
    """Return deterministic bridge responses covering every public tool."""
    return {
        "eval": {
            "ok": True,
            "data": {"result": 42},
        },
        "api_index": {
            "ok": True,
            "data": [
                {
                    "name": "ReplayController.SetFrameEvent",
                    "kind": "method",
                    "signature": "(eventId, force)",
                }
            ],
        },
        "get_texture": {
            "ok": True,
            "data": {
                "raw": base64.b64encode(
                    bytes((255, 0, 0, 255, 0, 255, 0, 255))
                ).decode("ascii"),
                "format": {
                    "name": "R8G8B8A8_UNORM",
                    "component_type": "UNorm",
                    "component_count": 4,
                    "component_byte_width": 1,
                },
                "width": 2,
                "height": 1,
                "mip_width": 2,
                "mip_height": 1,
                "resource_id": "ResourceId::1",
            },
        },
        "instance_info": {
            "ok": True,
            "data": {
                "port": 19876,
                "capture_loaded": True,
                "capture_path": "sample.rdc",
                "api": "Vulkan",
                "event_count": 12,
                "headless": False,
            },
        },
    }


class FakeConnectionPool:
    """Deterministic stand-in for ``ConnectionPool`` used by tool tests.

    Stage 5 (docs/SYNC_SUPERBRIAN.md) re-pointed this fixture from
    superbrian's flattened ``RenderDocClient`` (``spawn_headless_worker`` /
    ``close_headless_worker`` / ``reap_dead_workers``) to the
    alias-routed ``ConnectionPool`` interface that ``server.tools`` actually
    drives via ``_pool``. The fake mirrors the pool's public surface
    (``send`` / ``connect`` / ``disconnect`` / ``close`` / ``open`` /
    ``aliases`` / ``default_alias`` / ``connection_info`` /
    ``ensure_connected`` / ``reap_dead`` / ``discover_instances`` /
    ``set_default``) so tool tests run without a live bridge.

    It is pre-seeded with one connection on port 19876 so the Eval /
    Search-API / Get-Texture tools skip ``ensure_connected`` and route
    straight to ``send`` — matching how a real pool with an active
    connection behaves.
    """

    def __init__(self, responses: dict[str, dict]):
        self.responses = deepcopy(responses)
        self.calls: list[tuple[str, dict]] = []
        self._aliases: dict[str, dict] = {}
        self._default: str | None = None
        self.instances: list[dict] = [{"port": 19876}]
        self.spawned: dict[int, dict] = {}
        # Pre-seed one connection so tools skip ensure_connected().
        info = deepcopy(self.responses["instance_info"]["data"])
        self._aliases["instance_19876"] = info
        self._default = "instance_19876"

    @property
    def aliases(self) -> list[str]:
        return list(self._aliases.keys())

    @property
    def default_alias(self) -> str | None:
        if self._default is not None:
            return self._default
        if len(self._aliases) == 1:
            return next(iter(self._aliases))
        return None

    def send(self, cmd: str, params: dict,
             alias: str | None = None,
             read_timeout: float | None = None) -> dict:
        self.calls.append((cmd, deepcopy(params)))
        if cmd not in self.responses:
            raise AssertionError(f"unexpected RenderDoc command: {cmd}")
        return deepcopy(self.responses[cmd])

    def connect(self, port: int, alias: str | None = None) -> dict:
        # Mirror the real pool: connect() fetches instance_info first.
        info = deepcopy(self.send("instance_info", {})["data"])
        info["port"] = port
        if alias is None:
            alias = f"instance_{port}"
        self._aliases[alias] = info
        if self._default is None:
            self._default = alias
        result = dict(info)
        result["alias"] = alias
        result["port"] = port
        return result

    def connection_info(self) -> list[dict]:
        result = []
        for alias, info in self._aliases.items():
            entry = {
                "alias": alias,
                "port": info.get("port"),
                "headless": False,
            }
            if info:
                entry["info"] = deepcopy(info)
            result.append(entry)
        return result

    def disconnect(self, alias: str | None = None) -> str:
        target = alias or self.default_alias
        self._aliases.pop(target, None)
        if self._default == target:
            self._default = None
        return target

    def close(self, alias: str | None = None, force: bool = False) -> dict:
        target = alias or self.default_alias
        self._aliases.pop(target, None)
        if self._default == target:
            self._default = None
        return {
            "closed": True,
            "alias": target,
            "port": None,
            "exit_code": 0,
            "force": force,
        }

    def set_default(self, alias: str) -> None:
        if alias not in self._aliases:
            raise KeyError(f"no connection named {alias!r}")
        self._default = alias

    def ensure_connected(self) -> dict:
        if self._aliases:
            alias = self.default_alias
            return {
                "alias": alias,
                "port": self._aliases[alias].get("port"),
                "info": deepcopy(self._aliases[alias]),
            }
        port = self.instances[0]["port"]
        result = self.connect(port)
        return {"alias": result["alias"], "port": port, "info": result}

    def reap_dead(self) -> list[str]:
        return []

    def discover_instances(self, enrich: bool = False) -> list[dict]:
        return deepcopy(self.instances)

    def open(self, file: str, alias: str | None = None,
             bind_wait: float | None = None) -> dict:
        info = deepcopy(self.responses["instance_info"]["data"])
        info["port"] = 19877
        info["capture_path"] = file
        info["headless"] = True
        info["worker_id"] = "fake-worker-19877"
        if alias is None:
            alias = "instance_19877"
        self._aliases[alias] = info
        self._default = alias
        self.spawned[19877] = deepcopy(info)
        return {
            "alias": alias,
            "port": 19877,
            "remote_port": 39920,
            "pid": 4242,
            "info": deepcopy(info),
        }


@pytest.fixture
def fake_renderdoc_client(monkeypatch, representative_responses):
    """Replace the tools module's process-global pool with a deterministic fake."""
    from server import tools

    pool = FakeConnectionPool(representative_responses)
    monkeypatch.setattr(tools, "_pool", pool)
    return pool