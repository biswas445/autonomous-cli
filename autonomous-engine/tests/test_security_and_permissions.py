"""Security policy and the sandboxed ToolBox (plan.md §61, §62)."""

from __future__ import annotations

from pathlib import Path

import pytest

from autonomous_engine.core.config import PermissionClass
from autonomous_engine.core.security import (
    EndpointNotAllowed,
    find_secrets,
    is_safe_relative_path,
    validate_endpoint_url,
)
from autonomous_engine.runtime.permissions import PermissionDenied, ToolBox

# ---- endpoint policy -------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://api.openai.com/v1",
        "http://example.com:8080/path",
    ],
)
def test_valid_endpoints_pass(url: str):
    assert validate_endpoint_url(url) == url


@pytest.mark.parametrize(
    "url",
    [
        "https://localhost/v1",
        "http://127.0.0.1:11434",
        "http://[::1]:8080",
        "http://10.0.0.5/v1",
        "http://192.168.1.10/v1",
        "https://169.254.169.254/latest/meta-data",
        "http://metadata.google.internal/computeMetadata",
        "file:///etc/passwd",
        "ftp://example.com",
        "",
    ],
)
def test_forbidden_endpoints_rejected(url: str):
    with pytest.raises(EndpointNotAllowed):
        validate_endpoint_url(url)


def test_private_endpoints_require_explicit_opt_in():
    url = "http://127.0.0.1:11434/v1"
    with pytest.raises(EndpointNotAllowed):
        validate_endpoint_url(url)
    assert validate_endpoint_url(url, allow_private=True) == url


def test_secret_detection_reports_patterns_not_values():
    text = "key = sk-abcdefghijklmnopqrstuvwx and AKIAIOSFODNN7EXAMPLE"
    found = find_secrets(text)
    assert found, "secrets should be detected"
    for pattern in found:
        assert pattern not in text or pattern.startswith("\\")


def test_path_traversal_rejected():
    assert not is_safe_relative_path("../etc/passwd")
    assert not is_safe_relative_path("/absolute/path")
    assert not is_safe_relative_path("C:\\Windows\\system32")
    assert is_safe_relative_path("src/app/main.py")


# ---- ToolBox sandbox -------------------------------------------------------


def _toolbox(tmp_path, **perm_kwargs) -> ToolBox:
    perms = PermissionClass(
        name="test",
        read_repo=perm_kwargs.get("read_repo", True),
        write_paths=perm_kwargs.get("write_paths", ["src/**"]),
        run_commands=perm_kwargs.get("run_commands", True),
        allowed_command_globs=perm_kwargs.get("allowed_command_globs", ["python*"]),
        network=perm_kwargs.get("network", False),
        git_write=perm_kwargs.get("git_write", False),
    )
    return ToolBox(work_root=tmp_path, permissions=perms)


def test_write_policy_enforced(tmp_path: Path):
    tools = _toolbox(tmp_path)
    tools.write_file("src/app.py", "x = 1\n")
    assert (tmp_path / "src" / "app.py").read_text() == "x = 1\n"
    with pytest.raises(PermissionDenied):
        tools.write_file("elsewhere/app.py", "x = 1\n")


def test_path_traversal_blocked(tmp_path: Path):
    tools = _toolbox(tmp_path)
    with pytest.raises(PermissionDenied):
        tools.read_file("../outside.txt")


def test_read_requires_permission(tmp_path: Path):
    tools = _toolbox(tmp_path, read_repo=False)
    with pytest.raises(PermissionDenied):
        tools.list_dir(".")


def test_command_allowlist(tmp_path: Path):
    tools = _toolbox(tmp_path)
    result = tools.run_command('python -c "print(1)"')
    assert result.ok, result.stderr
    with pytest.raises(PermissionDenied):
        tools.run_command("rm -rf /")


def test_shell_metacharacters_do_not_execute(tmp_path: Path):
    """Model output is untrusted: commands run as argv lists, not a shell."""
    tools = _toolbox(tmp_path)
    result = tools.run_command('python -c "print(1); import os" # ; echo pwned')
    # The comment and `echo` must have been passed as python arguments, not run
    assert "pwned" not in result.stdout


def test_command_timeout(tmp_path: Path):
    tools = _toolbox(tmp_path)
    result = tools.run_command('python -c "import time; time.sleep(5)"', timeout=1)
    assert result.timed_out
    assert result.returncode == 124


def test_missing_command_reports_127(tmp_path: Path):
    tools = _toolbox(tmp_path, allowed_command_globs=["python*", "totally-missing-*"])
    result = tools.run_command("totally-missing-binary-xyz --flag")
    assert result.returncode == 127
    assert "not found" in result.stderr


def test_delete_file_policy(tmp_path: Path):
    tools = _toolbox(tmp_path)
    tools.write_file("src/app.py", "x = 1\n")
    removed = tools.delete_file("src/app.py")
    assert removed == "src/app.py"
    with pytest.raises(PermissionDenied):
        tools.delete_file("README.md")


def test_git_write_policy(tmp_path: Path):
    tools = _toolbox(tmp_path, git_write=False)
    with pytest.raises(PermissionDenied):
        tools.git_write(["status"])
