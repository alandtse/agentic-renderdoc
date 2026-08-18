"""Worker lifecycle tests — deliberately skipped in this repo.

The superbrian fork's worker-lifecycle suite (commit 0c60d5c, branch
002-headless-worker-lifecycle) targets its flattened worker
architecture: a single ``RenderDocClient``/``_spawned`` dict with
``spawn_headless_worker`` / ``close_headless_worker`` /
``reap_dead_workers`` / ``_WorkerBindError`` / ``_available_ports`` /
``_spawn_worker_attempt`` / ``_wait_for_worker`` / ``worker_id``.

This repo keeps the alias-routed ``ConnectionPool``
(``open`` / ``close`` / ``reap_dead`` / ``_launch_worker`` /
``_wait_for_bridge``) and explicitly rejected the flattened
architecture (docs/SYNC_SUPERBRIAN.md Stage 5). Porting those cases
would mean reconstructing the rejected API surface, so the suite is
left out of Stage 5 scope rather than rewritten against a shape we do
not ship. The cross-alias locking discipline and per-connection
serialization that Stage 5 *does* port are covered by
``test_client_serialization.py`` and ``test_pool_locking.py``.
"""

import pytest

pytest.skip(
    "branch 002 flattened-worker architecture; out of Stage 5 scope "
    "(docs/SYNC_SUPERBRIAN.md Stage 5)",
    allow_module_level=True,
)