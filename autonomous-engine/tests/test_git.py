"""Git manager: checkpoints, dirty tracking, worktrees, validated merges."""

from __future__ import annotations

from pathlib import Path

import pytest

from autonomous_engine.git.manager import GitError, GitManager


@pytest.fixture()
def repo(tmp_path: Path) -> GitManager:
    manager = GitManager(tmp_path / "repo")
    manager.repo_root.mkdir(parents=True)
    manager.ensure_repo()
    (manager.repo_root / "README.md").write_text("# repo\n", encoding="utf-8")
    manager.stage_all()
    manager.commit("initial")
    return manager


def test_is_repo_and_head(repo: GitManager):
    assert repo.is_repo()
    assert len(repo.head_commit()) == 40


def test_dirty_detection_and_diff(repo: GitManager):
    assert not repo.is_dirty()
    (repo.repo_root / "README.md").write_text("# changed\n", encoding="utf-8")
    assert repo.is_dirty()
    assert "README.md" in repo.diff_stat()


def test_checkpoint_commit_only_when_dirty(repo: GitManager):
    first = repo.create_checkpoint_commit("TASK-1", "checkpoint: one")
    assert first == repo.head_commit()  # nothing dirty: no new commit
    (repo.repo_root / "new.txt").write_text("data", encoding="utf-8")
    second = repo.create_checkpoint_commit("TASK-2", "checkpoint: two")
    assert second != first
    assert "checkpoint: two" in repo.log(1)[0]


def test_worktree_lifecycle_and_merge(repo: GitManager):
    worktree = repo.create_worktree("TASK-99")
    assert worktree is not None and worktree.is_dir()
    (worktree / "feature.txt").write_text("feature", encoding="utf-8")
    # commit inside the worktree (detached HEAD)
    result = repo._run(["add", "-A"], cwd=worktree)
    assert result.ok
    result = repo._run(["commit", "-m", "feature work"], cwd=worktree)
    assert result.ok
    merged = repo.merge_validated_worktree("TASK-99", "merge task TASK-99")
    assert len(merged) == 40
    assert (repo.repo_root / "feature.txt").read_text(encoding="utf-8") == "feature"
    repo.remove_worktree("TASK-99")
    assert not worktree.exists()


def test_merge_commits_uncommitted_worktree_changes(repo: GitManager):
    """Regression: the orchestrator never commits inside the worktree, so the
    merge must commit the verified working tree itself — otherwise the merge
    is a no-op against the base commit and remove_worktree deletes the work."""
    worktree = repo.create_worktree("TASK-98")
    assert worktree is not None
    (worktree / "feature.txt").write_text("feature", encoding="utf-8")
    merged = repo.merge_validated_worktree("TASK-98", "merge task TASK-98")
    assert len(merged) == 40
    assert (repo.repo_root / "feature.txt").read_text(encoding="utf-8") == "feature"
    repo.remove_worktree("TASK-98")
    assert not worktree.exists()


def test_merge_clean_worktree_is_a_no_op(repo: GitManager):
    worktree = repo.create_worktree("TASK-97")
    assert worktree is not None
    before = repo.head_commit()
    merged = repo.merge_validated_worktree("TASK-97", "merge task TASK-97")
    assert merged == before
    repo.remove_worktree("TASK-97")


def test_merge_conflict_aborts_cleanly(repo: GitManager):
    worktree = repo.create_worktree("TASK-77")
    assert worktree is not None
    try:
        (repo.repo_root / "README.md").write_text("main version\n", encoding="utf-8")
        repo.stage_all()
        repo.commit("main change")
        (worktree / "README.md").write_text("worktree version\n", encoding="utf-8")
        repo._run(["add", "-A"], cwd=worktree)
        repo._run(["commit", "-m", "worktree change"], cwd=worktree)
        with pytest.raises(GitError):
            repo.merge_validated_worktree("TASK-77", "conflicting merge")
        assert not repo.is_dirty()  # merge aborted; main is clean
    finally:
        repo.remove_worktree("TASK-77")


def test_reset_hard(repo: GitManager):
    (repo.repo_root / "temp.txt").write_text("x", encoding="utf-8")
    repo.stage_all()
    head_before = repo.commit("add temp")
    repo.reset_hard(repo.log(2)[1].split()[0])
    assert not (repo.repo_root / "temp.txt").exists()
    assert repo.head_commit() != head_before
