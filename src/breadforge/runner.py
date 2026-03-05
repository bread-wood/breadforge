"""runner.py — shim for backward compatibility.

RunResult and run_agent now live in breadforge.agents.runner.
build_agent_prompt now lives in breadforge.agents.prompts.
"""

from breadforge.agents.prompts import build_agent_prompt  # noqa: F401
from breadforge.agents.runner import RunResult, run_agent  # noqa: F401
