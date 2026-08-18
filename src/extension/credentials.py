"""Per-listener credentials for the local RenderDoc bridge.

The GUI extension imports this module from RenderDoc's embedded Python 3.6,
so it intentionally uses only the standard library and Python 3.6 syntax.
Credential tokens are never included in filenames, representations, or error
messages.
"""

import binascii
import errno
import hmac
import json
import os
import re
import stat
import sys
import tempfile
import time


RUNTIME_DIR_ENV = "AGENTIC_RENDERDOC_RUNTIME_DIR"
SCHEMA_VERSION = 1
TOKEN_BYTES = 32
MAX_CREDENTIAL_BYTES = 16 * 1024

_CREDENTIAL_NAME = re.compile(
    r"^bridge-(?P<port>[0-9]+)-(?P<pid>[0-9]+)-"
    r"(?P<credential_id>[0-9a-f]{32})\.json$"
)


class CredentialError(RuntimeError):
    """A credential could not be safely created, read, or removed."""


class BridgeCredential:
    """Validated credential metadata with a deliberately redacted repr."""

    def __init__(
        self,
        credential_id,
        port,
        pid,
        token,
        instance_id,
        created_at,
        path=None,
    ):
        self.credential_id = credential_id
        self.port = port
        self.pid = pid
        self.token = token
        self.instance_id = instance_id
        self.created_at = created_at
        self.path = path

    def __repr__(self):
        return (
            "BridgeCredential(credential_id={!r}, port={!r}, pid={!r}, "
            "token=<redacted>, instance_id={!r}, created_at={!r})"
        ).format(
            self.credential_id,
            self.port,
            self.pid,
            self.instance_id,
            self.created_at,
        )

    def to_payload(self):
        return {
            "schema_version": SCHEMA_VERSION,
            "credential_id": self.credential_id,
            "port": self.port,
            "pid": self.pid,
            "token": self.token,
            "instance_id": self.instance_id,
            "created_at": self.created_at,
        }


def generate_token():
    """Return a new 256-bit token encoded as lowercase hexadecimal."""
    return binascii.hexlify(os.urandom(TOKEN_BYTES)).decode("ascii")


def _generate_credential_id():
    """Return a public random identifier used only to make filenames unique."""
    return binascii.hexlify(os.urandom(16)).decode("ascii")


def runtime_directory(env=None, platform=None):
    """Resolve the per-user credential directory without creating it."""
    values = os.environ if env is None else env
    current_platform = sys.platform if platform is None else platform
    override = values.get(RUNTIME_DIR_ENV, "")

    if override:
        path = os.path.expanduser(override)
        if not os.path.isabs(path):
            raise CredentialError("credential runtime directory must be absolute")
        path = os.path.abspath(path)
    elif current_platform == "win32":
        local_app_data = values.get("LOCALAPPDATA", "")
        if not local_app_data:
            raise CredentialError("LOCALAPPDATA is required for bridge credentials")
        local_app_data = os.path.expanduser(local_app_data)
        if not os.path.isabs(local_app_data):
            raise CredentialError("LOCALAPPDATA must be absolute")
        path = os.path.join(
            os.path.abspath(local_app_data),
            "agentic-renderdoc",
            "runtime",
        )
    else:
        xdg_runtime = values.get("XDG_RUNTIME_DIR", "")
        if xdg_runtime:
            xdg_runtime = os.path.expanduser(xdg_runtime)
            if not os.path.isabs(xdg_runtime):
                raise CredentialError("XDG_RUNTIME_DIR must be absolute")
            path = os.path.join(
                os.path.abspath(xdg_runtime),
                "agentic-renderdoc",
            )
        else:
            home = os.path.expanduser("~")
            if not os.path.isabs(home):
                raise CredentialError("user home directory must be absolute")
            path = os.path.join(
                home,
                ".local",
                "state",
                "agentic-renderdoc",
                "runtime",
            )
            path = os.path.abspath(path)

    if not os.path.isabs(path):
        raise CredentialError("credential runtime directory must be absolute")
    return path


def ensure_runtime_directory(path=None):
    """Create and permission the runtime directory, rejecting symlink leaves."""
    if path is None:
        directory = runtime_directory()
    else:
        expanded = os.path.expanduser(path)
        if not os.path.isabs(expanded):
            raise CredentialError("credential runtime directory must be absolute")
        directory = os.path.abspath(expanded)

    if os.path.lexists(directory) and os.path.islink(directory):
        raise CredentialError("credential runtime directory cannot be a symlink")

    try:
        os.makedirs(directory, mode=0o700, exist_ok=True)
        if not os.path.isdir(directory):
            raise CredentialError("credential runtime path is not a directory")
        os.chmod(directory, 0o700)
    except CredentialError:
        raise
    except (OSError, IOError) as error:
        raise CredentialError("could not secure credential runtime directory") from error

    return directory


def publish_credential(
    port,
    instance_id=None,
    pid=None,
    runtime_dir=None,
    token=None,
):
    """Atomically publish a new credential and return its redacted object."""
    _validate_port(port)
    actual_pid = os.getpid() if pid is None else pid
    _validate_pid(actual_pid)
    _validate_instance_id(instance_id)

    actual_token = generate_token() if token is None else token
    _validate_token(actual_token)
    credential = BridgeCredential(
        credential_id=_generate_credential_id(),
        port=port,
        pid=actual_pid,
        token=actual_token,
        instance_id=instance_id,
        created_at=time.time(),
    )
    directory = ensure_runtime_directory(runtime_dir)
    filename = "bridge-{}-{}-{}.json".format(
        credential.port,
        credential.pid,
        credential.credential_id,
    )
    destination = os.path.join(directory, filename)
    temporary = None

    try:
        fd, temporary = tempfile.mkstemp(
            prefix=".credential-",
            suffix=".tmp",
            dir=directory,
        )
        try:
            os.chmod(temporary, stat.S_IRUSR | stat.S_IWUSR)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(
                    credential.to_payload(),
                    stream,
                    ensure_ascii=True,
                    separators=(",", ":"),
                    sort_keys=True,
                )
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
        except Exception:
            try:
                os.close(fd)
            except OSError:
                pass
            raise

        os.replace(temporary, destination)
        temporary = None
        credential.path = destination
        return credential
    except Exception as error:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except OSError:
                pass
        raise CredentialError("could not publish bridge credential") from error


def load_credential(path):
    """Load and validate one credential file without exposing its token."""
    try:
        if os.path.islink(path):
            raise CredentialError("credential file cannot be a symlink")
        size = os.path.getsize(path)
        if size <= 0 or size > MAX_CREDENTIAL_BYTES:
            raise CredentialError("credential file has an invalid size")
        with open(path, "r", encoding="utf-8") as stream:
            payload = json.load(stream)
    except CredentialError:
        raise
    except (OSError, IOError, ValueError, TypeError) as error:
        raise CredentialError("credential file is invalid") from error

    if not isinstance(payload, dict):
        raise CredentialError("credential file is invalid")
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise CredentialError("credential schema is unsupported")

    credential_id = payload.get("credential_id")
    port = payload.get("port")
    pid = payload.get("pid")
    token = payload.get("token")
    instance_id = payload.get("instance_id")
    created_at = payload.get("created_at")

    _validate_credential_id(credential_id)
    _validate_port(port)
    _validate_pid(pid)
    _validate_token(token)
    _validate_instance_id(instance_id)
    if not isinstance(created_at, (int, float)) or created_at <= 0:
        raise CredentialError("credential timestamp is invalid")

    expected_name = "bridge-{}-{}-{}.json".format(port, pid, credential_id)
    if os.path.basename(path) != expected_name:
        raise CredentialError("credential filename does not match its contents")

    return BridgeCredential(
        credential_id=credential_id,
        port=port,
        pid=pid,
        token=token,
        instance_id=instance_id,
        created_at=float(created_at),
        path=os.path.abspath(path),
    )


def discover_credentials(runtime_dir=None, process_alive=None):
    """Return live credentials newest-first and remove invalid/dead records."""
    directory = runtime_directory() if runtime_dir is None else os.path.abspath(runtime_dir)
    alive = _process_is_alive if process_alive is None else process_alive

    if not os.path.isdir(directory) or os.path.islink(directory):
        return []

    credentials = []
    try:
        names = os.listdir(directory)
    except OSError:
        return []

    for name in names:
        if _CREDENTIAL_NAME.match(name) is None:
            continue
        path = os.path.join(directory, name)
        try:
            credential = load_credential(path)
        except CredentialError:
            _unlink_quietly(path)
            continue

        if not alive(credential.pid):
            remove_credential(credential)
            continue
        credentials.append(credential)

    credentials.sort(key=lambda item: item.created_at, reverse=True)
    return credentials


def remove_credential(credential):
    """Remove exactly the credential record represented by *credential*."""
    if not isinstance(credential, BridgeCredential) or not credential.path:
        return False

    try:
        current = load_credential(credential.path)
    except CredentialError:
        return False

    if current.credential_id != credential.credential_id:
        return False
    if not hmac.compare_digest(current.token, credential.token):
        return False

    try:
        os.unlink(credential.path)
        return True
    except OSError as error:
        if error.errno == errno.ENOENT:
            return False
        raise CredentialError("could not remove bridge credential") from error


def _process_is_alive(pid):
    if pid == os.getpid():
        return True
    if sys.platform == "win32":
        return _windows_process_is_alive(pid)
    try:
        os.kill(pid, 0)
    except OSError as error:
        return error.errno == errno.EPERM
    return True


def _windows_process_is_alive(pid):
    """Check process state without ``os.kill``, which terminates on Windows."""
    import ctypes
    from ctypes import wintypes

    process_query_limited_information = 0x1000
    still_active = 259
    error_access_denied = 5
    kernel32 = ctypes.windll.kernel32

    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    handle = kernel32.OpenProcess(
        process_query_limited_information,
        False,
        pid,
    )
    if not handle:
        # Access denied means a process exists but cannot be inspected by this
        # account. Preserve its credential rather than treating it as stale.
        return kernel32.GetLastError() == error_access_denied

    try:
        exit_code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return True
        return exit_code.value == still_active
    finally:
        kernel32.CloseHandle(handle)


def _unlink_quietly(path):
    try:
        os.unlink(path)
    except OSError:
        pass


def _validate_credential_id(value):
    if not isinstance(value, str) or re.match(r"^[0-9a-f]{32}$", value) is None:
        raise CredentialError("credential identifier is invalid")


def _validate_port(value):
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 65535:
        raise CredentialError("credential port is invalid")


def _validate_pid(value):
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise CredentialError("credential process identifier is invalid")


def _validate_token(value):
    if not isinstance(value, str) or re.match(r"^[0-9a-f]{64}$", value) is None:
        raise CredentialError("credential token is invalid")


def _validate_instance_id(value):
    if value is not None and (not isinstance(value, str) or len(value) > 256):
        raise CredentialError("credential instance identifier is invalid")
