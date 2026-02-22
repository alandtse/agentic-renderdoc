"""Windows socket wrapper using ctypes.

RenderDoc's embedded Python environment does not include the standard
`socket` module. This provides the minimal socket API needed by the
bridge server, wrapping Winsock2 (ws2_32.dll) via ctypes.

Ported from orb-renderdoc v1. Windows-only, IPv4 loopback only.
"""

# TODO: Port from orb-renderdoc v1's winsock.py.
