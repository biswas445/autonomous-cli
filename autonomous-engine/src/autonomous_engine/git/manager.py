"""Git manager: checkpoints, worktrees, validated merges (plan.md §10).

Git is infrastructure, so every operation here is deterministic and
subprocess-driven with explicit argument lists (never shell=True). All git
commands used by agents are read-only; write operations only happen through
GitManager methods that the orchestrator calls after verification passes.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


class GitError(RuntimeError):
    pass


@dataclass(frozen=True)
class GitResult:
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


class GitManager:
    def __init__(self, repo_root: Path):
        self.repo_root = Path(repo_root).resolve()

    # ---- low-level ----

    def _run(self, args: list[str], *, cwd: Path | None = None, timeout: int = 120) -> GitResult:
        env = dict(os.environ)
        # Unconditional override: setdefault kept an inherited truthy value,
        # which can hang every git call on a credential prompt until timeout.
        env["GIT_TERMINAL_PROMPT"] = "0"
        try:
            proc = subprocess.run(  # noqa: S603 - fixed argv, no shell
                ["git", *args],
                cwd=str(cwd or self.repo_root),
                capture_output=True,
                text=True,
                timeout=timeout,
                env=env,
                check=False,
            )
        except FileNotFoundError as exc:
            raise GitError("git executable not found on PATH") from exc
        except subprocess.TimeoutExpired as exc:
            return GitResult(returncode=124, stdout="", stderr=f"git timed out: {exc}")
        return GitResult(proc.returncode, proc.stdout.strip(), proc.stderr.strip())

    # ---- state ----

    def is_repo(self) -> bool:
        return self._run(["rev-parse", "--is-inside-work-tree"]).ok

    def ensure_repo(self) -> None:
        """Initialize a repository if the target directory is not already one."""
        self.repo_root.mkdir(parents=True, exist_ok=True)
        if not self.is_repo():
            result = self._run(["init", "-b", "main"])
            if not result.ok:
                raise GitError(f"git init failed: {result.stderr}")
            self._run(["config", "user.name", "Autonomous Engine"])
            self._run(["config", "user.email", "autonomous-engine@localhost"])
        self._exclude_worktrees()

    def _exclude_worktrees(self) -> None:
        """Keep worktree directories out of status/diff and checkpoint commits.

        The exclude file is local to the repository (never committed), so the
        runtime never edits a user-authored .gitignore.
        """
        exclude = self.repo_root / ".git" / "info" / "exclude"
        try:
            existing = exclude.read_text(encoding="utf-8") if exclude.is_file() else ""
            if ".worktrees/" not in existing:
                exclude.parent.mkdir(parents=True, exist_ok=True)
                exclude.write_text(existing.rstrip("\n") + "\n.worktrees/\n", encoding="utf-8")
        except OSError:
            pass

    def head_commit(self) -> str:
        result = self._run(["rev-parse", "HEAD"])
        return result.stdout if result.ok else ""

    def is_dirty(self) -> bool:
        result = self._run(["status", "--porcelain"])
        return (not result.ok) or bool(result.stdout)

    def status_porcelain(self) -> list[str]:
        result = self._run(["status", "--porcelain"])
        return [line for line in result.stdout.splitlines() if line.strip()]

    def diff_stat(self, base: str = "HEAD", *, cached: bool = False) -> str:
        args = ["diff", "--stat"]
        if cached:
            args.append("--cached")
        else:
            args.append(base)
        result = self._run(args)
        return result.stdout

    def diff(
        self, base: str = "HEAD", *, paths: list[str] | None = None, max_bytes: int = 40_000
    ) -> str:
        args = ["diff", "--unified=3", base]
        if paths:
            args += ["--", *paths]
        result = self._run(args)
        text = result.stdout
        if len(text) > max_bytes:
            text = text[:max_bytes] + f"\n... [diff truncated at {max_bytes} bytes]"
        return text

    def log(self, limit: int = 10) -> list[str]:
        result = self._run(["log", f"-{limit}", "--pretty=format:%h %s"])
        return [line for line in result.stdout.splitlines() if line.strip()]

    def show_file(self, relpath: str, rev: str = "HEAD") -> str:
        result = self._run(["show", f"{rev}:{relpath}"])
        return result.stdout if result.ok else ""

    # ---- writes (orchestrator-controlled) ----

    def stage_all(self) -> None:
        self._run(["add", "-A"])

    def commit(self, message: str, *, allow_empty: bool = False) -> str:
        args = ["commit", "-m", message]
        if allow_empty:
            args.append("--allow-empty")
        result = self._run(args)
        if not result.ok:
            if "nothing to commit" in result.stdout + result.stderr:
                return self.head_commit()
            raise GitError(f"git commit failed: {result.stderr or result.stdout}")
        return self.head_commit()

    def create_checkpoint_commit(self, task_id: str, message: str) -> str:
        """Stage everything and commit; returns the commit sha (or '' if clean)."""
        before = self.head_commit()
        self.stage_all()
        dirty = self.status_porcelain()
        if not dirty and not before:
            return self.commit(message, allow_empty=True)
        if not dirty:
            return before
        return self.commit(message)

    def checkout(self, ref: str) -> None:
        result = self._run(["checkout", ref])
        if not result.ok:
            raise GitError(f"git checkout {ref} failed: {result.stderr}")

    def reset_hard(self, ref: str) -> None:
        """Destructive: discard working-tree changes and return to `ref`."""
        result = self._run(["reset", "--hard", ref])
        if not result.ok:
            raise GitError(f"git reset --hard {ref} failed: {result.stderr}")
        self._run(["clean", "-fd"])

    def tag(self, name: str, message: str) -> None:
        self._run(["tag", "-a", name, "-m", message])

    # ---- worktrees (parallel execution isolation, §10/§22) ----

    def worktree_path(self, task_id: str) -> Path:
        return self.repo_root / ".worktrees" / task_id

    def create_worktree(self, task_id: str, ref: str = "HEAD") -> Path | None:
        path = self.worktree_path(task_id)
        if path.exists():
            return path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._exclude_worktrees()
        result = self._run(["worktree", "add", "--detach", str(path), ref])
        if not result.ok:
            return None
        return path

    def remove_worktree(self, task_id: str) -> None:
        path = self.worktree_path(task_id)
        self._run(["worktree", "remove", "--force", str(path)])
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)
        self._run(["worktree", "prune"])

    def list_worktrees(self) -> list[str]:
        # --porcelain: the human-readable output splits on whitespace, which
        # truncated any path containing spaces (this project's own path has
        # two). One path per "worktree <path>" line.
        result = self._run(["worktree", "list", "--porcelain"])
        return [
            line[len("worktree ") :]
            for line in result.stdout.splitlines()
            if line.startswith("worktree ")
        ]

    def merge_validated_worktree(self, task_id: str, message: str) -> str:
        """Merge a verified task worktree back into the main branch.

        Git merges commits, not working directories, so the coder's verified
        working-tree changes are committed inside the worktree first — without
        this, HEAD still points at the base commit and the merge would be a
        no-op while remove_worktree deletes every change the task made. A
        clean worktree (task produced no files) is tolerated. Only ever
        called after the task's verification passed; on conflict the merge
        is aborted and the worktree is left intact for inspection.
        """
        path = self.worktree_path(task_id)
        if not path.is_dir():
            raise GitError(f"worktree does not exist: {path}")
        self._run(["add", "-A"], cwd=path)
        commit = self._run(["commit", "-m", message], cwd=path)
        if not commit.ok and "nothing to commit" not in (commit.stdout + commit.stderr):
            raise GitError(f"cannot commit worktree changes for {task_id}: {commit.stderr}")
        head = self._run(["rev-parse", "HEAD"], cwd=path)
        if not head.ok:
            raise GitError(f"cannot resolve worktree HEAD for {task_id}: {head.stderr}")
        merge = self._run(["merge", "--no-ff", "-m", message, head.stdout])
        if not merge.ok:
            self._run(["merge", "--abort"])
            raise GitError(f"merge conflict merging {task_id}: {merge.stderr}")
        return self.head_commit()
