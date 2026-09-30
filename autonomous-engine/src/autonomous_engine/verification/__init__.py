"""Verification engine: the Definition-of-Done checker and evidence hierarchy.

The evidence hierarchy (plan.md §12) ranks proof from strongest to weakest:

    user requirement > acceptance criteria > executable test > observed result
    > agent reasoning

An agent saying "this should work" carries almost no weight; a passing command
carries a lot. This module only counts executable evidence — model opinions
alone can never mark a task complete.
"""

from .engine import (
    CheckStatus,
    CommandEvidence,
    DefinitionOfDone,
    DoDCheck,
    VerificationEngine,
    VerificationReport,
)

__all__ = [
    "CheckStatus",
    "CommandEvidence",
    "DefinitionOfDone",
    "DoDCheck",
    "VerificationReport",
    "VerificationEngine",
]
