"""Spec interview — interactive Q&A to refine the draft."""

from __future__ import annotations

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
