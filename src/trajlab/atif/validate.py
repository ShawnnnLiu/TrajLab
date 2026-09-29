"""Validate ATIF trajectory files with Harbor's own validator."""

import logging
from pathlib import Path

from harbor.utils.trajectory_validator import TrajectoryValidator

logger = logging.getLogger(__name__)


def validate_trajectory(path: Path) -> list[str]:
    """Validate an ATIF trajectory file; return Harbor's error messages, empty when valid.

    Also checks that local image and audio paths referenced by the trajectory exist.
    """
    if not path.is_file():
        return [f"File not found: {path}"]
    validator = TrajectoryValidator()
    validator.validate(path)
    errors = validator.get_errors()
    logger.debug("validated %s: %d error(s)", path, len(errors))
    return errors
