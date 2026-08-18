"""Platform-independent tests for extension socket receive timeouts."""

import sys

import pytest

from extension import winsock


class _StdlibSocket:
    def __init__(self):
        self.timeout = "unset"

    def settimeout(self, seconds):
        self.timeout = seconds


@pytest.mark.skipif(sys.platform == "win32", reason="stdlib path is non-Windows")
def test_stdlib_socket_delegates_receive_timeout():
    raw_socket = _StdlibSocket()
    sock = winsock.Socket(handle=raw_socket)

    sock.settimeout(5.0)
    assert raw_socket.timeout == 5.0
    sock.settimeout(None)
    assert raw_socket.timeout is None


@pytest.mark.skipif(sys.platform == "win32", reason="stdlib path is non-Windows")
def test_stdlib_timeout_error_is_identified():
    import socket

    assert winsock.is_timeout_error(socket.timeout())
    assert not winsock.is_timeout_error(OSError("other failure"))


@pytest.mark.skipif(sys.platform != "win32", reason="Winsock path is Windows-only")
def test_windows_socket_sets_receive_timeout_in_milliseconds(monkeypatch):
    captured = {}

    def fake_setsockopt(handle, level, option, value_pointer, value_size):
        captured.update(
            handle=handle,
            level=level,
            option=option,
            milliseconds=winsock.ctypes.cast(
                value_pointer,
                winsock.ctypes.POINTER(winsock.wintypes.DWORD),
            ).contents.value,
            value_size=value_size,
        )
        return 0

    monkeypatch.setattr(winsock.ws2_32, "setsockopt", fake_setsockopt)
    sock = winsock.Socket.__new__(winsock.Socket)
    sock._handle = 123

    sock.settimeout(5.0)

    assert captured == {
        "handle": 123,
        "level": winsock.SOL_SOCKET,
        "option": winsock.SO_RCVTIMEO,
        "milliseconds": 5000,
        "value_size": winsock.ctypes.sizeof(winsock.wintypes.DWORD),
    }


@pytest.mark.skipif(sys.platform != "win32", reason="Winsock path is Windows-only")
def test_windows_socket_restores_blocking_timeout(monkeypatch):
    values = []

    def fake_setsockopt(_handle, _level, _option, pointer, _size):
        value = winsock.ctypes.cast(
            pointer,
            winsock.ctypes.POINTER(winsock.wintypes.DWORD),
        ).contents.value
        values.append(value)
        return 0

    monkeypatch.setattr(winsock.ws2_32, "setsockopt", fake_setsockopt)
    sock = winsock.Socket.__new__(winsock.Socket)
    sock._handle = 123

    sock.settimeout(None)

    assert values == [0]


@pytest.mark.skipif(sys.platform != "win32", reason="Winsock path is Windows-only")
def test_windows_timeout_error_is_identified():
    timed_out = OSError("timed out")
    timed_out.wsa_error = winsock.WSAETIMEDOUT
    other = OSError("other failure")
    other.wsa_error = 10054

    assert winsock.is_timeout_error(timed_out)
    assert not winsock.is_timeout_error(other)
    assert not winsock.is_timeout_error(OSError("no winsock code"))
