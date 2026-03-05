"""Spec forge entry point — coordinates interview, drafting, validation, and writing."""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from breadforge.config import Registry


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

    from breadforge.forge.drafter import _draft_spec
    from breadforge.forge.interview import _apply_interview, _run_interview
    from breadforge.forge.validator import _check_violations, validate_spec

    if description is None and file is not None:
        description = file.read_text(encoding="utf-8")
    if not description:
        print("error: provide a description or --file", file=sys.stderr)
        return []

    context = ""
    if registry:
        context = _scan_repo_context(registry)

    answers: dict[str, str] = {}
    if interactive:
        answers = _run_interview()

    print("Drafting spec...")
    spec_text = await _draft_spec(description, context)
    spec_text = _apply_interview(spec_text, answers)

    missing = validate_spec(spec_text)
    if missing:
        print(f"Warning: spec missing sections: {', '.join(missing)}")

    violations = _check_violations(spec_text)
    if violations:
        print("\nArchitecture violations detected:")
        for v in violations:
            print(f"  [VIOLATION] {v}")
        spec_text += "\n\n## Architecture Notes\n"
        for v in violations:
            spec_text += f"- [VIOLATION] {v}\n"

    print("\n" + "=" * 60)
    print(spec_text)
    print("=" * 60)

    if interactive and sys.stdin.isatty():
        confirm = input("\nWrite this spec? [y/N] ").strip().lower()
        if confirm != "y":
            print("Aborted.")
            return []

    written: list[Path] = []
    out_dir = output_dir or Path.cwd()

    first_line = spec_text.splitlines()[0].lstrip("# ").strip()
    filename = re.sub(r"[^\w.-]", "-", first_line.lower()) + ".md"
    filename = re.sub(r"-+", "-", filename)

    out_path = out_dir / filename
    out_path.write_text(spec_text, encoding="utf-8")
    written.append(out_path)
    print(f"Written: {out_path}")

    return written
