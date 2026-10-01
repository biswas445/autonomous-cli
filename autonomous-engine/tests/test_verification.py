"""Definition-of-Done engine: parsing and real execution (plan.md §13)."""

from __future__ import annotations

from pathlib import Path

from autonomous_engine.core.config import PermissionClass
from autonomous_engine.core.task import Task
from autonomous_engine.runtime.permissions import ToolBox
from autonomous_engine.verification.engine import (
    CheckStatus,
    DefinitionOfDone,
    VerificationEngine,
    parse_test_counts,
)


def _engine(tmp_path: Path) -> VerificationEngine:
    tools = ToolBox(
        work_root=tmp_path,
        permissions=PermissionClass(
            name="tester", read_repo=True, write_paths=["**"], run_commands=True
        ),
    )
    return VerificationEngine(tools)


def test_dod_parsing():
    dod = DefinitionOfDone(
        [
            "[ ] pytest tests/",
            "run: npm test",
            "file exists: src/app.py",
            "file contains: README.md: Verify",
            "file absent: secrets.txt",
            "The system shall be robust.",
        ]
    )
    checks = dod.checks()
    kinds = [c.kind for c in checks]
    assert kinds == ["command", "command", "file_exists", "file_contains", "file_absent", "manual"]


def test_dod_prose_is_manual_not_a_command():
    """Regression: 'Handles empty input gracefully' was parsed as a shell
    command (first token looked path-like) and failed verification."""
    dod = DefinitionOfDone(["Handles empty input gracefully", "pytest -q"])
    checks = dod.checks()
    assert checks[0].kind == "manual"
    assert checks[1].kind == "command"


def test_file_contains_handles_windows_absolute_paths(tmp_path: Path):
    """Regression: the first-colon split turned 'D:\\repo\\app.py: needle'
    into path 'D' and failed every Windows absolute-path criterion."""
    (tmp_path / "app.py").write_text("def main():\n    pass\n", encoding="utf-8")
    engine = _engine(tmp_path)
    task = Task(
        id="TASK-901",
        title="win paths",
        definition_of_done=[
            f"file contains: {tmp_path / 'app.py'}: def main",
        ],
    )
    report = engine.verify_task(task)
    assert report.passed, report.failures


def test_verification_passes_with_real_evidence(tmp_path: Path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("VALUE = 41\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("# Demo\n\n## Verify\nrun pytest\n", encoding="utf-8")
    engine = _engine(tmp_path)
    task = Task(
        id="TASK-001",
        title="demo",
        verification_commands=['python -c "import sys; sys.exit(0)"'],
        definition_of_done=[
            "file exists: src/app.py",
            "file contains: README.md: Verify",
            "file absent: secrets.txt",
        ],
    )
    report = engine.verify_task(task)
    assert report.passed, report.failures
    assert len(report.evidence) == 1
    assert all(c.status == CheckStatus.PASS for c in report.checks)


def test_verification_fails_on_failing_command(tmp_path: Path):
    engine = _engine(tmp_path)
    task = Task(
        id="TASK-002",
        title="broken",
        verification_commands=['python -c "import sys; sys.exit(3)"'],
    )
    report = engine.verify_task(task)
    assert not report.passed
    assert any("exit 3" in f or "3" in f for f in report.failures)


def test_verification_fails_on_missing_file(tmp_path: Path):
    engine = _engine(tmp_path)
    task = Task(id="T", title="t", definition_of_done=["file exists: missing.md"])
    report = engine.verify_task(task)
    assert not report.passed
    assert report.checks[0].status == CheckStatus.FAIL


def test_manual_criteria_are_unknown_not_pass(tmp_path: Path):
    engine = _engine(tmp_path)
    task = Task(id="T", title="t", definition_of_done=["The interface feels premium."])
    report = engine.verify_task(task)
    assert not report.passed
    assert report.checks[0].status == CheckStatus.UNKNOWN
    assert report.unverified


def test_task_without_criteria_is_not_verified(tmp_path: Path):
    engine = _engine(tmp_path)
    task = Task(id="T", title="nothing checkable")
    report = engine.verify_task(task)
    assert not report.passed
    assert report.unverified


def test_file_contains_bad_syntax_is_a_clear_failure(tmp_path: Path):
    engine = _engine(tmp_path)
    task = Task(id="T", title="t", definition_of_done=["file contains: Verify"])
    report = engine.verify_task(task)
    assert not report.passed
    assert "file-contains syntax" in report.checks[0].detail


def test_parse_test_counts():
    counts = parse_test_counts("3 failed, 12 passed, 1 skipped in 0.5s")
    assert counts["passed"] == 12
    assert counts["failed"] == 3
    assert counts["skipped"] == 1
