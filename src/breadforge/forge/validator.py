"""Spec validator — checks for required sections and architecture violations."""

from __future__ import annotations

import re

_VIOLATION_PATTERNS = [
    (
        r"dual.scheduler|two.scheduler|second.scheduler",
        "dual schedulers detected — consolidate into a single scheduler per service",
    ),
    (
        r"direct.*(import|depend).*(cross.repo|other.repo)",
        "direct cross-repo imports — use APIs or events instead",
    ),
    (
        r"parallel.cost.track",
        "parallel cost tracking — breadmin-llm is the single cost tracking authority",
    ),
    (
        r"bypass.*bead|skip.*bead|without.*bead",
        "bypassing bead tracking — all tracked work must have a WorkBead",
    ),
]


def _check_violations(spec_text: str) -> list[str]:
    violations = []
    text_lower = spec_text.lower()
    for pattern, message in _VIOLATION_PATTERNS:
        if re.search(pattern, text_lower):
            violations.append(message)
    return violations


def validate_spec(spec_text: str) -> list[str]:
    """Return list of missing required sections."""
    from breadforge.spec import validate_spec as _validate

    return _validate(spec_text)
