"""Build script for agentic-renderdoc.

Produces two artifacts in dist/:
    dist/extension/   RenderDoc extension, ready to deploy.
    dist/*.whl        MCP server wheel, pip-installable.

Usage:
    python package.py
"""

import os
import shutil
import subprocess
import sys
import time

ROOT     = os.path.dirname(os.path.abspath(__file__))
DIST     = os.path.join(ROOT, "dist")
EXT_SRC  = os.path.join(ROOT, "src", "extension")
EXT_DEST = os.path.join(DIST, "extension")


def clean():
    """Remove the dist/ directory. Retries on Windows locking errors."""
    if os.path.exists(DIST):
        for attempt in range(3):
            try:
                shutil.rmtree(DIST)
                break
            except OSError:
                if attempt == 2:
                    raise
                time.sleep(0.5)

    os.makedirs(DIST, exist_ok=True)


def build_extension():
    """Copy the extension source into dist/extension/."""
    shutil.copytree(
        EXT_SRC,
        EXT_DEST,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    print(f"Extension -> {EXT_DEST}")


def build_server():
    """Build the MCP server wheel into dist/."""
    subprocess.check_call(
        [sys.executable, "-m", "build", "--wheel", "--no-isolation", "--outdir", DIST],
        cwd=ROOT,
    )
    wheels = [f for f in os.listdir(DIST) if f.endswith(".whl")]
    for w in wheels:
        print(f"Server    -> {os.path.join(DIST, w)}")


def main():
    clean()
    build_extension()
    build_server()
    print("Done.")


if __name__ == "__main__":
    main()
