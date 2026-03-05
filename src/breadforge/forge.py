"""Spec Forge — interactive, human-in-the-loop spec design.

Takes a feature description, scans all registered repos, conducts a structured
interview, drafts spec files, validates them against TEMPLATE.md, checks for
architecture violations, and updates the platform campaign.

Usage:
  breadforge spec "add order history to pantry"
  breadforge spec --file feature.md
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from breadforge.config import Registry

from breadforge.spec import validate_spec

# ---------------------------------------------------------------------------
# Architecture violation patterns
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Repo scanner
# ---------------------------------------------------------------------------


def _scan_repo_context(registry: Registry) -> str:
    """Read CLAUDE.md files from all registered repos to build context."""
    context_parts = []
    for entry in registry.list():
        claude_md = entry.local_path / "CLAUDE.md"
        if claude_md.exists():
            try:
                content = claude_md.read_text(encoding="utf-8")[:3000]
                context_parts.append(f"=== {entry.repo} CLAUDE.md ===\n{content}")
            except OSError:
                pass
    return "\n\n".join(context_parts)


# ---------------------------------------------------------------------------
# LLM-based spec drafting
# ---------------------------------------------------------------------------

_DRAFT_PROMPT = """You are a platform spec writer for the bread-wood platform.

Platform context:
{context}

Feature request: {description}

Draft a milestone spec following this exact structure:

# <Project> v<X.Y.Z> — <Milestone Name>

## Overview
<1-3 paragraphs: what, why, key architectural decisions>

## Success Criteria
- [ ] <measurable acceptance criterion>
- [ ] <...>

## Scope
### Included
- <concrete deliverable>

### Excluded
- <explicit non-goal>

## Key Unknowns
- **[P1]** <open question that must be answered before impl>

## Modules
- <module-name>: <one-line description of what it does>

Rules:
- Identify which existing repo(s) this belongs to, or propose a new one
- Keep it product-focused (what + why), not technical (not how)
- Success Criteria must be testable/verifiable
- Key Unknowns must have P0-P4 priority labels
- If the feature spans multiple repos, produce one spec per repo
"""


async def _draft_spec(description: str, context: str) -> str:
    """Draft a spec via LLM."""
    prompt = _DRAFT_PROMPT.format(description=description, context=context[:8000])

    try:
        from breadmin_llm.registry import ProviderRegistry
        from breadmin_llm.types import LLMCall, LLMMessage, MessageRole

        registry = ProviderRegistry.default()
        call = LLMCall(
            model="claude-sonnet-4-6",
            messages=[LLMMessage(role=MessageRole.USER, content=prompt)],
            max_tokens=4000,
            caller="breadforge.forge",
        )
        result = await registry.complete(call)
        return result.content
    except ImportError:
        pass

    import anthropic

    client = anthropic.AsyncAnthropic()
    response = await client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=4000,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# Interview questions
# ---------------------------------------------------------------------------

_INTERVIEW_QUESTIONS = [
    ("repo", "Which repo does this belong to? (or 'new' for a new repo)"),
    (
        "interface",
        "What is the delivery interface? (CLI command / API endpoint / library / daemon)",
    ),
    ("cross_repo", "Does this coordinate with other repos? If so, which ones?"),
    ("p1_unknowns", "What are the P1 unknowns — things you must investigate before building?"),
    ("non_goals", "What is explicitly out of scope for this milestone?"),
]


def _run_interview() -> dict[str, str]:
    """Run interactive interview in terminal. Returns answers dict."""
    import sys

    if not sys.stdin.isatty():
        return {}

    print("\nbreadforge spec-forge — answering these will improve the spec:\n")
    answers: dict[str, str] = {}
    for key, question in _INTERVIEW_QUESTIONS:
        try:
            answer = input(f"  {question}\n  > ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if answer:
            answers[key] = answer
    return answers


def _apply_interview(spec_text: str, answers: dict[str, str]) -> str:
    """Refine spec draft with interview answers."""
    if not answers:
        return spec_text
    notes = "\n\n## Spec-Forge Notes\n"
    for key, value in answers.items():
        notes += f"- **{key}**: {value}\n"
    return spec_text + notes


# ---------------------------------------------------------------------------
# Campaign update
# ---------------------------------------------------------------------------


def _update_campaign(
    campaign_path: Path,
    new_milestone: str,
    new_repo: str,
    blocked_by: list[str] | None = None,
) -> None:
    """Append new milestone to campaign file at correct wave position."""
    if not campaign_path.exists():
        return

    content = campaign_path.read_text(encoding="utf-8")
    blocked_note = ""
    if blocked_by:
        blocked_note = f" (blocked by: {', '.join(blocked_by)})"

    entry = f"| {new_milestone} | {new_repo} | PLANNED | TBD{blocked_note} | TBD |\n"

    # Find the version map table and append
    if "| Version |" in content:
        lines = content.splitlines()
        insert_at = len(lines)
        for i, line in enumerate(lines):
            if "| Version |" in line:
                # Find end of table
                j = i + 1
                while j < len(lines) and lines[j].startswith("|"):
                    j += 1
                insert_at = j
                break
        lines.insert(insert_at, entry.rstrip())
        campaign_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


async def spec_forge(
    description: str | None = None,
    *,
    file: Path | None = None,
    registry: Registry | None = None,
    interactive: bool = True,
    output_dir: Path | None = None,
) -> list[Path]:
    """Run the spec-forge pipeline. Returns list of written spec file paths."""
    import sys

    # Get description
    if description is None and file is not None:
        description = file.read_text(encoding="utf-8")
    if not description:
        print("error: provide a description or --file", file=sys.stderr)
        return []

    # Scan platform context
    context = ""
    if registry:
        context = _scan_repo_context(registry)

    # Run interview
    answers: dict[str, str] = {}
    if interactive:
        answers = _run_interview()

    # Draft spec
    print("Drafting spec...")
    spec_text = await _draft_spec(description, context)
    spec_text = _apply_interview(spec_text, answers)

    # Validate
    missing = validate_spec(spec_text)
    if missing:
        print(f"Warning: spec missing sections: {', '.join(missing)}")

    # Check architecture violations
    violations = _check_violations(spec_text)
    if violations:
        print("\nArchitecture violations detected:")
        for v in violations:
            print(f"  [VIOLATION] {v}")
        spec_text += "\n\n## Architecture Notes\n"
        for v in violations:
            spec_text += f"- [VIOLATION] {v}\n"

    # Show draft to user
    print("\n" + "=" * 60)
    print(spec_text)
    print("=" * 60)

    if interactive:
        import sys

        if sys.stdin.isatty():
            confirm = input("\nWrite this spec? [y/N] ").strip().lower()
            if confirm != "y":
                print("Aborted.")
                return []

    # Write spec file
    written: list[Path] = []
    out_dir = output_dir or Path.cwd()

    # Extract filename from spec header
    first_line = spec_text.splitlines()[0].lstrip("# ").strip()
    # Sanitize to filename
    filename = re.sub(r"[^\w.-]", "-", first_line.lower()) + ".md"
    filename = re.sub(r"-+", "-", filename)

    out_path = out_dir / filename
    out_path.write_text(spec_text, encoding="utf-8")
    written.append(out_path)
    print(f"Written: {out_path}")

    return written
