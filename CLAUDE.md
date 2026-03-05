# breadforge — Agent Context

`breadforge` is a Python CLI platform build orchestrator. Source lives in `src/breadforge/`.
Tests live in `tests/`. Default branch: `mainline`.

## Architecture

breadforge is spec-driven, bead-tracked, and multi-repo:

1. **Spec files** describe what to build (one file per milestone)
2. **Beads** track state on disk — canonical source of truth
3. **Rolling dispatcher** fills concurrency slots, runs watchdog, drains merge queue
4. **Assessor/Allocator** selects model tier based on task complexity
5. **Monitor** detects anomalies and dispatches repair agents

No HLD/LLD/research pipeline stages — agents reason about approach inline.
Docs are generated retroactively from built code.

## Module Table

| Label | Allowed filesystem scope |
|-------|--------------------------|
| `mod:beads` | `src/breadforge/beads.py` |
| `mod:config` | `src/breadforge/config.py` |
| `mod:spec` | `src/breadforge/spec.py` |
| `mod:runner` | `src/breadforge/runner.py` |
| `mod:dispatch` | `src/breadforge/dispatch.py` |
| `mod:merge` | `src/breadforge/merge.py` |
| `mod:assessor` | `src/breadforge/assessor.py` |
| `mod:monitor` | `src/breadforge/monitor.py` |
| `mod:forge` | `src/breadforge/forge.py` |
| `mod:cli` | `src/breadforge/cli.py` |
| `mod:health` | `src/breadforge/health.py` |
| `mod:logger` | `src/breadforge/logger.py` |
| `infra` | `pyproject.toml`, `.github/`, `CLAUDE.md`, `README.md` |

## Bead Layout

```
~/.breadforge/beads/<owner>/<repo>/
  work/<N>.json          WorkBead — issue lifecycle
  prs/pr-<N>.json        PRBead — PR state
  merge-queue.json       MergeQueue — sequential merge ordering
  campaign.json          CampaignBead — multi-milestone progress
  anomalies/<id>.json    AnomalyBead — monitor anomalies
  logs/<owner>_<repo>.jsonl  JSONL event log
```

## Bead Invariants

1. Beads lead, GitHub follows. Query beads first.
2. Every tracked issue has a WorkBead before dispatch.
3. Dedup against beads, not GitHub issue list.
4. `in-progress` label mirrors `claimed` bead state.
5. Abandoned beads close open PRs.

## Key Design Decisions

- **No pipeline stages** — no research/HLD/LLD required. Agents reason inline.
- **Retroactive docs** — docs are generated from code, not before code.
- **Multi-model via breadmin-llm** — assessor selects model tier per task.
- **Rolling dispatch** — fills concurrency slots as agents complete.
- **Watchdog** — SIGTERM → SIGKILL for hung agents.
- **Spec-forge** — interactive spec design from natural language description.

## Testing

```bash
uv run pytest                     # all tests
uv run pytest tests/unit/         # unit tests only
uv run pytest tests/integration/  # integration tests
```

## Linting

```bash
uv run ruff check src/ tests/
uv run ruff format --check src/ tests/
```

## Dependency Management

```bash
uv add <package>        # runtime dependency
uv add --dev <package>  # dev dependency
```
