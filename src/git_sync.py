"""Commit and push vault changes.

Ratings arrive over Telegram at unpredictable times, so each one is committed
individually. `git push` is best-effort: a failed push must never lose the
user's rating, which is already durable on disk by the time we get here.

Callers pass the **vault directory** as `repo`, never the code checkout. Git
resolves the enclosing repository from there, which makes both supported
layouts work with no branching:

    single repo   vault/ lives inside the code repo  -> resolves to that repo
    split repos   vault/ is a private repo checked   -> resolves to the vault
                  out inside a public code repo         repo
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)

TIMEOUT_SECONDS = 60
BOT_NAME = "DeepRecall Bot"
BOT_EMAIL = "deeprecall@users.noreply.github.com"


def _run(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args, cwd=cwd, capture_output=True, text=True, timeout=TIMEOUT_SECONDS, check=False
    )


def is_repo(path: Path) -> bool:
    try:
        result = _run(["git", "rev-parse", "--is-inside-work-tree"], path)
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0 and result.stdout.strip() == "true"


def _ensure_identity(repo: Path) -> None:
    """CI runners and fresh clones have no committer identity configured."""
    if _run(["git", "config", "user.email"], repo).stdout.strip():
        return
    _run(["git", "config", "user.email", BOT_EMAIL], repo)
    _run(["git", "config", "user.name", BOT_NAME], repo)


def commit_paths(
    paths: list[Path],
    message: str,
    repo: Path,
    push: bool = True,
    removals: list[Path] | None = None,
) -> bool:
    """Stage, commit and (optionally) push `paths`. Returns True if a commit was made.

    `removals` are paths deleted on purpose — a regenerated note that no longer
    has a third constraint modifier, say. They are staged with `git add -A`,
    which records a deletion where a plain `git add` on a vanished path would
    abort.

    `repo` should be the vault directory; git resolves the repository from it.
    """
    paths = [p for p in paths if p]
    removals = [p for p in (removals or []) if p]
    if not paths and not removals:
        return False

    if not repo.exists() or not is_repo(repo):
        log.info("Not a git repository (%s); skipping commit. Changes are saved on disk.", repo)
        return False

    _ensure_identity(repo)

    # `git add` is all-or-nothing: one missing pathspec aborts the whole
    # command and stages nothing, so a single vanished file would silently
    # leave an entire sync uncommitted. Drop missing paths first.
    present = [p for p in paths if p.exists()]
    for missing in [p for p in paths if not p.exists()]:
        log.warning("Expected to commit %s but it is not on disk; skipping it.", missing)

    if not present and not removals:
        log.error("None of the %d path(s) to commit exist on disk.", len(paths))
        return False

    if present:
        add = _run(["git", "add", "--", *[str(p) for p in present]], repo)
        if add.returncode != 0:
            log.error("git add failed: %s", add.stderr.strip())
            return False

    for gone in removals:
        # Tolerated individually: a path git never tracked makes `add` fail, and
        # that must not cost us the rest of the commit.
        staged_removal = _run(["git", "add", "-A", "--", str(gone)], repo)
        if staged_removal.returncode != 0:
            log.warning("Could not stage deletion of %s: %s", gone, staged_removal.stderr.strip())

    staged = _run(["git", "diff", "--cached", "--quiet"], repo)
    if staged.returncode == 0:
        log.debug("Nothing staged; vault already up to date.")
        return False

    commit = _run(["git", "commit", "-m", message], repo)
    if commit.returncode != 0:
        log.error("git commit failed: %s", (commit.stderr or commit.stdout).strip())
        return False
    log.info("Committed: %s", message)

    if push:
        # Rebase first: the daily sync workflow may have pushed since we started.
        pull = _run(["git", "pull", "--rebase", "--autostash"], repo)
        if pull.returncode != 0:
            log.warning("git pull --rebase failed: %s", pull.stderr.strip())

        pushed = _run(["git", "push"], repo)
        if pushed.returncode != 0:
            log.warning(
                "git push failed (%s). The commit is local; it will go out with the next push.",
                pushed.stderr.strip(),
            )
    return True
