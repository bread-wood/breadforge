"""JSONL structured logger with heartbeat support."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4


class Logger:
    """Append-only JSONL event logger."""

    def __init__(self, log_path: Path, run_id: str | None = None) -> None:
        self._path = log_path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self.run_id = run_id or str(uuid4())

    def _write(self, event: str, data: dict[str, Any]) -> None:
        record = {
            "event": event,
            "run_id": self.run_id,
            "ts": datetime.now(UTC).isoformat(),
            **data,
        }
        with open(self._path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")

    def heartbeat(
        self,
        active_agents: int,
        queue_depth: int,
        completed: int,
        cost_usd: float,
    ) -> None:
        self._write(
            "heartbeat",
            {
                "active_agents": active_agents,
                "queue_depth": queue_depth,
                "completed": completed,
                "cost_usd": cost_usd,
            },
        )

    def dispatch(
        self,
        issue_number: int,
        branch: str,
        model: str,
        tier: str | None = None,
        upgraded: bool = False,
    ) -> None:
        self._write(
            "dispatch",
            {
                "issue_number": issue_number,
                "branch": branch,
                "model": model,
                "tier": tier,
                "upgraded": upgraded,
            },
        )

    def agent_complete(
        self,
        issue_number: int,
        branch: str,
        exit_code: int,
        duration_ms: float,
        pr_number: int | None = None,
    ) -> None:
        self._write(
            "agent_complete",
            {
                "issue_number": issue_number,
                "branch": branch,
                "exit_code": exit_code,
                "duration_ms": duration_ms,
                "pr_number": pr_number,
            },
        )

    def merge(self, pr_number: int, issue_number: int, branch: str) -> None:
        self._write(
            "merge",
            {
                "pr_number": pr_number,
                "issue_number": issue_number,
                "branch": branch,
            },
        )

    def error(self, message: str, **kwargs: Any) -> None:
        self._write("error", {"message": message, **kwargs})

    def info(self, message: str, **kwargs: Any) -> None:
        self._write("info", {"message": message, **kwargs})

    def watchdog_kill(self, issue_number: int, branch: str, reason: str) -> None:
        self._write(
            "watchdog_kill",
            {
                "issue_number": issue_number,
                "branch": branch,
                "reason": reason,
            },
        )

    def anomaly(self, anomaly_id: str, kind: str, issue_number: int | None = None) -> None:
        self._write(
            "anomaly",
            {
                "anomaly_id": anomaly_id,
                "kind": kind,
                "issue_number": issue_number,
            },
        )

    def repair(
        self,
        anomaly_id: str,
        issue_number: int | None,
        action: str,
        pr_number: int | None = None,
    ) -> None:
        self._write(
            "repair",
            {
                "anomaly_id": anomaly_id,
                "issue_number": issue_number,
                "action": action,
                "pr_number": pr_number,
            },
        )

    def cost(self, provider: str, model: str, cost_usd: float, caller: str | None = None) -> None:
        self._write(
            "cost",
            {
                "provider": provider,
                "model": model,
                "cost_usd": cost_usd,
                "caller": caller,
            },
        )
