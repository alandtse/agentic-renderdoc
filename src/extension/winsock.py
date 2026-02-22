"""Windows socket wrapper using ctypes.

RenderDoc's embedded Python environment does not include the standard
``socket`` module. This provides the minimal socket API needed by the
bridge server, wrapping Winsock2 (ws2_32.dll) via ctypes.

Ported from orb-renderdoc v1. Windows-only, IPv4 loopback only.
"""

import ctypes
from ctypes import wintypes


# --- Winsock DLL ---

ws2_32 = ctypes.windll.ws2_32


# --- Constants ---

AF_INET         = 2
SOCK_STREAM     = 1
IPPROTO_TCP     = 6
SOL_SOCKET      = 0xFFFF
SO_REUSEADDR    = 4
INVALID_SOCKET  = ~0 & 0xFFFFFFFFFFFFFFFF
SOCKET_ERROR    = -1
INADDR_LOOPBACK = 0x7F000001  # 127.0.0.1; needs htonl before use.


# --- Structures ---

class WSADATA(ctypes.Structure):
    """Winsock startup data returned by WSAStartup."""

    _fields_ = [
        ("wVersion",      wintypes.WORD),
        ("wHighVersion",  wintypes.WORD),
        ("iMaxSockets",   ctypes.c_ushort),
        ("iMaxUdpDg",     ctypes.c_ushort),
        ("lpVendorInfo",  ctypes.c_char_p),
        ("szDescription", ctypes.c_char * 257),
        ("szSystemStatus", ctypes.c_char * 129),
    ]


class sockaddr_in(ctypes.Structure):
    """IPv4 socket address (struct sockaddr_in)."""

    _fields_ = [
        ("sin_family", ctypes.c_short),
        ("sin_port",   ctypes.c_ushort),
        ("sin_addr",   ctypes.c_ulong),
        ("sin_zero",   ctypes.c_char * 8),
    ]


# --- Function Signatures ---

ws2_32.WSAStartup.argtypes     = [wintypes.WORD, ctypes.POINTER(WSADATA)]
ws2_32.WSAStartup.restype      = ctypes.c_int

ws2_32.WSACleanup.argtypes     = []
ws2_32.WSACleanup.restype      = ctypes.c_int

ws2_32.WSAGetLastError.argtypes = []
ws2_32.WSAGetLastError.restype  = ctypes.c_int

ws2_32.socket.argtypes         = [ctypes.c_int, ctypes.c_int, ctypes.c_int]
ws2_32.socket.restype          = ctypes.c_uint64

ws2_32.bind.argtypes           = [ctypes.c_uint64, ctypes.POINTER(sockaddr_in), ctypes.c_int]
ws2_32.bind.restype            = ctypes.c_int

ws2_32.listen.argtypes         = [ctypes.c_uint64, ctypes.c_int]
ws2_32.listen.restype          = ctypes.c_int

ws2_32.accept.argtypes         = [ctypes.c_uint64, ctypes.POINTER(sockaddr_in), ctypes.POINTER(ctypes.c_int)]
ws2_32.accept.restype          = ctypes.c_uint64

ws2_32.recv.argtypes           = [ctypes.c_uint64, ctypes.c_char_p, ctypes.c_int, ctypes.c_int]
ws2_32.recv.restype            = ctypes.c_int

ws2_32.send.argtypes           = [ctypes.c_uint64, ctypes.c_char_p, ctypes.c_int, ctypes.c_int]
ws2_32.send.restype            = ctypes.c_int

ws2_32.closesocket.argtypes    = [ctypes.c_uint64]
ws2_32.closesocket.restype     = ctypes.c_int

ws2_32.setsockopt.argtypes     = [ctypes.c_uint64, ctypes.c_int, ctypes.c_int, ctypes.c_char_p, ctypes.c_int]
ws2_32.setsockopt.restype      = ctypes.c_int

ws2_32.htons.argtypes          = [ctypes.c_ushort]
ws2_32.htons.restype           = ctypes.c_ushort

ws2_32.htonl.argtypes          = [ctypes.c_ulong]
ws2_32.htonl.restype           = ctypes.c_ulong


# --- WSA Lifecycle ---

_initialized = False


def wsa_startup():
    """Initialize Winsock. Idempotent -- safe to call multiple times."""
    global _initialized
    if not _initialized:
        wsadata = WSADATA()
        result  = ws2_32.WSAStartup(0x0202, ctypes.byref(wsadata))
        if result != 0:
            raise OSError(f"WSAStartup failed: {result}")
        _initialized = True


def wsa_cleanup():
    """Shut down Winsock. Call once at process exit."""
    global _initialized
    if _initialized:
        ws2_32.WSACleanup()
        _initialized = False


def _last_error():
    """Return the most recent Winsock error code."""
    return ws2_32.WSAGetLastError()


# --- Error ---

class WinsockError(OSError):
    """Winsock operation failure with the underlying error code."""

    def __init__(self, operation: str):
        code = _last_error()
        super().__init__(f"{operation} failed with error {code}")
        self.wsa_error = code


# --- WinsockSocket ---

class WinsockSocket:
    """Minimal TCP socket wrapper over Winsock2.

    Supports the server-side lifecycle: bind, listen, accept, send,
    recv, close. Used as a context manager for automatic cleanup.
    """

    def __init__(self, handle: int = None):
        """Create a new TCP socket, or wrap an existing handle.

        Args:
            handle: Raw Winsock SOCKET handle. When None, a new
                    AF_INET/SOCK_STREAM socket is allocated.
        """
        wsa_startup()

        if handle is not None:
            self._handle = handle
        else:
            self._handle = ws2_32.socket(AF_INET, SOCK_STREAM, IPPROTO_TCP)
            if self._handle == INVALID_SOCKET:
                raise WinsockError("socket")

    def setsockopt_reuse(self):
        """Enable SO_REUSEADDR so the port can be rebound immediately."""
        val    = ctypes.c_int(1)
        result = ws2_32.setsockopt(
            self._handle, SOL_SOCKET, SO_REUSEADDR,
            ctypes.cast(ctypes.byref(val), ctypes.c_char_p),
            ctypes.sizeof(val),
        )
        if result == SOCKET_ERROR:
            raise WinsockError("setsockopt")

    def bind(self, host: str, port: int):
        """Bind to the given address and port.

        Args:
            host: IPv4 address string. Only ``"127.0.0.1"`` is supported.
            port: TCP port number.
        """
        addr            = sockaddr_in()
        addr.sin_family = AF_INET
        addr.sin_port   = ws2_32.htons(port)

        # Only loopback is supported. This runs inside RenderDoc on the
        # local machine -- there is no reason to bind externally.
        if host == "127.0.0.1":
            addr.sin_addr = ws2_32.htonl(INADDR_LOOPBACK)
        else:
            raise ValueError(f"unsupported host: {host}")

        result = ws2_32.bind(self._handle, ctypes.byref(addr), ctypes.sizeof(addr))
        if result == SOCKET_ERROR:
            raise WinsockError("bind")

    def listen(self, backlog: int = 1):
        """Start listening for incoming connections.

        Args:
            backlog: Maximum length of the pending-connections queue.
        """
        result = ws2_32.listen(self._handle, backlog)
        if result == SOCKET_ERROR:
            raise WinsockError("listen")

    def accept(self) -> "WinsockSocket":
        """Accept an incoming connection. Blocks until one arrives.

        Returns:
            A new WinsockSocket wrapping the client connection.
        """
        addr     = sockaddr_in()
        addr_len = ctypes.c_int(ctypes.sizeof(addr))
        client   = ws2_32.accept(
            self._handle, ctypes.byref(addr), ctypes.byref(addr_len),
        )
        if client == INVALID_SOCKET:
            raise WinsockError("accept")
        return WinsockSocket(handle=client)

    def recv(self, bufsize: int) -> bytes:
        """Receive up to ``bufsize`` bytes.

        Returns:
            The received bytes, or ``b""`` if the peer closed the
            connection.
        """
        buf    = ctypes.create_string_buffer(bufsize)
        result = ws2_32.recv(self._handle, buf, bufsize, 0)
        if result == SOCKET_ERROR:
            raise WinsockError("recv")
        if result == 0:
            return b""
        return buf.raw[:result]

    def sendall(self, data: bytes):
        """Send all of ``data``, looping until every byte is written.

        Args:
            data: The bytes to send.
        """
        total = 0
        while total < len(data):
            sent = ws2_32.send(self._handle, data[total:], len(data) - total, 0)
            if sent == SOCKET_ERROR:
                raise WinsockError("send")
            total += sent

    def close(self):
        """Close the socket. Safe to call multiple times."""
        if self._handle and self._handle != INVALID_SOCKET:
            ws2_32.closesocket(self._handle)
            self._handle = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
