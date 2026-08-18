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


class FakeRenderDocClient:
    """Small stateful replacement for the TCP client used by tool tests."""

    def __init__(self, responses: dict[str, dict]):
        self.responses = deepcopy(responses)
        self.calls: list[tuple[str, dict]] = []
        self.connected_port: int | None = 19876
        self.instances = [{"port": 19876}]
        self.spawned: dict[int, dict] = {}

    @property
    def is_connected(self) -> bool:
        return self.connected_port is not None

    def send(self, cmd: str, params: dict) -> dict:
        self.calls.append((cmd, deepcopy(params)))
        if cmd not in self.responses:
            raise AssertionError(f"unexpected RenderDoc command: {cmd}")
        response = deepcopy(self.responses[cmd])
        if cmd == "instance_info" and self.connected_port is not None:
            response["data"]["port"] = self.connected_port
            if self.connected_port in self.spawned:
                response["data"].update(deepcopy(self.spawned[self.connected_port]))
        return response

    def connect(self, port: int) -> None:
        self.connected_port = port

    def disconnect(self) -> None:
        self.connected_port = None

    def discover_instances(self, enrich: bool = False) -> list[dict]:
        return deepcopy(self.instances)

    def reap_dead_workers(self) -> list[int]:
        return []

    def spawn_headless_worker(self, capture_path: str) -> dict:
        info = {
            **deepcopy(self.responses["instance_info"]["data"]),
            "port": 19877,
            "capture_path": capture_path,
            "headless": True,
            "worker_id": "fake-worker-19877",
        }
        self.spawned[19877] = deepcopy(info)
        self.instances = [{"port": 19877}]
        return {
            "port": 19877,
            "remote_port": 39920,
            "pid": 4242,
            "info": info,
        }

    def close_headless_worker(self, port: int, force: bool = False) -> dict:
        self.spawned.pop(port, None)
        self.instances = [item for item in self.instances if item["port"] != port]
        if self.connected_port == port:
            self.connected_port = None
        return {"closed": True, "port": port, "exit_code": 0, "force": force}


@pytest.fixture
def fake_renderdoc_client(monkeypatch, representative_responses):
    """Replace the tools module's process-global client with a deterministic fake."""
    from server import tools

    client = FakeRenderDocClient(representative_responses)
    monkeypatch.setattr(tools, "_client", client)
    return client
