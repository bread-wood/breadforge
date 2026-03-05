"""Spec file parser.

Parses milestone spec markdown files into structured MilestoneSpec objects.
Each spec file describes one milestone: what to build, success criteria, scope,
key unknowns, and affected modules.

Expected format (following brimstone-specs TEMPLATE.md):

  # Project vX.Y.Z — Milestone Name
  ## Overview
  ...
  ## Success Criteria
  - [ ] criterion
  ## Scope
  ### Included
  - item
  ### Excluded
  - item
  ## Key Unknowns
  - **[P1]** question
  ## Modules
  - module-name: description
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

    for line in lines[1:]:
        # Section headers
        if line.startswith("## "):
            current_section = line[3:].strip().lower()
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

        elif current_section == "success criteria":
            m = re.match(r"^\s*-\s*\[[ x]\]\s*(.+)$", line)
            if m:
                success_criteria.append(m.group(1).strip())

        elif current_section == "scope":
            if current_subsection == "included" or (
                not current_subsection and line.strip().startswith("- ")
            ):
                item = line.strip().lstrip("- ").strip()
                if item and not item.startswith("#"):
                    scope_included.append(item)
            elif current_subsection in ("excluded", "explicitly excluded"):
                item = line.strip().lstrip("- ").strip()
                if item and not item.startswith("#"):
                    scope_excluded.append(item)

        elif current_section == "key unknowns":
            # "- **[P1]** question text"
            m = re.match(r"^\s*-\s*\*\*\[(P[0-4])\]\*\*\s*(.+)$", line)
            if m:
                key_unknowns.append(KeyUnknown(priority=m.group(1), text=m.group(2).strip()))
            elif line.strip().startswith("- "):
                text = line.strip().lstrip("- ").strip()
                if text:
                    key_unknowns.append(KeyUnknown(priority="P2", text=text))

        elif current_section == "modules":
            # "- module-name: description"
            m = re.match(r"^\s*-\s*([\w/-]+):\s*(.+)$", line)
            if m:
                modules.append(ModuleSpec(name=m.group(1).strip(), description=m.group(2).strip()))

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


_REQUIRED_SECTIONS = [
    "## Overview",
    "## Success Criteria",
    "## Scope",
    "## Key Unknowns",
]


def validate_spec(spec_text: str) -> list[str]:
    """Return list of missing required sections."""
    missing = []
    for section in _REQUIRED_SECTIONS:
        if section not in spec_text:
            missing.append(section)
    return missing


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
