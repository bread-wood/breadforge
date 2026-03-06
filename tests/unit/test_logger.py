"""Unit tests for Logger — JSONL structured event logger."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from breadforge.logger import Logger


@pytest.fixture
def log_path(tmp_path: Path) -> Path:
    return tmp_path / "logs" / "test.jsonl"


@pytest.fixture
def logger(log_path: Path) -> Logger:
    return Logger(log_path, run_id="test-run-001")


def _read_records(log_path: Path) -> list[dict]:
    return [json.loads(line) for line in log_path.read_text().splitlines() if line.strip()]


class TestLoggerInit:
    def test_creates_parent_dirs(self, tmp_path: Path) -> None:
        path = tmp_path / "deep" / "nested" / "logs" / "test.jsonl"
        Logger(path)
        assert path.parent.exists()

    def test_run_id_set(self, log_path: Path) -> None:
        lg = Logger(log_path, run_id="my-run")
        assert lg.run_id == "my-run"

    def test_run_id_auto_generated(self, log_path: Path) -> None:
        lg = Logger(log_path)
        assert lg.run_id  # non-empty UUID


class TestLoggerEvents:
    def test_heartbeat(self, logger: Logger, log_path: Path) -> None:
        logger.heartbeat(active_agents=2, queue_depth=5, completed=3, cost_usd=0.42)
        records = _read_records(log_path)
        assert len(records) == 1
        r = records[0]
        assert r["event"] == "heartbeat"
        assert r["run_id"] == "test-run-001"
        assert r["active_agents"] == 2
        assert r["queue_depth"] == 5
        assert r["completed"] == 3
        assert r["cost_usd"] == 0.42
        assert "ts" in r

    def test_dispatch(self, logger: Logger, log_path: Path) -> None:
        logger.dispatch(
            issue_number=10,
            branch="10-feat",
            model="claude-sonnet-4-6",
            tier="build",
            upgraded=False,
        )
        records = _read_records(log_path)
        r = records[0]
        assert r["event"] == "dispatch"
        assert r["issue_number"] == 10
        assert r["branch"] == "10-feat"
        assert r["model"] == "claude-sonnet-4-6"
        assert r["tier"] == "build"
        assert r["upgraded"] is False

    def test_dispatch_defaults(self, logger: Logger, log_path: Path) -> None:
        logger.dispatch(issue_number=5, branch="5-fix", model="claude-haiku-4-5-20251001")
        records = _read_records(log_path)
        r = records[0]
        assert r["tier"] is None
        assert r["upgraded"] is False

    def test_agent_complete(self, logger: Logger, log_path: Path) -> None:
        logger.agent_complete(
            issue_number=7, branch="7-b", exit_code=0, duration_ms=1234.5, pr_number=42
        )
        records = _read_records(log_path)
        r = records[0]
        assert r["event"] == "agent_complete"
        assert r["exit_code"] == 0
        assert r["duration_ms"] == 1234.5
        assert r["pr_number"] == 42

    def test_agent_complete_no_pr(self, logger: Logger, log_path: Path) -> None:
        logger.agent_complete(issue_number=8, branch="8-b", exit_code=1, duration_ms=500.0)
        r = _read_records(log_path)[0]
        assert r["pr_number"] is None

    def test_merge(self, logger: Logger, log_path: Path) -> None:
        logger.merge(pr_number=99, issue_number=10, branch="10-feat")
        r = _read_records(log_path)[0]
        assert r["event"] == "merge"
        assert r["pr_number"] == 99
        assert r["issue_number"] == 10
        assert r["branch"] == "10-feat"

    def test_error(self, logger: Logger, log_path: Path) -> None:
        logger.error("something broke", node_id="build-a")
        r = _read_records(log_path)[0]
        assert r["event"] == "error"
        assert r["message"] == "something broke"
        assert r["node_id"] == "build-a"

    def test_info(self, logger: Logger, log_path: Path) -> None:
        logger.info("node started", node_id="plan")
        r = _read_records(log_path)[0]
        assert r["event"] == "info"
        assert r["message"] == "node started"

    def test_watchdog_kill(self, logger: Logger, log_path: Path) -> None:
        logger.watchdog_kill(issue_number=3, branch="3-b", reason="timeout")
        r = _read_records(log_path)[0]
        assert r["event"] == "watchdog_kill"
        assert r["reason"] == "timeout"

    def test_anomaly(self, logger: Logger, log_path: Path) -> None:
        logger.anomaly(anomaly_id="stuck-10-999", kind="stuck_issue", issue_number=10)
        r = _read_records(log_path)[0]
        assert r["event"] == "anomaly"
        assert r["anomaly_id"] == "stuck-10-999"
        assert r["kind"] == "stuck_issue"
        assert r["issue_number"] == 10

    def test_anomaly_no_issue(self, logger: Logger, log_path: Path) -> None:
        logger.anomaly(anomaly_id="stale-5-999", kind="stale_label")
        r = _read_records(log_path)[0]
        assert r["issue_number"] is None

    def test_repair(self, logger: Logger, log_path: Path) -> None:
        logger.repair(
            anomaly_id="stuck-10-999", issue_number=10, action="re-dispatch", pr_number=55
        )
        r = _read_records(log_path)[0]
        assert r["event"] == "repair"
        assert r["action"] == "re-dispatch"
        assert r["pr_number"] == 55

    def test_repair_no_pr(self, logger: Logger, log_path: Path) -> None:
        logger.repair(anomaly_id="stale-5-999", issue_number=None, action="remove-label")
        r = _read_records(log_path)[0]
        assert r["pr_number"] is None

    def test_node_dispatch(self, logger: Logger, log_path: Path) -> None:
        logger.node_dispatch(node_id="v1-plan", node_type="plan", model="claude-sonnet-4-6")
        r = _read_records(log_path)[0]
        assert r["event"] == "node_dispatch"
        assert r["node_id"] == "v1-plan"
        assert r["node_type"] == "plan"
        assert r["model"] == "claude-sonnet-4-6"

    def test_node_dispatch_no_model(self, logger: Logger, log_path: Path) -> None:
        logger.node_dispatch(node_id="v1-merge", node_type="merge")
        r = _read_records(log_path)[0]
        assert r["model"] is None

    def test_node_done(self, logger: Logger, log_path: Path) -> None:
        logger.node_done(node_id="v1-build-core", node_type="build", duration_ms=2500.0)
        r = _read_records(log_path)[0]
        assert r["event"] == "node_done"
        assert r["duration_ms"] == 2500.0

    def test_node_done_default_duration(self, logger: Logger, log_path: Path) -> None:
        logger.node_done(node_id="v1-plan", node_type="plan")
        r = _read_records(log_path)[0]
        assert r["duration_ms"] == 0.0

    def test_node_failed(self, logger: Logger, log_path: Path) -> None:
        logger.node_failed(node_id="v1-build-auth", node_type="build", error="clone failed")
        r = _read_records(log_path)[0]
        assert r["event"] == "node_failed"
        assert r["error"] == "clone failed"

    def test_cost(self, logger: Logger, log_path: Path) -> None:
        logger.cost(provider="anthropic", model="claude-sonnet-4-6", cost_usd=0.05, caller="plan")
        r = _read_records(log_path)[0]
        assert r["event"] == "cost"
        assert r["provider"] == "anthropic"
        assert r["cost_usd"] == 0.05
        assert r["caller"] == "plan"

    def test_cost_no_caller(self, logger: Logger, log_path: Path) -> None:
        logger.cost(provider="openai", model="gpt-4.1", cost_usd=0.02)
        r = _read_records(log_path)[0]
        assert r["caller"] is None

    def test_multiple_events_appended(self, logger: Logger, log_path: Path) -> None:
        logger.info("first")
        logger.info("second")
        logger.info("third")
        records = _read_records(log_path)
        assert len(records) == 3
        assert [r["message"] for r in records] == ["first", "second", "third"]

    def test_all_records_have_run_id(self, logger: Logger, log_path: Path) -> None:
        logger.info("a")
        logger.error("b")
        logger.heartbeat(1, 0, 0, 0.0)
        for r in _read_records(log_path):
            assert r["run_id"] == "test-run-001"

    def test_valid_jsonl(self, logger: Logger, log_path: Path) -> None:
        logger.info("test")
        logger.dispatch(1, "1-b", "model")
        lines = log_path.read_text().splitlines()
        for line in lines:
            json.loads(line)  # must not raise
