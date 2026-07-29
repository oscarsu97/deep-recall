"""git_sync tests against real throwaway repositories in tmp_path.

The contract that matters: `repo` is the *vault* directory, and git resolves
the enclosing repository from it. That is what lets the same code serve both
the single-repo layout and the public-code/private-vault split.
"""

import subprocess

import pytest

from src import git_sync
from src.vault import Card, Vault

pytestmark = pytest.mark.skipif(
    subprocess.run(["git", "--version"], capture_output=True).returncode != 0,
    reason="git is not installed",
)


@pytest.fixture(autouse=True)
def isolated_git_config(tmp_path, monkeypatch):
    """Ignore the developer's global/system git config.

    Without this the suite inherits whatever is on the machine — a global
    `user.email` hides the fresh-checkout case, and `commit.gpgsign=true`
    would fail every commit here.
    """
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "no-global-gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", str(tmp_path / "no-system-gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")


def git(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)


def make_repo(path):
    path.mkdir(parents=True, exist_ok=True)
    git("init", "-b", "main", cwd=path)
    git("config", "user.email", "test@example.com", cwd=path)
    git("config", "user.name", "Test", cwd=path)
    (path / "README.md").write_text("seed")
    git("add", "-A", cwd=path)
    git("commit", "-m", "seed", cwd=path)
    return path


def write_card(vault, card_id="kafka-retry-patterns"):
    card = Card(
        id=card_id,
        topic="Distributed Systems",
        question=f"How does {card_id} work?",
        sections={"Direct Mechanism": "Uses `commitSync()`."},
        meta={"id": card_id, "topic": "Distributed Systems"},
    )
    return vault.save(card)


def log_subjects(repo):
    return git("log", "--format=%s", cwd=repo).stdout.split("\n")


# --- repo detection -------------------------------------------------------


def test_is_repo_detects_a_real_repository(tmp_path):
    assert git_sync.is_repo(make_repo(tmp_path / "repo"))


def test_is_repo_rejects_a_plain_directory(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    assert not git_sync.is_repo(plain)


def test_commit_is_skipped_outside_a_repository(tmp_path, caplog):
    vault = Vault(tmp_path / "vault")
    vault.ensure()
    path = write_card(vault)

    assert git_sync.commit_paths([path], "msg", repo=vault.root, push=False) is False
    assert path.exists()  # the card is still safely on disk


def test_missing_vault_directory_does_not_raise(tmp_path):
    assert git_sync.commit_paths(
        [tmp_path / "nope" / "x.md"], "msg", repo=tmp_path / "nope", push=False
    ) is False


# --- single-repo layout ---------------------------------------------------


def test_vault_nested_in_the_code_repo_commits_to_that_repo(tmp_path):
    repo = make_repo(tmp_path / "deep-recall")
    vault = Vault(repo / "vault")
    vault.ensure()
    path = write_card(vault)

    assert git_sync.commit_paths([path], "sync: add 1 card", repo=vault.root, push=False) is True
    assert "sync: add 1 card" in log_subjects(repo)
    assert "vault/distributed-systems/kafka-retry-patterns.md" in git(
        "show", "--name-only", "--format=", cwd=repo
    ).stdout


# --- split-repo layout ----------------------------------------------------


def test_vault_repo_nested_inside_a_code_repo_commits_to_the_vault_repo(tmp_path):
    """The public-code / private-vault layout: two independent repos."""
    code = make_repo(tmp_path / "deep-recall")
    (code / ".gitignore").write_text("/vault/\n")
    git("add", "-A", cwd=code)
    git("commit", "-m", "ignore vault", cwd=code)

    vault_repo = make_repo(code / "vault")
    vault = Vault(vault_repo)
    path = write_card(vault)

    assert git_sync.commit_paths([path], "review(x): quality=3", repo=vault.root, push=False)

    # The rating landed in the vault repo…
    assert "review(x): quality=3" in log_subjects(vault_repo)
    # …and left the public code repo untouched.
    assert "review(x): quality=3" not in log_subjects(code)
    assert git("status", "--porcelain", cwd=code).stdout.strip() == ""


# --- behaviour ------------------------------------------------------------


def test_committing_unchanged_content_is_a_no_op(tmp_path):
    repo = make_repo(tmp_path / "repo")
    vault = Vault(repo / "vault")
    vault.ensure()
    path = write_card(vault)

    assert git_sync.commit_paths([path], "first", repo=vault.root, push=False) is True
    assert git_sync.commit_paths([path], "second", repo=vault.root, push=False) is False
    assert "second" not in log_subjects(repo)


def test_identity_is_configured_when_the_repo_has_none(tmp_path):
    repo = tmp_path / "bare-identity"
    repo.mkdir()
    git("init", "-b", "main", cwd=repo)
    # Deliberately no user.email — a fresh CI checkout.
    vault = Vault(repo / "vault")
    vault.ensure()
    path = write_card(vault)

    assert git_sync.commit_paths([path], "first commit", repo=vault.root, push=False) is True
    assert git("config", "user.email", cwd=repo).stdout.strip() == git_sync.BOT_EMAIL


def test_push_failure_still_leaves_the_commit_intact(tmp_path, caplog):
    repo = make_repo(tmp_path / "repo")
    vault = Vault(repo / "vault")
    vault.ensure()
    path = write_card(vault)

    # No remote configured, so push must fail — but the rating is not lost.
    assert git_sync.commit_paths([path], "review: rated", repo=vault.root, push=True) is True
    assert "review: rated" in log_subjects(repo)
    assert any("push failed" in r.message for r in caplog.records)


def test_one_missing_path_does_not_lose_the_whole_commit(tmp_path, caplog):
    """`git add` aborts on a bad pathspec and stages nothing — the real failure
    mode that left a whole sync uncommitted."""
    repo = make_repo(tmp_path / "repo")
    vault = Vault(repo / "vault")
    vault.ensure()
    good_a = write_card(vault, "card-a")
    good_b = write_card(vault, "card-b")
    vanished = vault.root / "distributed-systems" / "never-written.md"

    assert git_sync.commit_paths(
        [good_a, vanished, good_b], "sync: add 3 card(s)", repo=vault.root, push=False
    ) is True

    committed = git("show", "--name-only", "--format=", cwd=repo).stdout
    assert "card-a.md" in committed
    assert "card-b.md" in committed, "a missing sibling must not drop the good cards"
    assert any("not on disk" in r.message for r in caplog.records)


def test_commit_fails_cleanly_when_no_path_exists(tmp_path):
    repo = make_repo(tmp_path / "repo")
    vault = Vault(repo / "vault")
    vault.ensure()
    assert git_sync.commit_paths(
        [vault.root / "gone.md"], "nothing", repo=vault.root, push=False
    ) is False


def test_empty_path_list_is_ignored(tmp_path):
    repo = make_repo(tmp_path / "repo")
    assert git_sync.commit_paths([], "nothing", repo=repo, push=False) is False
