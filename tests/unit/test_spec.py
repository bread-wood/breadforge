"""Unit tests for spec parser."""

from pathlib import Path

import pytest

from breadforge.spec import parse_spec, validate_spec

SAMPLE_SPEC = """# MyProject v1.2.0 — Feature Name

## Overview
This milestone adds feature X to the platform. It provides Y and enables Z.
Without this, users cannot do W.

## Success Criteria
- [ ] `myproject run feature` completes successfully
- [ ] Feature X handles error case Y gracefully
- [ ] `make test` passes clean

## Scope
### Included
- Feature X implementation
- Unit and integration tests

### Excluded
- Feature Y (v1.3.0)
- UI changes

## Key Unknowns
- **[P1]** Does the external API support batch requests?
- **[P2]** What is the rate limit for service Y?

## Modules
- core: main feature logic
- api: HTTP endpoint for feature X
"""


@pytest.fixture
def spec_file(tmp_path: Path) -> Path:
    f = tmp_path / "v1.2.0-feature-name.md"
    f.write_text(SAMPLE_SPEC, encoding="utf-8")
    return f


class TestParseSpec:
    def test_header_parsed(self, spec_file: Path) -> None:
        spec = parse_spec(spec_file)
        assert spec.project == "MyProject"
        assert spec.version == "v1.2.0"
        assert spec.milestone_name == "Feature Name"

    def test_overview(self, spec_file: Path) -> None:
        spec = parse_spec(spec_file)
        assert "feature X" in spec.overview

    def test_success_criteria(self, spec_file: Path) -> None:
        spec = parse_spec(spec_file)
        assert len(spec.success_criteria) == 3
        assert any("make test" in c for c in spec.success_criteria)

    def test_scope_included(self, spec_file: Path) -> None:
        spec = parse_spec(spec_file)
        assert len(spec.scope_included) >= 1
        assert any("Feature X" in s for s in spec.scope_included)

    def test_scope_excluded(self, spec_file: Path) -> None:
        spec = parse_spec(spec_file)
        assert any("UI" in s for s in spec.scope_excluded)

    def test_key_unknowns(self, spec_file: Path) -> None:
        spec = parse_spec(spec_file)
        assert len(spec.key_unknowns) == 2
        assert spec.key_unknowns[0].priority == "P1"
        assert "batch" in spec.key_unknowns[0].text

    def test_modules(self, spec_file: Path) -> None:
        spec = parse_spec(spec_file)
        assert len(spec.modules) == 2
        names = [m.name for m in spec.modules]
        assert "core" in names
        assert "api" in names

    def test_title(self, spec_file: Path) -> None:
        spec = parse_spec(spec_file)
        assert spec.title == "MyProject v1.2.0 — Feature Name"

    def test_issue_title(self, spec_file: Path) -> None:
        spec = parse_spec(spec_file)
        assert spec.issue_title == "impl: Feature Name"

    def test_missing_file(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            parse_spec(tmp_path / "nonexistent.md")


class TestValidateSpec:
    def test_valid_spec_passes(self) -> None:
        missing = validate_spec(SAMPLE_SPEC)
        assert missing == []

    def test_missing_title_detected(self) -> None:
        text = "## Overview\nsome text\n## Success Criteria\n- [ ] thing\n"
        errors = validate_spec(text)
        assert any("title" in e for e in errors)

    def test_partial_spec_is_valid(self) -> None:
        # Missing Scope/Key Unknowns is no longer an error
        text = "# Project\n## Overview\nsome text\n## Goals\n- thing done\n"
        assert validate_spec(text) == []
