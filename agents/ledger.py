"""ledger.py — append-only JSONL cost ledger for breadforge agent runs.

Records per-node token usage and cost after each run_agent call, and
provides per-run and cross-run summary queries.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from breadforge.agents.runner import RunResult


_LEDGER_DIR = Path.home() / ".breadforge" / "runs"


class CostLedger:
    """Append-only JSONL cost ledger.

    Each run produces a `~/.breadforge/runs/{run_id}.jsonl` file.
    Every call to :meth:`append` writes one JSON line with token usage
    and cost for a single node execution.
    """

    def ledger_path(self, run_id: str) -> Path:
        """Return the JSONL ledger path for *run_id*.

        The file is at ``~/.breadforge/runs/{run_id}.jsonl``.
        """
        return _LEDGER_DIR / f"{run_id}.jsonl"

    def append(
        self,
        run_id: str,
        node_id: str,
        model: str,
        result: RunResult,
    ) -> None:
        """Append one cost record for *node_id* to the ledger for *run_id*.

        Each record contains:
        - ``run_id``        — string identifier for the run
        - ``node_id``       — string identifier for the graph node
        - ``model``         — model used for the node
        - ``input_tokens``  — input token count (0 if unavailable)
        - ``output_tokens`` — output token count (0 if unavailable)
        - ``cost_usd``      — estimated cost in USD (0.0 if unavailable)
        - ``timestamp``     — ISO-8601 UTC timestamp

        The ledger file is created (including parent directories) if it
        does not already exist.
        """
        path = self.ledger_path(run_id)
        path.parent.mkdir(parents=True, exist_ok=True)

        record = {
            "run_id": run_id,
            "node_id": node_id,
            "model": model,
            "input_tokens": result.input_tokens or 0,
            "output_tokens": result.output_tokens or 0,
            "cost_usd": result.cost_usd or 0.0,
            "timestamp": datetime.now(UTC).isoformat(),
        }

        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")

    def summarize(self, run_id: str) -> dict:
        """Return aggregated totals for a single run.

        Returns a dict with keys:
        - ``total_cost_usd``      — sum of all ``cost_usd`` values
        - ``total_input_tokens``  — sum of all ``input_tokens`` values
        - ``total_output_tokens`` — sum of all ``output_tokens`` values
        - ``node_count``          — number of records in the ledger

        If no ledger exists for *run_id*, all numeric fields are zero.
        """
        path = self.ledger_path(run_id)
        if not path.exists():
            return {
                "total_cost_usd": 0.0,
                "total_input_tokens": 0,
                "total_output_tokens": 0,
                "node_count": 0,
            }

        total_cost = 0.0
        total_input = 0
        total_output = 0
        count = 0

        with path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                record = json.loads(line)
                total_cost += float(record.get("cost_usd", 0.0))
                total_input += int(record.get("input_tokens", 0))
                total_output += int(record.get("output_tokens", 0))
                count += 1

        return {
            "total_cost_usd": total_cost,
            "total_input_tokens": total_input,
            "total_output_tokens": total_output,
            "node_count": count,
        }

    def summarize_all(self) -> list[dict]:
        """Return per-run summaries for all recorded runs.

        Scans ``~/.breadforge/runs/`` for ``*.jsonl`` files and returns a
        list of summary dicts (same shape as :meth:`summarize`) sorted by
        ``run_id``, each augmented with the ``run_id`` key.

        Returns an empty list if the ledger directory does not exist.
        """
        if not _LEDGER_DIR.exists():
            return []

        summaries = []
        for path in sorted(_LEDGER_DIR.glob("*.jsonl")):
            run_id = path.stem
            summary = self.summarize(run_id)
            summary["run_id"] = run_id
            summaries.append(summary)

        return summaries
