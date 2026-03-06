"""Extended spec parser tests covering missing lines."""

from __future__ import annotations

from pathlib import Path

from breadforge.spec import (
    parse_campaign,
    parse_spec,
)


def write_spec(tmp_path: Path, name: str, content: str) -> Path:
    p = tmp_path / name
    p.write_text(content, encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# Header parsing edge cases (lines 154-158: fallback when no regex match)
# ---------------------------------------------------------------------------


class TestHeaderFallback:
    def test_no_version_in_title_falls_back_to_filename(self, tmp_path: Path) -> None:
        """Spec title without 'vX.Y.Z — Name' pattern falls back to filename-based parsing."""
        content = "# Simple Title\n\nJust a description.\n"
        p = write_spec(tmp_path, "v1.0-my-feature.md", content)
        spec = parse_spec(p)
        assert spec.version == "v1.0"
        assert spec.milestone_name == "My Feature"

    def test_fallback_uses_parent_dir_as_project(self, tmp_path: Path) -> None:
        content = "# Just A Title\n\nSome text.\n"
        p = write_spec(tmp_path, "plain-spec.md", content)
        spec = parse_spec(p)
        assert spec.project == tmp_path.name

    def test_filename_no_dash_uses_stem(self, tmp_path: Path) -> None:
        """Filename with no dash: version=stem, milestone_name=stem."""
        content = "# Something\n"
        p = write_spec(tmp_path, "plain.md", content)
        spec = parse_spec(p)
        assert spec.version == "plain"
        assert spec.milestone_name == "plain"

    def test_empty_spec(self, tmp_path: Path) -> None:
        """Empty spec file should not raise."""
        p = write_spec(tmp_path, "v1.0-empty.md", "")
        spec = parse_spec(p)
        assert spec.overview == ""
        assert spec.success_criteria == []


# ---------------------------------------------------------------------------
# Free text before any section → overview fallback (lines 232, 236)
# ---------------------------------------------------------------------------


class TestFreeTextOverview:
    def test_free_text_used_as_overview_when_no_overview_section(self, tmp_path: Path) -> None:
        content = "# MyProject v1.0.0 — Feature\n\nThis is free text before any section.\n"
        spec = parse_spec(write_spec(tmp_path, "v1.0.0-feature.md", content))
        assert "free text" in spec.overview

    def test_explicit_overview_section_takes_precedence(self, tmp_path: Path) -> None:
        content = (
            "# MyProject v1.0.0 — Feature\n\n"
            "This is free text.\n\n"
            "## Overview\n"
            "This is the real overview.\n"
        )
        spec = parse_spec(write_spec(tmp_path, "v1.0.0-feature.md", content))
        assert "real overview" in spec.overview
        assert "free text" not in spec.overview


# ---------------------------------------------------------------------------
# Goals / success criteria (line 178)
# ---------------------------------------------------------------------------


class TestGoalsSection:
    def test_goals_alias_parsed(self, tmp_path: Path) -> None:
        content = "# P v1.0.0 — F\n\n## Goals\n- Do thing A\n- Do thing B\n"
        spec = parse_spec(write_spec(tmp_path, "v1.0.0-f.md", content))
        assert len(spec.success_criteria) == 2
        assert "Do thing A" in spec.success_criteria

    def test_success_criteria_alias(self, tmp_path: Path) -> None:
        content = "# P v1.0.0 — F\n\n## Success Criteria\n- [ ] Task one\n- [x] Task two\n"
        spec = parse_spec(write_spec(tmp_path, "v1.0.0-f.md", content))
        assert len(spec.success_criteria) == 2

    def test_priority_tagged_goal(self, tmp_path: Path) -> None:
        content = "# P v1.0.0 — F\n\n## Goals\n- **[P1]** High priority item\n"
        spec = parse_spec(write_spec(tmp_path, "v1.0.0-f.md", content))
        assert any("High priority" in c for c in spec.success_criteria)


# ---------------------------------------------------------------------------
# Scope sections (lines 204-211)
# ---------------------------------------------------------------------------


class TestScopeSection:
    def test_out_of_scope_section(self, tmp_path: Path) -> None:
        content = "# P v1.0.0 — F\n\n## Out of Scope\n- Not this\n- Not that\n"
        spec = parse_spec(write_spec(tmp_path, "v1.0.0-f.md", content))
        assert len(spec.scope_excluded) == 2
        assert "Not this" in spec.scope_excluded

    def test_excluded_alias(self, tmp_path: Path) -> None:
        content = "# P v1.0.0 — F\n\n## Excluded\n- Skip this\n"
        spec = parse_spec(write_spec(tmp_path, "v1.0.0-f.md", content))
        assert "Skip this" in spec.scope_excluded

    def test_constraints_section(self, tmp_path: Path) -> None:
        content = "# P v1.0.0 — F\n\n## Constraints\n- Python 3.11+\n- No third-party HTTP\n"
        spec = parse_spec(write_spec(tmp_path, "v1.0.0-f.md", content))
        assert any("[constraint]" in s and "Python" in s for s in spec.scope_included)
        assert any("[constraint]" in s and "HTTP" in s for s in spec.scope_included)

    def test_scope_included_subsection(self, tmp_path: Path) -> None:
        content = "# P v1.0.0 — F\n\n## Scope\n### Included\n- Thing A\n### Excluded\n- Thing B\n"
        spec = parse_spec(write_spec(tmp_path, "v1.0.0-f.md", content))
        assert "Thing A" in spec.scope_included
        assert "Thing B" in spec.scope_excluded


# ---------------------------------------------------------------------------
# Open questions (lines 220-222: plain bullet without priority tag)
# ---------------------------------------------------------------------------


class TestOpenQuestions:
    def test_plain_unknown_gets_p2_priority(self, tmp_path: Path) -> None:
        content = "# P v1.0.0 — F\n\n## Open Questions\n- Which library to use?\n"
        spec = parse_spec(write_spec(tmp_path, "v1.0.0-f.md", content))
        assert len(spec.key_unknowns) == 1
        assert spec.key_unknowns[0].priority == "P2"
        assert "library" in spec.key_unknowns[0].text

    def test_question_mark_becomes_p2(self, tmp_path: Path) -> None:
        content = "# P v1.0.0 — F\n\n## Open Questions\n- **[?]** Agent decides\n"
        spec = parse_spec(write_spec(tmp_path, "v1.0.0-f.md", content))
        assert spec.key_unknowns[0].priority == "P2"

    def test_key_unknowns_alias(self, tmp_path: Path) -> None:
        content = "# P v1.0.0 — F\n\n## Key Unknowns\n- **[P0]** Critical unknown\n"
        spec = parse_spec(write_spec(tmp_path, "v1.0.0-f.md", content))
        assert spec.key_unknowns[0].priority == "P0"

    def test_unknowns_alias(self, tmp_path: Path) -> None:
        content = "# P v1.0.0 — F\n\n## Unknowns\n- **[P3]** Low priority unknown\n"
        spec = parse_spec(write_spec(tmp_path, "v1.0.0-f.md", content))
        assert spec.key_unknowns[0].priority == "P3"


# ---------------------------------------------------------------------------
# Blockquote skip (line 178: > lines skipped)
# ---------------------------------------------------------------------------


class TestBlockquoteSkip:
    def test_blockquote_lines_skipped(self, tmp_path: Path) -> None:
        content = (
            "# P v1.0.0 — F\n\n> This is a blockquote — metadata\n## Overview\nActual overview.\n"
        )
        spec = parse_spec(write_spec(tmp_path, "v1.0.0-f.md", content))
        assert "blockquote" not in spec.overview
        assert "Actual overview" in spec.overview


# ---------------------------------------------------------------------------
# parse_campaign (lines 273-298)
# ---------------------------------------------------------------------------


class TestParseCampaign:
    def test_basic_campaign(self, tmp_path: Path) -> None:
        specs_dir = tmp_path / "specs"
        specs_dir.mkdir()
        (specs_dir / "v1.0.md").write_text("# P v1.0.0 — A\n", encoding="utf-8")
        (specs_dir / "v2.0.md").write_text("# P v2.0.0 — B\n", encoding="utf-8")

        campaign_content = """\
# Campaign

```
breadforge run \\
  specs/v1.0.md \\
  specs/v2.0.md
```
"""
        campaign = tmp_path / "campaign.md"
        campaign.write_text(campaign_content, encoding="utf-8")
        paths = parse_campaign(campaign)
        assert len(paths) == 2
        assert paths[0].name == "v1.0.md"
        assert paths[1].name == "v2.0.md"

    def test_brimstone_command_also_accepted(self, tmp_path: Path) -> None:
        campaign_content = """\
# Campaign

```
brimstone run \\
  specs/v1.0.md
```
"""
        campaign = tmp_path / "campaign.md"
        campaign.write_text(campaign_content, encoding="utf-8")
        paths = parse_campaign(campaign)
        assert len(paths) == 1
        assert paths[0].name == "v1.0.md"

    def test_no_code_block_returns_empty(self, tmp_path: Path) -> None:
        campaign_content = "# Campaign\n\nJust text, no code block.\n"
        campaign = tmp_path / "campaign.md"
        campaign.write_text(campaign_content, encoding="utf-8")
        paths = parse_campaign(campaign)
        assert paths == []

    def test_flags_skipped(self, tmp_path: Path) -> None:
        campaign_content = """\
# Campaign

```
breadforge run \\
  --repo owner/repo \\
  --milestone v1.0 \\
  specs/v1.0.md
```
"""
        campaign = tmp_path / "campaign.md"
        campaign.write_text(campaign_content, encoding="utf-8")
        paths = parse_campaign(campaign)
        assert len(paths) == 1
        assert paths[0].name == "v1.0.md"

    def test_non_md_lines_skipped(self, tmp_path: Path) -> None:
        campaign_content = """\
# Campaign

```
breadforge run \\
  specs/v1.0.md \\
  some-flag \\
  specs/v2.0.md
```
"""
        campaign = tmp_path / "campaign.md"
        campaign.write_text(campaign_content, encoding="utf-8")
        paths = parse_campaign(campaign)
        assert len(paths) == 2
