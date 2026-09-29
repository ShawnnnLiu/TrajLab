"""Validate ATIF trajectory files with Harbor's own validator, plus our strict load.

Validation only reads. A failure is reported, never repaired: the file is not rewritten,
moved, or deleted.
"""

import logging
from pathlib import Path

from harbor.utils.trajectory_validator import TrajectoryValidator

from trajlab.atif.load import TrajectoryLoadError, load_trajectory

logger = logging.getLogger(__name__)


def validate_trajectory(path: Path) -> list[str]:
    """Validate an ATIF trajectory file; return every error message, empty when valid.

    Runs Harbor's `TrajectoryValidator` (schema plus local image and audio paths), and, if that
    passes, the strict load so `validate` fails whenever `load_trajectory` would.

    Harbor collects every field-level error across all steps, but its model-level rules report
    less: the agent-only-fields rule gives at most one error per step, and the trajectory-level
    rules (sequential step ids, same-step `source_call_id`s) run only once every step is valid
    and stop at their first violation. Fixing one error can therefore surface another.
    """
    if not path.is_file():
        return [f"File not found: {path}"]
    validator = TrajectoryValidator()
    if validator.validate(path):
        try:
            load_trajectory(path)
        except TrajectoryLoadError as e:
            errors = [f"strict: {error}" for error in e.errors]
        else:
            errors = []
    else:
        errors = validator.get_errors()
    logger.debug("validated %s: %d error(s)", path, len(errors))
    return errors
