"""forge.py — shim for backward compatibility.

spec_forge and helpers now live in breadforge.forge (the sub-package).
"""

from breadforge.forge.interview import _apply_interview, _run_interview  # noqa: F401
from breadforge.forge.main import spec_forge  # noqa: F401
from breadforge.forge.validator import _check_violations  # noqa: F401
