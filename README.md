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
| `breadforge run-issue --issue N` | Dispatch a single issue (used by GHA) |
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
  GraphExecutor (async DAG)
  ┌────────────────────────────────────────┐
  │  plan node → expands build nodes      │
  │  research node → Gemini / GPT-4.1     │
  │  build node → Claude agent            │
  │  wait node → blocks on cross-repo dep │
  │  consensus node → votes on proposals  │
  │  design_doc node → LLM design output  │
  │                                        │
  │  concurrency=3  watchdog=60s           │
  └────────────────────────────────────────┘
       │
       ▼
  MergeQueue → squash merge → close WorkBead
```

### DAG Executor

The `GraphExecutor` drives an async event loop over an `ExecutionGraph` DAG. Key properties:

- **Dynamic expansion**: `plan` nodes emit new build/merge/readme nodes at runtime; the executor wires overlap edges between build nodes touching the same files.
- **Crash recovery**: nodes found in `running` state on restart are handed to the handler's `recover()` method before re-dispatching.
- **Dry-run mode**: skips build/merge dispatch; creates `WorkBead`s so the plan can be reviewed before agents run.
- **BackendRouter**: routes node types to LLM backends — `research`/`plan` nodes to `research_model` (Gemini or GPT-4.1), `build`/`merge`/`readme` nodes to `build_model` (Claude), `wait`/`consensus`/`design_doc` to `design_model`.

### Node Types

| Type | Handler | Description |
|------|---------|-------------|
| `plan` | `PlanHandler` | LLM-driven planning; expands graph with build nodes |
| `research` | `ResearchHandler` | Investigation node; routes to configurable backend |
| `build` | `BuildHandler` | Dispatches a Claude Code agent for one issue |
| `merge` | `MergeHandler` | Squash-merges a PR after CI passes |
| `readme` | `ReadmeHandler` | Generates or updates module README |
| `wait` | `WaitHandler` | Polls until a cross-repo milestone ships |
| `consensus` | `ConsensusHandler` | Selects the best proposal from upstream nodes |
| `design_doc` | `DesignDocHandler` | Generates a design document via LLM |

### Bead System

Beads are the canonical source of truth. All state lives in `~/.breadforge/beads/`.

- `WorkBead` — issue lifecycle: `open → claimed → pr_open → merge_ready → closed`
- `PRBead` — PR state: `open → reviewing → merge_ready → merged`
- `MergeQueue` — sequential squash merge ordering
- `CampaignBead` — multi-milestone campaign progress; carries `blocked_by` for cross-repo deps
- `AnomalyBead` — monitor anomalies and repair state

### Multi-Backend Support

Research and plan nodes can be routed to alternative LLM backends:

- `anthropic` (default) — uses `run_agent` subprocess via Claude
- `gemini` — Google Gemini via `GeminiBackend`
- `openai` — GPT-4.1 via `OpenAIBackend`

Configure via `BREADFORGE_RESEARCH_BACKEND` / `BREADFORGE_PLAN_BACKEND`.

### Credential Proxy

The loopback credential proxy (`breadforge.proxy`) prevents raw API key injection into
agent subprocesses. It starts an HTTP server on `127.0.0.1` at a random port, issues
scoped HMAC tokens (one per node, scoped to `anthropic`/`openai`/`google`), validates
tokens on each request, and forwards traffic to the real upstream API with the real key
injected server-side.

### Cross-Repo Blocking

Declare `blocked_by: ["owner/otherrepo:v1"]` in a `CampaignBead` milestone plan. The
graph builder inserts `wait` nodes that poll until the upstream milestone status reaches
`"shipped"` before the plan node is allowed to run.

### GitHub Actions Integration

`.github/workflows/pipeline.yml` triggers `breadforge run-issue` automatically when the
`stage/impl` label is added to a milestoned issue:

```yaml
on:
  issues:
    types: [labeled]
```

### Assessor / Allocator

Before dispatching each agent, breadforge estimates task complexity and selects
an appropriate model tier:

- `LOW` → cheap model (haiku) — docs, formatting, config changes
- `MEDIUM` → standard model (sonnet) — feature work, tests
- `HIGH` → capable model (opus) — security changes, multi-module coordination

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
| `BREADFORGE_MODEL` | `claude-sonnet-4-6` | Default model for build/merge nodes |
| `BREADFORGE_RESEARCH_BACKEND` | `anthropic` | Backend for research nodes (`anthropic`/`gemini`/`openai`) |
| `BREADFORGE_PLAN_BACKEND` | `anthropic` | Backend for plan nodes |
| `BREADFORGE_RESEARCH_MODEL` | _(backend default)_ | Model override for research nodes |
| `BREADFORGE_PLAN_MODEL` | _(backend default)_ | Model override for plan nodes |
| `BREADFORGE_BUILD_MODEL` | `claude-sonnet-4-6` | Model for build/merge/readme nodes |
| `BREADFORGE_AGENT_TIMEOUT_MINUTES` | `60` | Agent timeout before watchdog kills |
| `BREADFORGE_WATCHDOG_INTERVAL_SECONDS` | `60` | Watchdog check interval |
| `BREADFORGE_MAX_RETRIES` | `3` | Max retries per node before abandoning |
| `BREADFORGE_BEADS_DIR` | `~/.breadforge/beads` | Bead storage directory |
| `BREADFORGE_GH_TOKEN` | — | GitHub token forwarded to build agents |
| `BREADFORGE_PROXY_SECRET` | _(ephemeral)_ | HMAC secret for credential proxy tokens |
| `ANTHROPIC_API_KEY` | — | Required for build/merge/readme nodes |
| `OPENAI_API_KEY` | — | Required when `research_backend=openai` |
| `GOOGLE_API_KEY` | — | Required when `research_backend=gemini` |

## Module Overview

| Module | Description |
|--------|-------------|
| `breadforge.cli` | Typer CLI; entry point for all commands including `run-issue` |
| `breadforge.config` | Runtime `Config` dataclass and platform repo `Registry` |
| `breadforge.spec` | Spec and campaign file parsing |
| `breadforge.graph.executor` | `ExecutionGraph` and async `GraphExecutor` DAG engine |
| `breadforge.graph.builder` | Graph construction helpers and cross-repo blocking wiring |
| `breadforge.graph.node` | `GraphNode`, `NodeHandler` protocol, `BackendRouter`, `CredentialProxy` facade |
| `breadforge.graph.handlers` | One handler per node type: build, merge, plan, research, readme, wait, consensus, design_doc |
| `breadforge.backends` | Pluggable LLM backends: `AnthropicBackend`, `GeminiBackend`, `OpenAIBackend` |
| `breadforge.proxy` | Loopback credential proxy server and HMAC token issuance/validation |
| `breadforge.beads` | `BeadStore` and bead types (`WorkBead`, `PRBead`, `CampaignBead`, …) |
| `breadforge.agents` | Agent runner and prompt templates |
| `breadforge.monitor` | Anomaly detection, repair loop, and watchdog |
| `breadforge.forge` | Interactive spec-forge (interview, draft, validate) |
| `breadforge.health` | Preflight health checks |
| `breadforge.logger` | Structured logger |

## Tests

```bash
uv sync --group dev
uv run pytest
uv run ruff check src tests
```

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
