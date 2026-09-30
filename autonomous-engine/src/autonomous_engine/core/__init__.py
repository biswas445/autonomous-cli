"""Core domain models: project, tasks, events, checkpoints, decisions.

These are plain pydantic models with an explicit schema — the persistent
"Project Brain" of the runtime. Everything the orchestrator persists is
defined here.
"""
