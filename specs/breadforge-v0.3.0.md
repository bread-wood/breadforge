# breadforge v0.3.0 — Reliability + Observability

## Overview

Close the maturity gap between breadforge and brimstone. breadforge currently has no
cost tracking, no error classification, no fallback model on rate-limit, no orchestrator
lock, and no budget cap. Long v0.2.0 runs burned unknown amounts of money silently and
crashed without useful diagnostics. This milestone fixes the infrastructure that makes
breadforge safe to run unattended.

## Success Criteria

- [ ] `breadforge run` stops and reports cost when `--max-budget` is exceeded
- [ ] `breadforge cost` shows per-run and per-milestone USD spend
- [ ] Rate-limit and billing errors are logged with structured error type; run retries with haiku fallback on overload
- [ ] Two concurrent `breadforge run` invocations against the same repo are prevented by an orchestrator lock
- [ ] All existing tests pass; new tests cover the above behaviors

## Scope

### Included

- **Richer RunResult**: parse `stream-json` events from `claude --print --output-format stream-json`; capture `input_tokens`, `output_tokens`, `cost_usd` from `usage` events; classify terminal errors (`rate_limit`, `billing_error`, `auth_failure`, `error_max_turns`) from `result` events
- **Fallback model**: when a `run_agent` call exits with classified `rate_limit` or `overload` error, retry once with `claude-haiku-4-5-20251001`; log the downgrade; count against the same node retry budget
- **Cost ledger**: append-only JSONL at `~/.breadforge/runs/{run-id}.jsonl`; one record per completed `run_agent` call with fields: `run_id`, `node_id`, `model`, `input_tokens`, `output_tokens`, `cost_usd`, `timestamp`; `breadforge cost` reads and summarizes
- **Budget cap**: `GraphExecutor` accepts `max_budget_usd: float | None`; accumulates spend from completed nodes; refuses to dispatch new nodes once cap is exceeded; logs "budget cap reached" and marks remaining pending nodes abandoned
- **Orchestrator lock**: file lock at `~/.breadforge/locks/{owner}-{repo}.lock` using `fcntl.flock`; acquired at `GraphExecutor.run()` start, released at exit; second invocation prints "another breadforge run is active for {repo}" and exits 1

### Excluded

- Per-model pricing table updates (use fixed rates: sonnet=$3/$15 per MTok, haiku=$0.25/$1.25, opus=$15/$75)
- Streaming cost updates in the Rich live display (v0.4.0)
- Budget enforcement mid-agent (cap is checked between node dispatches, not inside a running agent)

## Key Unknowns

- **[P2]** `stream-json` event schema for cost — verify `usage` field name and structure from `claude --print --output-format stream-json --verbose` output before implementing
- **[P2]** `fcntl.flock` vs `filelock` library — determine if `filelock` is already a transitive dep; use it if present, otherwise `fcntl` (Unix-only is acceptable for now)

## Modules

- **runner**: `agents/runner.py` — `RunResult` enrichment, cost parsing, error classification, fallback model retry
- **ledger**: `agents/ledger.py` (new) — `CostLedger`, append-only JSONL writer, `breadforge cost` data model
- **executor**: `graph/executor.py` — budget cap enforcement, lock acquisition/release
- **lock**: `graph/lock.py` (new) — `OrchestratorLock` context manager using `fcntl.flock`
- **cli**: `cli.py` — `--max-budget` flag on `run`, new `breadforge cost` command

## Dependencies

- runner before ledger (ledger reads enriched RunResult)
- ledger before executor (executor writes to ledger on node completion)
- lock before executor (executor acquires lock)
- all of the above before cli (cli wires flags to executor)
