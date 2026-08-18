# Syncing infrastructure from SuperBrianLee/agentic-renderdoc

Staged plan for selectively adopting hardening work from a more actively
maintained third-party fork, `SuperBrianLee/agentic-renderdoc` (remote
`superbrian`, branch `superbrian/master`, last pushed 2026-08-11). Written
2026-08-18 after a read-only investigation + a bounded-debate review of the
merge strategy. Each stage below is meant to land as its own commit, verified
before moving to the next — do not batch stages together.

## Background: why these forks diverged the way they did

Both forks branched from upstream `EdenLabs/agentic-renderdoc` at the same
commit (`d62de7a`). From there:

- **Our `dev`** built the actual product functionality this project exists
  for: `targets_list`/`trigger_capture` (capturing a running game),
  `match`/`wait_secs` polling, `Instance(action="launch")` (launching a game
  under RenderDoc), `describe_resource` utilities, concept docs. 32 commits
  superbrian lacks.
- **`superbrian/master`** never touched any of that (confirmed: zero
  mentions of `ExecuteAndInject`/`targets_list`/`trigger_capture` in their
  `handlers.py`/`tools.py`) and instead built infrastructure hardening: MCP
  SDK v2 migration, worker lifecycle stability, bridge protocol resource
  boundaries, local bridge authentication, RenderDoc 1.45 UI extension
  lifecycle fixes, and a real pytest suite (20+ files) — we currently have
  zero automated tests. 26 commits we lack.

These are **orthogonal codebases, not competing rewrites** — confirmed via
line counts at the merge-base: superbrian's `tools.py` is byte-identical in
size to `d62de7a` (921 lines); the huge `dev`-vs-`superbrian` diffstat is
entirely our own feature growth, not their deletions.

## The one piece to reject: their `client.py`

Their `RenderDocClient` flattens our `ConnectionPool` (alias-routed,
multi-instance — `client.py:578`, 55 `alias:` references across
`tools.py`) into a single-port connection. This is a real architectural
fork, not a refactor. **Do not adopt it.** `ConnectionPool` is load-bearing
for us: `targets_list`, `trigger_capture`, `match`/`wait_secs` polling,
`launch`, and `find_first_divergence` (which explicitly holds two live
connections at once, `instance_a`/`instance_b`) all depend on addressing
multiple simultaneous instances. Their flattened client can't do that.

Where their `client.py` branch has real value (auth, operation-locking), we
port the *behavior* onto our `ConnectionPool`, not their file onto our repo.

## Clearing up an apparent contradiction: MCP2 vs. their new auth work

These solve unrelated problems at different layers, despite both sounding
"MCP2-adjacent":

- **MCP2's statelessness** applies to the Claude ↔ MCP-server-subprocess
  link, which is **stdio** (a spawned child process), not a network
  connection — there was never any auth to remove there.
- **Their bridge auth** (branch 005) protects the *separate* hop:
  `MCP server ↔ RenderDoc extension`, a real **TCP socket on `localhost`,
  port range 19876-19885** (see main `README.md`). `Eval` executes arbitrary
  Python inside the RenderDoc GUI process. Today, unauthenticated, *any*
  other local process on the machine — any user — can connect to that port
  and get arbitrary code execution inside `qrenderdoc.exe`. Their
  per-OS-user token (`credentials.py`, stored in `LOCALAPPDATA`/
  `XDG_RUNTIME_DIR`) closes that hole. Unrelated to MCP2, predates it.

## Staged plan

Each stage lists: what, why, source commit(s), files touched, adoption
approach, and the test(s) to port/write before calling the stage done. Build
+ run the relevant tests after each stage before starting the next.

### Stage 0 — baseline
Add `pytest` (and whatever `conftest.py` needs) to dev dependencies so
`tests/` is runnable at all. Confirm `python -m pytest` runs (even with zero
tests) before stage 1.

### Stage 1 — extension re-registration teardown fix
**What**: guard `register()` in `src/extension/__init__.py` against being
called again while a prior registration is still live (silently overwrote
`_extension`/`_server`/`ctx` with no cleanup today — leaks the bridge TCP
socket, leaves a stale `CaptureViewer` registered forever). Real risk given
our own `reload` handler exists specifically to re-trigger registration.
**Source**: `4df93e7` (also covers 003/T001 — same fix, not two separate
things).
**Adopt**: apply by hand (branch diff, not a cherry-pickable commit on our
history) — add `_teardown()`, guard `register()`, try/except-with-cleanup
around registration, using the `ctx.AddCaptureViewer`/`RemoveCaptureViewer`
pair our code already calls.
**Test**: port `tests/test_extension_lifecycle.py`.
**Verify**: re-run the manual repro from earlier today (rapid `reload` calls
via Eval) and confirm no leaked bridge socket / stale `CaptureViewer`.

### Stage 2 — Texture Viewer selection API fix
**What**: `make_view_texture()` in `src/extension/utilities.py` uses a
legacy, unreliable fallback (`pyrenderdoc.ViewTextureDisplay(...)` /
`ShowTextureViewer()`) instead of the current API
(`pyrenderdoc.GetTextureViewer().ViewTexture(resource_id, CompType.Typeless,
True)`), and doesn't verify the requested resource actually got selected.
Confirmed: this is the *same* `GetTextureViewer().ViewTexture(...)` call
pattern used successfully in today's RenderDoc-fork repro work — the old
fallback is unreliable on 1.45/1.46.
**Source**: `d125861` (003/T002).
**Adopt**: port the fixed `make_view_texture()` body, including the
post-call verification (`viewer.GetCurrentResource()` check, structured
mismatch error).
**Test**: port `tests/test_ui_utilities.py`.

### Stage 3 — bridge protocol resource boundaries
**What**: our `bridge.py`/`winsock.py` currently have **zero** request-size
limits, connection timeouts, or concurrent-connection caps (confirmed:
`grep -n "settimeout\|MAX_.*SIZE\|RequestFramer"` → no matches on `dev`).
Fully exposed to an unbounded request, a peer that never sends/reads (hangs
`recv()` forever), or unlimited concurrent connections. Divergence from
merge-base is small on our side (`bridge.py` +15/-... lines, `winsock.py`
+21 lines vs. merge-base) — low conflict risk, this is the opposite
situation from `client.py`.
**Source** (4 commits, `superbrian/master`):
- `3a3abfe` — `RequestFramer`: 8 MiB request cap, structured error
  responses, handles split/multi-request and EOF boundaries.
- `982f932` — `settimeout` (5s) on both the stdlib and ctypes-Winsock
  receive paths.
- `8eb4ae1` — applies the framer + 5s idle timer to Qt-backend connections,
  caps read buffer size and max active connections.
- `a01ccdd` — same for the threaded backend (5s timeout, max 4 concurrent
  connections).
**Adopt**: port all 4 as one stage (they're a single cohesive hardening
pass on the same two files) directly onto our `bridge.py`/`winsock.py`.
**Test**: port `tests/test_bridge_protocol.py`,
`tests/test_winsock_timeout.py`, `tests/test_qt_bridge.py`,
`tests/test_threaded_bridge.py`.

### Stage 4 — mechanical MCP2 migration
**What**: bump `mcp>=2,<3` (official MCP Python SDK v2.0.0, 2026-07-28,
stateless protocol spec revision). Migration itself is 3 lines: `app.py`
swaps `from mcp.server.fastmcp import FastMCP` → `from mcp.server.mcpserver
import MCPServer` (+`version=` kwarg); `tools.py` swaps the `Image` import
path. v2 servers stay backward-compatible with old stateful clients per the
SDK's own guarantee — no urgency, but cheap and unblocks the MCP2-coupled
tests below.
**Source**: relevant slice of `001-mcp2-migration-and-regression-tests`
(`8465488`/`81d0b75` for the actual import-swap commits — confirm exact SHAs
against `git log superbrian/master -- src/server/app.py` before starting,
since that branch also bundles unrelated client-state-serialization work we
are explicitly NOT adopting, see rejected-`client.py` section above).
**Test**: port `tests/test_mcp_protocol.py` (confirm it only needs the SDK
swap, not their flattened client, before porting — if it imports
`RenderDocClient` internals, defer the affected assertions to stage 5).

### Stage 5 — auth + operation-locking, ported onto `ConnectionPool`
**What**: port superbrian's local-bridge auth (`credentials.py`, per-OS-user
token) and operation-lock serialization (prevents two concurrent operations
racing the same connection) **onto our existing `ConnectionPool`**, not by
replacing it with their flattened client. This is a design task, not a
file copy — expect real work per the earlier debate's finding.
**Source**: branch `005-local-bridge-authentication` (`935b560`, `06a1627`,
`2a40236`, `a719a2c`) for auth; the locking piece of
`001-mcp2-migration-and-regression-tests` (`6fa4c7a`, "RenderDocClient
shared-state serialization") for the lock semantics to adapt.
**Adopt**: 
1. Port `credentials.py` as-is (self-contained, stdlib-only, already
   written for RenderDoc's embedded Python 3.6).
2. Wire credential issuance/validation into our `bridge.py` (stage 3 must
   land first — same file).
3. Add per-connection (not per-client-flattened) operation locking to
   `ConnectionPool`, adapting their `_serialized_operation` semantics to a
   pool of connections rather than a single one.
**Test**: adapt `conftest.py`'s `FakeRenderDocClient` fixture to mock the
pool interface (it currently exposes `spawn_headless_worker`/
`close_headless_worker`/`reap_dead_workers`, matching their flattened
client — needs re-pointing at `ConnectionPool`'s actual method names before
the ported tests can pass). Then port `tests/test_bridge_auth.py`,
`tests/test_client_auth.py`, `tests/test_client_serialization.py`,
`tests/test_worker_lifecycle.py` (`_WorkerBindError` and friends need
equivalents added to our `client.py`, or the fixture needs adjusting — check
which on landing).

### Stage 6 — standalone test/infra files (can land any time after stage 0)
Not coupled to anything else: `tests/test_probe.py` (also 003/T003 —
probe-discovery hardening to skip unrelated listeners, `scripts/probe.py`),
`tests/test_setup_cli.py`, `tests/test_packaging_contract.py`,
`tests/test_release_check.py`. Low priority, low cost — slot in wherever
convenient, no ordering dependency on stages 1-5.

### Stage 7 — backfill tests for our unique features
Write new tests (not ported — superbrian never built these features) for
`targets_list`/`trigger_capture`, `match`/`wait_secs` polling, and
`Instance(action="launch")` (added 2026-08-18). This is the payoff stage:
once stages 0-6 land, the test suite covers both sides of the codebase.

## Explicitly deferred / not planned

- **006 (RenderDoc 1.45 release validation)**: smoke-test docs/scripts
  specific to validating against 1.45. Useful methodology, not urgent —
  revisit if/when we do our own release validation pass.
- **Their `RenderDocClient` (flattened single-connection client)**:
  rejected, see above. Not a future stage — the intent is to keep
  `ConnectionPool` permanently, porting behavior onto it rather than ever
  adopting their file wholesale.

## Verification discipline

After each stage: run the newly-ported/written tests for that stage, run
the full suite to confirm no regression in earlier stages, and do a manual
smoke check against a live RenderDoc instance for anything that touches the
bridge or extension registration (stages 1, 3, 5) before moving on — those
are exactly the paths that fail silently (leaked sockets, hung
connections) rather than throwing.
