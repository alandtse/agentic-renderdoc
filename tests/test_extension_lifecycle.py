"""RenderDoc-independent tests for UI extension registration lifecycle."""

import sys
from types import SimpleNamespace

import pytest

import extension


@pytest.fixture(autouse=True)
def reset_extension_state():
    extension._extension = None
    extension._extension_ctx = None
    extension._server = None
    yield
    extension._extension = None
    extension._extension_ctx = None
    extension._server = None


def _install_fakes(monkeypatch, events, *, stop_error=None):
    class FakeCaptureViewer:
        pass

    class FakeBridgeServer:
        instances = []

        def __init__(self, handler_ctx):
            self.handler_ctx = handler_ctx
            self.port = None
            self.index = len(self.instances) + 1
            self.instances.append(self)

        def start(self):
            events.append(("server.start", self.index))
            self.port = 19875 + self.index

        def stop(self):
            events.append(("server.stop", self.index))
            if stop_error is not None:
                raise stop_error

    monkeypatch.setitem(
        sys.modules,
        "qrenderdoc",
        SimpleNamespace(CaptureViewer=FakeCaptureViewer),
    )
    monkeypatch.setattr(extension, "BridgeServer", FakeBridgeServer)
    return FakeBridgeServer


class FakeCaptureContext:
    def __init__(self, events, *, remove_error=None):
        self.events = events
        self.remove_error = remove_error
        self.added = []
        self.removed = []

    def AddCaptureViewer(self, viewer):
        self.added.append(viewer)
        self.events.append(("viewer.add", viewer))

    def RemoveCaptureViewer(self, viewer):
        self.removed.append(viewer)
        self.events.append(("viewer.remove", viewer))
        if self.remove_error is not None:
            raise self.remove_error


def test_reregister_and_unregister_teardown_each_viewer_once(monkeypatch):
    events = []
    bridge_type = _install_fakes(monkeypatch, events)
    ctx = FakeCaptureContext(events)

    extension.register("1.45", ctx)
    first_viewer = ctx.added[-1]

    extension.register("1.45", ctx)
    second_viewer = ctx.added[-1]

    extension.unregister()

    assert len(bridge_type.instances) == 2
    assert ctx.removed == [first_viewer, second_viewer]
    assert events == [
        ("viewer.add", first_viewer),
        ("server.start", 1),
        ("server.stop", 1),
        ("viewer.remove", first_viewer),
        ("viewer.add", second_viewer),
        ("server.start", 2),
        ("server.stop", 2),
        ("viewer.remove", second_viewer),
    ]
    assert extension._server is None
    assert extension._extension is None
    assert extension._extension_ctx is None


def test_teardown_continues_and_clears_state_after_cleanup_failures(
    monkeypatch,
    capsys,
):
    events = []
    _install_fakes(monkeypatch, events, stop_error=RuntimeError("stop failed"))
    ctx = FakeCaptureContext(events, remove_error=RuntimeError("remove failed"))

    extension.register("1.45", ctx)
    viewer = ctx.added[-1]

    extension.unregister()

    assert events[-2:] == [
        ("server.stop", 1),
        ("viewer.remove", viewer),
    ]
    assert extension._server is None
    assert extension._extension is None
    assert extension._extension_ctx is None

    output = capsys.readouterr().out
    assert "Bridge stop failed: stop failed" in output
    assert "CaptureViewer removal failed: remove failed" in output
