"""Shared pydantic models: the only capture module analysis code imports.

Models only: no I/O, no imports from other trajlab modules.
"""

from trajlab.contracts.checkpoint import CheckpointRecord
from trajlab.contracts.steps import (
    CHECKPOINT_STEP_MESSAGE,
    CONTEXT_MANAGEMENT_EXTRA_KEY,
    TRAJLAB_EXTRA_KEY,
    CheckpointStepExtra,
    CompactionStepExtra,
    ContextManagementExtra,
    OriginalStepExtra,
    TrajlabStepExtra,
)
from trajlab.contracts.trial import TrialRecord

__all__ = [
    "CHECKPOINT_STEP_MESSAGE",
    "CONTEXT_MANAGEMENT_EXTRA_KEY",
    "TRAJLAB_EXTRA_KEY",
    "CheckpointRecord",
    "CheckpointStepExtra",
    "CompactionStepExtra",
    "ContextManagementExtra",
    "OriginalStepExtra",
    "TrajlabStepExtra",
    "TrialRecord",
]
