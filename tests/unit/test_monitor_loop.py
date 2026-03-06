"""Unit tests for monitor/loop.py — run_monitor."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from breadforge.beads import BeadStore
from breadforge.config import Config
from breadforge.logger import Logger
from breadforge.monitor.anomaly import AnomalyBead, AnomalyKind, AnomalyStore


@pytest.fixture
def config(tmp_path: Path) -> Config:
    cfg = Config.from_env("owner/repo")
    cfg.beads_dir = tmp_path / "beads"
    return cfg


@pytest.fixture
def store(tmp_path: Path) -> BeadStore:
    return BeadStore(tmp_path / "beads", "owner/repo")


@pytest.fixture
def logger(tmp_path: Path) -> Logger:
    return Logger(tmp_path / "test.jsonl", run_id="test")


class TestRunMonitor:
    def test_once_no_anomalies(self, store: BeadStore, config: Config, logger: Logger) -> None:
        from breadforge.monitor.loop import run_monitor

        with patch("breadforge.monitor.detect._gh") as mock_gh:
            mock_gh.return_value = MagicMock(returncode=0, stdout="[]")
            asyncio.run(run_monitor(store, config, logger, once=True, interval_seconds=0))
        # Should complete without error

    def test_once_dry_run_logs_but_does_not_repair(
        self, store: BeadStore, config: Config, logger: Logger, tmp_path: Path
    ) -> None:
        import json
        from datetime import UTC, datetime, timedelta

        from breadforge.beads.types import WorkBead
        from breadforge.monitor.loop import run_monitor

        # Inject a stuck issue
        bead = WorkBead(issue_number=10, repo="owner/repo", title="Stuck")
        bead.state = "claimed"  # type: ignore
        data = bead.model_dump(mode="json")
        data["updated_at"] = (datetime.now(UTC) - timedelta(minutes=300)).isoformat()
        (store._work_dir / "10.json").write_text(json.dumps(data))

        repair_auto = AsyncMock()
        repair_agent = AsyncMock()
        with patch("breadforge.monitor.detect._gh") as mock_gh:
            mock_gh.return_value = MagicMock(returncode=0, stdout="[]")
            with (
                patch("breadforge.monitor.loop._repair_auto", repair_auto),
                patch("breadforge.monitor.loop._repair_agent", repair_agent),
            ):
                asyncio.run(
                    run_monitor(store, config, logger, once=True, dry_run=True, interval_seconds=0)
                )

        repair_auto.assert_not_called()
        repair_agent.assert_not_called()

    def test_once_repairs_auto_tier(
        self, store: BeadStore, config: Config, logger: Logger, tmp_path: Path
    ) -> None:
        from breadforge.monitor.loop import run_monitor

        # Pre-populate an anomaly bead requiring auto repair
        astore = AnomalyStore(config.beads_dir, config.repo)
        anomaly = AnomalyBead(
            anomaly_id="conflict-100-999",
            repo="owner/repo",
            kind=AnomalyKind.CONFLICT_PR,
            pr_number=100,
            issue_number=1,
            branch="1-b",
            repair_tier="auto",
        )
        astore.write(anomaly)

        repair_auto = AsyncMock()
        repair_agent = AsyncMock()
        with patch("breadforge.monitor.detect._gh") as mock_gh:
            mock_gh.return_value = MagicMock(returncode=0, stdout="[]")
            with (
                patch("breadforge.monitor.loop._repair_auto", repair_auto),
                patch("breadforge.monitor.loop._repair_agent", repair_agent),
            ):
                asyncio.run(
                    run_monitor(store, config, logger, once=True, dry_run=False, interval_seconds=0)
                )

        repair_auto.assert_called_once()
        repair_agent.assert_not_called()

    def test_once_repairs_agent_tier(
        self, store: BeadStore, config: Config, logger: Logger, tmp_path: Path
    ) -> None:
        from breadforge.monitor.loop import run_monitor

        astore = AnomalyStore(config.beads_dir, config.repo)
        anomaly = AnomalyBead(
            anomaly_id="stuck-5-999",
            repo="owner/repo",
            kind=AnomalyKind.STUCK_ISSUE,
            issue_number=5,
            repair_tier="agent",
        )
        astore.write(anomaly)

        repair_auto = AsyncMock()
        repair_agent = AsyncMock()
        with patch("breadforge.monitor.detect._gh") as mock_gh:
            mock_gh.return_value = MagicMock(returncode=0, stdout="[]")
            with (
                patch("breadforge.monitor.loop._repair_auto", repair_auto),
                patch("breadforge.monitor.loop._repair_agent", repair_agent),
            ):
                asyncio.run(
                    run_monitor(store, config, logger, once=True, dry_run=False, interval_seconds=0)
                )

        repair_agent.assert_called_once()
        repair_auto.assert_not_called()

    def test_max_repair_attempts_skips_anomaly(
        self, store: BeadStore, config: Config, logger: Logger, tmp_path: Path
    ) -> None:
        from breadforge.monitor.loop import run_monitor

        astore = AnomalyStore(config.beads_dir, config.repo)
        anomaly = AnomalyBead(
            anomaly_id="stuck-6-999",
            repo="owner/repo",
            kind=AnomalyKind.STUCK_ISSUE,
            issue_number=6,
            repair_tier="agent",
            repair_attempts=5,  # exceeds max_repair_attempts=3
        )
        astore.write(anomaly)

        repair_agent = AsyncMock()
        with patch("breadforge.monitor.detect._gh") as mock_gh:
            mock_gh.return_value = MagicMock(returncode=0, stdout="[]")
            with patch("breadforge.monitor.loop._repair_agent", repair_agent):
                asyncio.run(
                    run_monitor(
                        store,
                        config,
                        logger,
                        once=True,
                        dry_run=False,
                        max_repair_attempts=3,
                        interval_seconds=0,
                    )
                )

        repair_agent.assert_not_called()

    def test_deduplicates_existing_anomalies(
        self, store: BeadStore, config: Config, logger: Logger, tmp_path: Path
    ) -> None:
        """Same kind+issue anomaly already in store should not be added again."""
        import json
        from datetime import UTC, datetime, timedelta

        from breadforge.beads.types import WorkBead
        from breadforge.monitor.loop import run_monitor

        astore = AnomalyStore(config.beads_dir, config.repo)
        existing = AnomalyBead(
            anomaly_id="stuck-10-111",
            repo="owner/repo",
            kind=AnomalyKind.STUCK_ISSUE,
            issue_number=10,
            repair_tier="agent",
        )
        astore.write(existing)

        # Inject same stuck issue again
        bead = WorkBead(issue_number=10, repo="owner/repo", title="Stuck")
        bead.state = "claimed"  # type: ignore
        data = bead.model_dump(mode="json")
        data["updated_at"] = (datetime.now(UTC) - timedelta(minutes=300)).isoformat()
        (store._work_dir / "10.json").write_text(json.dumps(data))

        repair_agent = AsyncMock()
        with patch("breadforge.monitor.detect._gh") as mock_gh:
            mock_gh.return_value = MagicMock(returncode=0, stdout="[]")
            with patch("breadforge.monitor.loop._repair_agent", repair_agent):
                asyncio.run(
                    run_monitor(store, config, logger, once=True, dry_run=True, interval_seconds=0)
                )

        # Should only have one anomaly (the existing one), not two
        open_anomalies = astore.list_open()
        stuck = [
            a for a in open_anomalies if a.kind == AnomalyKind.STUCK_ISSUE and a.issue_number == 10
        ]
        assert len(stuck) == 1
