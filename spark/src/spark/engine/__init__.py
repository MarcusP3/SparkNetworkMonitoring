"""The check engine: state machine, incident lifecycle, and the runner."""

from .runner import check_target, run_target, spec_for
from .state import Transition, apply_outcome, human_duration, observed_status

__all__ = [
    "Transition",
    "apply_outcome",
    "check_target",
    "human_duration",
    "observed_status",
    "run_target",
    "spec_for",
]
