"""Sync the local dev branch with upstream/master.

Preserves our fork-local commits as a linear stack on top of upstream.
``dev`` is the canonical local branch; ``upstream`` points at
EdenLabs/agentic-renderdoc. Our commits stay in a defensible order
(compat fix → infra → features → safety) so each one is reviewable
in isolation when we PR back.

Usage:
    python scripts/sync_upstream.py             # rebase (default)
    python scripts/sync_upstream.py --merge     # merge instead

Run from a clean working tree on dev.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import subprocess
import sys


def _run(cmd: list[str], check: bool = True, capture: bool = True) -> subprocess.CompletedProcess:
    """Run a git command. By default captures output and raises on failure."""
    return subprocess.run(
        cmd,
        check          = check,
        text           = True,
        capture_output = capture,
    )


def _porcelain_clean() -> bool:
    return _run(["git", "status", "--porcelain"]).stdout.strip() == ""


def _current_branch() -> str:
    return _run(["git", "rev-parse", "--abbrev-ref", "HEAD"]).stdout.strip()


def _commits_ahead(base: str, head: str = "HEAD") -> int:
    out = _run(["git", "rev-list", "--count", f"{head}..{base}"]).stdout.strip()
    return int(out or "0")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--merge", action="store_true",
                        help="Use --no-ff merge instead of rebase.")
    args = parser.parse_args(argv[1:])

    # Sanity checks.
    if not _porcelain_clean():
        print("working tree dirty — commit or stash first", file=sys.stderr)
        return 1

    branch = _current_branch()
    if branch != "dev":
        print(f"not on dev (currently {branch})", file=sys.stderr)
        return 1

    print("fetching upstream…")
    _run(["git", "fetch", "upstream"], capture=False)

    ahead = _commits_ahead("upstream/master")
    if ahead == 0:
        print("already up to date with upstream/master.")
        return 0

    print(f"{ahead} new upstream commit(s):")
    _run(["git", "log", "--oneline", "HEAD..upstream/master"], capture=False)

    # Safety tag in case the operation goes sideways.
    backup = "dev-pre-sync-" + _dt.datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    _run(["git", "branch", backup, "dev"])
    print(f"safety branch created: {backup}")

    if args.merge:
        print("merging upstream/master into dev…")
        r = _run(["git", "merge", "--no-ff", "upstream/master",
                  "-m", "chore: sync with upstream/master"],
                 check=False, capture=False)
        if r.returncode != 0:
            print()
            print("merge hit a conflict. resolve and commit, or "
                  "'git merge --abort'.")
            print(f"backup at {backup}.")
            return r.returncode
    else:
        print("rebasing dev onto upstream/master…")
        r = _run(["git", "rebase", "upstream/master"],
                 check=False, capture=False)
        if r.returncode != 0:
            print()
            print("rebase hit a conflict. resolve and "
                  "'git rebase --continue', or 'git rebase --abort'.")
            print(f"backup at {backup}.")
            return r.returncode

    print()
    print("done. our commits on top of upstream/master:")
    _run(["git", "log", "--oneline", "upstream/master..HEAD"], capture=False)
    print()
    print("if everything looks good, drop the backup with:")
    print(f"    git branch -D {backup}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
