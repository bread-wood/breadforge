"""Spec file parser.

Parses milestone spec markdown files into structured MilestoneSpec objects.

## Spec format

The spec format is deliberately permissive. The only hard requirement is a
`# Title` heading. Every other section is optional — the plan agent infers
missing structure from free text.

### Minimal valid spec

  # Minesweeper — Desktop Game

  A classic minesweeper game. The player reveals and flags cells.
  Mines are randomly placed. Standard 9x9 rules apply.

### Full spec

  # Project vX.Y.Z — Milestone Name

  ## Overview
  Free text description of what to build.

  ## Goals  (alias: Success Criteria)
  - Done looks like this
  - And this

  ## Out of Scope  (alias: Scope / Excluded)
  - Not this
  - Or this

  ## Open Questions  (alias: Key Unknowns)
  - **[P1]** Which UI library?
  - **[?]** Completely open — agent decides

  ## Constraints
  - Must use Python 3.11+
  - No third-party HTTP clients

  ## Modules
  - game: board state, mine placement, reveal logic
  - ui: pygame rendering and event loop

### Ambiguity markers

  - `[P0]`–`[P4]` — priority; missing priority defaults to `[P2]`
  - `[?]` — completely undecided; agent must choose
  - No Modules section → plan agent proposes its own module breakdown

### Section aliases (all accepted)

  Goals / Success Criteria
  Out of Scope / Scope (Excluded subsection) / Excluded
  Open Questions / Key Unknowns / Unknowns
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


@dataclass
class ModuleSpec:
    name: str
    description: str


@dataclass
class KeyUnknown:
    priority: str  # P0-P4
    text: str


@dataclass
class MilestoneSpec:
    """Parsed representation of a single milestone spec file."""

    path: Path
    project: str
    version: str
    milestone_name: str
    overview: str
    success_criteria: list[str]
    scope_included: list[str]
    scope_excluded: list[str]
    key_unknowns: list[KeyUnknown]
    modules: list[ModuleSpec]
    raw: str

    @property
    def title(self) -> str:
        return f"{self.project} {self.version} — {self.milestone_name}"

    @property
    def issue_title(self) -> str:
        return f"impl: {self.milestone_name}"


_SECTION_ALIASES: dict[str, str] = {
    # Goals / success criteria
    "goals": "goals",
    "success criteria": "goals",
    # Out of scope
    "out of scope": "out of scope",
    "excluded": "out of scope",
    "explicitly excluded": "out of scope",
    # Open questions / key unknowns
    "open questions": "open questions",
    "key unknowns": "open questions",
    "unknowns": "open questions",
    # Pass-through sections
    "overview": "overview",
    "scope": "scope",
    "constraints": "constraints",
    "modules": "modules",
}


def _normalize_section(raw: str) -> str:
    """Normalize a section heading to a canonical key."""
    key = raw.strip().lower().split("(")[0].strip()  # strip parenthetical notes
    return _SECTION_ALIASES.get(key, key)


def parse_spec(path: Path) -> MilestoneSpec:
    """Parse a spec markdown file into a MilestoneSpec."""
    raw = path.read_text(encoding="utf-8")
    lines = raw.splitlines()

    project = ""
    version = ""
    milestone_name = ""
    overview_lines: list[str] = []
    success_criteria: list[str] = []
    scope_included: list[str] = []
    scope_excluded: list[str] = []
    key_unknowns: list[KeyUnknown] = []
    modules: list[ModuleSpec] = []

    # Parse header: "# Project vX.Y.Z — Milestone Name"
    if lines:
        header = lines[0].lstrip("# ").strip()
        # Try to match "Name vX.Y.Z — Title" or "Name vX.Y.Z: Title"
        m = re.match(r"^(.+?)\s+(v[\d.x]+(?:-[\w-]+)?)\s+[—–-]+\s+(.+)$", header)
        if m:
            project = m.group(1).strip()
            version = m.group(2).strip()
            milestone_name = m.group(3).strip()
        else:
            # fallback: use filename
            stem = path.stem
            parts = stem.split("-", 1)
            version = parts[0] if parts else stem
            milestone_name = parts[1].replace("-", " ").title() if len(parts) > 1 else stem
            project = path.parent.name

    current_section = ""
    current_subsection = ""

    # Collect free-text lines when no structured section matches (folded into overview)
    free_lines: list[str] = []

    for line in lines[1:]:
        # Section headers
        if line.startswith("## "):
            current_section = _normalize_section(line[3:].strip())
            current_subsection = ""
            continue
        if line.startswith("### "):
            current_subsection = line[4:].strip().lower()
            continue

        # Skip metadata blocks at top
        if line.startswith("> "):
            continue

        if current_section == "overview":
            if line.strip():
                overview_lines.append(line.strip())

        elif current_section == "goals":
            # Accept: "- [ ] text", "- [x] text", "- **[Px]** text", "- text"
            m = re.match(r"^\s*-\s*(?:\[[ x?]\]\s*|\*\*\[(?:P[0-4]|\?)\]\*\*\s*)?(.+)$", line)
            if m:
                text = m.group(1).strip()
                if text:
                    success_criteria.append(text)

        elif current_section == "scope":
            if current_subsection in ("included", ""):
                if line.strip().startswith("- "):
                    item = line.strip().lstrip("- ").strip()
                    if item:
                        scope_included.append(item)
            elif current_subsection in ("excluded", "explicitly excluded", "out of scope"):
                item = line.strip().lstrip("- ").strip()
                if item:
                    scope_excluded.append(item)

        elif current_section == "out of scope":
            item = line.strip().lstrip("- ").strip()
            if item:
                scope_excluded.append(item)

        elif current_section == "constraints":
            item = line.strip().lstrip("- ").strip()
            if item:
                scope_included.append(f"[constraint] {item}")

        elif current_section == "open questions":
            # Accept: "- **[P1]** text", "- **[?]** text", "- text"
            m = re.match(r"^\s*-\s*\*\*\[(P[0-4]|\?)\]\*\*\s*(.+)$", line)
            if m:
                priority = "P2" if m.group(1) == "?" else m.group(1)
                key_unknowns.append(KeyUnknown(priority=priority, text=m.group(2).strip()))
            elif line.strip().startswith("- "):
                text = line.strip().lstrip("- ").strip()
                if text:
                    key_unknowns.append(KeyUnknown(priority="P2", text=text))

        elif current_section == "modules":
            # "- module-name: description"
            m = re.match(r"^\s*-\s*([\w/-]+):\s*(.+)$", line)
            if m:
                modules.append(ModuleSpec(name=m.group(1).strip(), description=m.group(2).strip()))

        elif current_section == "":
            # Free text before any section — treat as overview
            if line.strip():
                free_lines.append(line.strip())

    # If no explicit overview, use free text that appeared before any section
    if not overview_lines and free_lines:
        overview_lines = free_lines

    return MilestoneSpec(
        path=path,
        project=project,
        version=version,
        milestone_name=milestone_name,
        overview=" ".join(overview_lines),
        success_criteria=success_criteria,
        scope_included=scope_included,
        scope_excluded=scope_excluded,
        key_unknowns=key_unknowns,
        modules=modules,
        raw=raw,
    )


def validate_spec(spec_text: str) -> list[str]:
    """Return list of validation errors.

    Only hard-fails if there is no title heading at all.  Missing optional
    sections are not errors — the plan agent infers structure from free text.
    """
    errors = []
    lines = spec_text.strip().splitlines()
    has_title = any(line.startswith("# ") for line in lines[:3])
    if not has_title:
        errors.append("missing title: first line must be a # heading")
    return errors


def parse_campaign(campaign_path: Path) -> list[tuple[str, list[Path]]]:
    """Parse a campaign.md and return ordered list of (milestone, [spec_paths]).

    Looks for a fenced code block with 'breadforge run' or 'brimstone run'
    listing spec files in order.
    """
    raw = campaign_path.read_text(encoding="utf-8")
    base = campaign_path.parent

    # Find code block with run command
    in_block = False
    run_lines: list[str] = []
    for line in raw.splitlines():
        if line.strip().startswith("```") and not in_block:
            in_block = True
            continue
        if line.strip().startswith("```") and in_block:
            break
        if in_block:
            run_lines.append(line.strip())

    spec_paths: list[Path] = []
    for line in run_lines:
        # Skip the command line itself and flags
        if line.startswith("breadforge") or line.startswith("brimstone") or line.startswith("--"):
            continue
        line = line.rstrip(" \\")
        if line.endswith(".md"):
            p = base / line if not Path(line).is_absolute() else Path(line)
            spec_paths.append(p)

    return spec_paths
