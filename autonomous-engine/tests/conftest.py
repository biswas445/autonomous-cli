"""Shared fixtures: isolated project workspaces and a reset echo provider."""

from __future__ import annotations

from pathlib import Path

import pytest

from autonomous_engine.core.config import ProjectConfig
from autonomous_engine.core.workspace import Workspace
from autonomous_engine.models.base import get_provider
from autonomous_engine.runtime.bootstrap import init_project
from autonomous_engine.runtime.context_setup import open_context


@pytest.fixture()
def echo():
    """The registered echo provider, reset between tests.

    The provider registry is global, so every test gets the same instance;
    clearing its canned state keeps tests order-independent.
    """
    provider = get_provider("echo")
    provider.canned.clear()
    provider.queue.clear()
    return provider


@pytest.fixture()
def project(tmp_path: Path) -> Path:
    """A freshly initialised project workspace."""
    root = tmp_path / "target-project"
    init_project(root, project_name="target", objective="Build the target system")
    return root


@pytest.fixture()
def context(project: Path):
    ctx = open_context(project)
    yield ctx
    ctx.db.close()


@pytest.fixture()
def workspace(project: Path) -> Workspace:
    return Workspace(project)


@pytest.fixture()
def quiet_config() -> ProjectConfig:
    """Config with fast budgets for tests."""
    return ProjectConfig()
