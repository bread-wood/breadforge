# {Project} {vX.Y.Z} — {Milestone Name}

<!--
TEMPLATE — copy this file, fill in what you know, leave the rest out.
Every section below is OPTIONAL except the # title heading.
The plan agent will infer missing structure from your description.
-->

## Overview

One paragraph describing what this milestone builds and why.
Free text — prose is fine. The more context the better, but even a single
sentence is enough for the plan agent to proceed.

## Goals

What "done" looks like from a user or caller perspective.

- The thing works as described
- `uv run pytest` passes
- **[P0]** Hard requirement — must be met before ship

Accepted formats for bullet priorities:
- `- item` — default P2
- `- **[P1]** item` — explicit priority
- `- **[?]** item` — completely open; agent decides

## Out of Scope

Things explicitly NOT included in this milestone. Omit section if nothing to exclude.

- Feature X
- Nice-to-have Y

## Open Questions

Ambiguous areas. The plan agent emits research nodes for P0/P1 questions.

- **[P1]** Which library should handle X?
- **[P2]** What should the error format look like?
- **[?]** Entirely open — agent decides

## Constraints

Hard technical requirements that limit the solution space.

- Must use Python 3.11+
- No new runtime dependencies outside the stdlib
- Must remain backward-compatible with existing config schema

## Modules

Explicit module breakdown. Omit section to let the plan agent propose its own.

- core: the main implementation logic
- cli: command-line interface wiring
- tests: unit and integration tests

---

### Minimal valid spec (just a title + description)

The absolute minimum breadforge accepts:

```markdown
# My Project — Feature Name

Build a thing that does stuff. Users need to click a button to activate it.
The button should be green.
```

The plan agent will infer modules, files, and success criteria from the description.
Set confidence < 0.6 triggers research nodes for unknowns before planning.
