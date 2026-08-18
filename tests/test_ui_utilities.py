"""RenderDoc-independent tests for extension UI utility contracts."""

from types import SimpleNamespace

import pytest

from extension import utilities


class FakeTextureViewer:
    def __init__(self, selected_resource=None, error=None):
        self.selected_resource = selected_resource
        self.error = error
        self.calls = []

    def ViewTexture(self, resource_id, type_cast, focus):
        self.calls.append((resource_id, type_cast, focus))
        if self.error is not None:
            raise self.error

    def GetCurrentResource(self):
        return self.selected_resource


class FakeCaptureContext:
    def __init__(self, viewer):
        self.viewer = viewer
        self.get_texture_viewer_calls = 0

    def GetTextureViewer(self):
        self.get_texture_viewer_calls += 1
        return self.viewer


class FakeHandlerContext:
    headless = False

    def __init__(self, viewer):
        self.ctx = FakeCaptureContext(viewer)
        self.ui_calls = 0

    def invoke_ui(self, callback):
        self.ui_calls += 1
        callback()


@pytest.fixture
def typeless(monkeypatch):
    value = object()
    monkeypatch.setattr(
        utilities,
        "rd",
        SimpleNamespace(CompType=SimpleNamespace(Typeless=value)),
    )
    return value


def test_view_texture_selects_and_verifies_requested_resource(typeless):
    resource_id = "ResourceId::23"
    viewer = FakeTextureViewer(selected_resource=resource_id)
    ctx = FakeHandlerContext(viewer)

    result = utilities.make_view_texture(ctx)(resource_id)

    assert result == {"viewing_texture": True}
    assert ctx.ui_calls == 1
    assert ctx.ctx.get_texture_viewer_calls == 1
    assert viewer.calls == [(resource_id, typeless, True)]


def test_view_texture_reports_selected_resource_mismatch(typeless):
    viewer = FakeTextureViewer(selected_resource="ResourceId::99")
    ctx = FakeHandlerContext(viewer)

    result = utilities.make_view_texture(ctx)("ResourceId::23")

    assert result == {
        "ok": False,
        "error": "texture viewer selected a different resource",
        "requested_resource": "ResourceId::23",
        "current_resource": "ResourceId::99",
    }


def test_view_texture_propagates_renderdoc_ui_error(typeless):
    viewer = FakeTextureViewer(error=RuntimeError("viewer failed"))
    ctx = FakeHandlerContext(viewer)

    with pytest.raises(RuntimeError, match="viewer failed"):
        utilities.make_view_texture(ctx)("ResourceId::23")


def test_view_texture_rejects_headless_without_touching_ui():
    class HeadlessContext:
        headless = True

        def invoke_ui(self, callback):
            raise AssertionError("headless view_texture must not touch the UI")

    result = utilities.make_view_texture(HeadlessContext())("ResourceId::23")

    assert result == {
        "ok": False,
        "error": "headless: UI navigation is unavailable in this worker",
    }
