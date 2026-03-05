# breadforge

Platform build orchestrator — spec-driven, bead-tracked, multi-repo.

breadforge takes a spec file describing what to build, files GitHub issues, dispatches
Claude Code agents in parallel, tracks state with beads, and merges when CI passes.

No HLD/LLD/research pipeline. Agents reason about approach inline and build directly
from the spec. Docs are generated retroactively from built code.

## Quick Start

```bash
# Install
uv add breadforge  # or: pip install breadforge

# Register your repo
breadforge repo add bread-wood/myproject --local-path ~/dev/myproject

# Run a spec
breadforge run specs/v1.0.0-feature.md --repo bread-wood/myproject

# Run a full campaign
breadforge run specs/campaign.md --repo bread-wood/myproject

# Check status
breadforge status --repo bread-wood/myproject

# Design a new spec interactively
breadforge spec "add order history with export to CSV"
```

## Commands

| Command | Description |
|---------|-------------|
| `breadforge run <spec.md>` | Parse spec, file issues, dispatch agents |
| `breadforge plan <spec.md>` | Seed issues without dispatching |
| `breadforge init --milestone v1.0.0` | Create a GitHub milestone |
| `breadforge status` | Show live bead state table |
| `breadforge beads` | Show all beads for a repo |
| `breadforge monitor` | Run anomaly detection and repair loop |
| `breadforge spec "description"` | Interactive spec-forge |
| `breadforge cost` | Show LLM cost summary |
| `breadforge health` | Preflight health checks |
| `breadforge repo add/list/remove` | Manage platform repo registry |

## Architecture

```
breadforge run spec.md
       │
       ▼
  parse spec → file GitHub issues → seed WorkBeads
       │
       ▼
  RollingDispatcher
  ┌────────────────────────────────────┐
  │  slot 1: agent (issue #10)         │
  │  slot 2: agent (issue #11)         │
  │  slot 3: agent (issue #12)         │
  │                                    │
  │  watchdog: kill hung agents        │
  │  heartbeat: log vitals every N s  │
  └────────────────────────────────────┘
       │
       ▼
  MergeQueue → squash merge → close WorkBead
```

### Bead system

Beads are the canonical source of truth. All state lives in `~/.breadforge/beads/`.

- `WorkBead` — issue lifecycle: `open → claimed → pr_open → merge_ready → closed`
- `PRBead` — PR state: `open → reviewing → merge_ready → merged`
- `MergeQueue` — sequential squash merge ordering
- `CampaignBead` — multi-milestone campaign progress
- `AnomalyBead` — monitor anomalies and repair state

### Assessor / Allocator

Before dispatching each agent, breadforge estimates task complexity and selects
an appropriate model tier:

- `LOW` → cheap model (haiku) — docs, formatting, config changes
- `MEDIUM` → standard model (sonnet) — feature work, tests
- `HIGH` → capable model (opus) — security changes, multi-module coordination

Requires `breadmin-llm`: `pip install breadforge[llm]`

### Monitor

The monitor runs as a background loop detecting:

- `zombie_pr` — PR with CI failing for too long
- `stuck_issue` — claimed issue with no PR after timeout
- `conflict_pr` — PR with merge conflicts
- `stale_label` — `in-progress` label with no matching claimed bead

Auto-repairs stale labels and rebases conflict branches. Dispatches repair agents
for zombie PRs and stuck issues.

### Spec Forge

`breadforge spec "description"` runs an interactive session that:

1. Scans all registered repos' CLAUDE.md files for platform context
2. Conducts a structured interview (repo home, interface, cross-repo deps, unknowns)
3. Drafts a spec file following TEMPLATE.md format
4. Validates required sections are present
5. Checks for architecture violations
6. Updates the platform campaign

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `BREADFORGE_CONCURRENCY` | `3` | Max parallel agents |
| `BREADFORGE_MODEL` | `claude-sonnet-4-6` | Override model for all agents |
| `BREADFORGE_AGENT_TIMEOUT_MINUTES` | `60` | Agent timeout before watchdog kills |
| `BREADFORGE_WATCHDOG_INTERVAL_SECONDS` | `60` | Watchdog check interval |
| `BREADFORGE_MAX_RETRIES` | `3` | Max retries per issue before abandoning |
| `BREADFORGE_BEADS_DIR` | `~/.breadforge/beads` | Bead storage directory |
| `BREADMIN_DB_PATH` | `data/breadmin.db` | breadmin-llm cost database |

## Spec Format

```markdown
# Project vX.Y.Z — Milestone Name

## Overview
What and why. 1-3 paragraphs.

## Success Criteria
- [ ] Measurable acceptance criterion
- [ ] Another criterion

## Scope
### Included
- Concrete deliverable

### Excluded
- Explicit non-goal

## Key Unknowns
- **[P1]** Open question requiring investigation before impl

## Modules
- module-name: one-line description
```

## Multi-repo Campaign

```markdown
# Platform Campaign

\`\`\`bash
breadforge run \
  specs/myproject/v1.0.0-foundation.md \
  specs/myproject/v1.1.0-api.md \
  specs/other-service/v0.1.0-client.md \
  --repo bread-wood/myproject
\`\`\`
```
