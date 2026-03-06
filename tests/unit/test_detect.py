"""Unit tests for monitor/detect.py — anomaly detection logic."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from breadforge.beads import BeadStore, PRBead, WorkBead
from breadforge.monitor import AnomalyKind, _detect_anomalies


@pytest.fixture
def store(tmp_path: Path) -> BeadStore:
    return BeadStore(tmp_path / "beads", "owner/repo")


def _write_old_work_bead(store: BeadStore, issue_number: int, age_minutes: int, pr_number=None) -> None:
    """Write a work bead with updated_at backdated by age_minutes."""
    bead = WorkBead(issue_number=issue_number, repo="owner/repo", title=f"Issue {issue_number}")
    bead.state = "claimed"  # type: ignore
    bead.pr_number = pr_number
    data = bead.model_dump(mode="json")
    data["updated_at"] = (datetime.now(UTC) - timedelta(minutes=age_minutes)).isoformat()
    (store._work_dir / f"{issue_number}.json").write_text(json.dumps(data))


def _write_old_pr_bead(store: BeadStore, pr_number: int, issue_number: int, age_minutes: int, state: str = "open") -> None:
    bead = PRBead(pr_number=pr_number, repo="owner/repo", issue_number=issue_number, branch=f"{issue_number}-branch")
    bead.state = state  # type: ignore
    data = bead.model_dump(mode="json")
    data["updated_at"] = (datetime.now(UTC) - timedelta(minutes=age_minutes)).isoformat()
    (store._prs_dir / f"pr-{pr_number}.json").write_text(json.dumps(data))


def _mock_gh_empty():
    m = MagicMock()
    m.returncode = 0
    m.stdout = "[]"
    return m


# ---------------------------------------------------------------------------
# Stuck issue detection
# ---------------------------------------------------------------------------


class TestStuckIssueDetection:
    def test_detects_stuck_issue(self, store: BeadStore) -> None:
        _write_old_work_bead(store, issue_number=10, age_minutes=200)
        with patch("breadforge.monitor.detect._gh", return_value=_mock_gh_empty()):
            anomalies = _detect_anomalies(store, "owner/repo", stuck_minutes=60)
        stuck = [a for a in anomalies if a.kind == AnomalyKind.STUCK_ISSUE]
        assert len(stuck) == 1
        assert stuck[0].issue_number == 10
        assert stuck[0].repair_tier == "agent"

    def test_fresh_claimed_not_stuck(self, store: BeadStore) -> None:
        bead = WorkBead(issue_number=11, repo="owner/repo", title="Fresh")
        bead.state = "claimed"  # type: ignore
        store.write_work_bead(bead)
        with patch("breadforge.monitor.detect._gh", return_value=_mock_gh_empty()):
            anomalies = _detect_anomalies(store, "owner/repo", stuck_minutes=120)
        stuck = [a for a in anomalies if a.kind == AnomalyKind.STUCK_ISSUE]
        assert len(stuck) == 0

    def test_claimed_with_pr_not_stuck(self, store: BeadStore) -> None:
        _write_old_work_bead(store, issue_number=12, age_minutes=200, pr_number=99)
        with patch("breadforge.monitor.detect._gh", return_value=_mock_gh_empty()):
            anomalies = _detect_anomalies(store, "owner/repo", stuck_minutes=60)
        stuck = [a for a in anomalies if a.kind == AnomalyKind.STUCK_ISSUE]
        assert len(stuck) == 0

    def test_not_claimed_not_stuck(self, store: BeadStore) -> None:
        bead = WorkBead(issue_number=13, repo="owner/repo", title="Open")
        store.write_work_bead(bead)
        with patch("breadforge.monitor.detect._gh", return_value=_mock_gh_empty()):
            anomalies = _detect_anomalies(store, "owner/repo", stuck_minutes=0)
        stuck = [a for a in anomalies if a.kind == AnomalyKind.STUCK_ISSUE]
        assert len(stuck) == 0

    def test_multiple_stuck_issues(self, store: BeadStore) -> None:
        for i in range(3):
            _write_old_work_bead(store, issue_number=20 + i, age_minutes=300)
        with patch("breadforge.monitor.detect._gh", return_value=_mock_gh_empty()):
            anomalies = _detect_anomalies(store, "owner/repo", stuck_minutes=60)
        stuck = [a for a in anomalies if a.kind == AnomalyKind.STUCK_ISSUE]
        assert len(stuck) == 3


# ---------------------------------------------------------------------------
# Zombie PR detection
# ---------------------------------------------------------------------------


class TestZombiePRDetection:
    def test_detects_zombie_pr_checkrun_failure(self, store: BeadStore) -> None:
        _write_old_pr_bead(store, pr_number=100, issue_number=1, age_minutes=90)
        ci_data = json.dumps({
            "statusCheckRollup": [
                {"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "FAILURE"}
            ]
        })
        with patch("breadforge.monitor.detect._gh") as mock_gh:
            mock_gh.return_value = MagicMock(returncode=0, stdout=ci_data)
            anomalies = _detect_anomalies(store, "owner/repo", zombie_minutes=60)
        zombie = [a for a in anomalies if a.kind == AnomalyKind.ZOMBIE_PR]
        assert len(zombie) >= 1
        assert zombie[0].pr_number == 100

    def test_detects_zombie_pr_timed_out(self, store: BeadStore) -> None:
        _write_old_pr_bead(store, pr_number=101, issue_number=2, age_minutes=90)
        ci_data = json.dumps({
            "statusCheckRollup": [
                {"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "TIMED_OUT"}
            ]
        })
        with patch("breadforge.monitor.detect._gh") as mock_gh:
            mock_gh.return_value = MagicMock(returncode=0, stdout=ci_data)
            anomalies = _detect_anomalies(store, "owner/repo", zombie_minutes=60)
        zombie = [a for a in anomalies if a.kind == AnomalyKind.ZOMBIE_PR and a.pr_number == 101]
        assert len(zombie) == 1

    def test_detects_zombie_pr_status_context_failure(self, store: BeadStore) -> None:
        _write_old_pr_bead(store, pr_number=102, issue_number=3, age_minutes=90)
        ci_data = json.dumps({
            "statusCheckRollup": [{"state": "FAILURE"}]
        })
        with patch("breadforge.monitor.detect._gh") as mock_gh:
            mock_gh.return_value = MagicMock(returncode=0, stdout=ci_data)
            anomalies = _detect_anomalies(store, "owner/repo", zombie_minutes=60)
        zombie = [a for a in anomalies if a.kind == AnomalyKind.ZOMBIE_PR and a.pr_number == 102]
        assert len(zombie) == 1

    def test_fresh_pr_not_zombie(self, store: BeadStore) -> None:
        bead = PRBead(pr_number=200, repo="owner/repo", issue_number=5, branch="5-b")
        store.write_pr_bead(bead)
        with patch("breadforge.monitor.detect._gh", return_value=_mock_gh_empty()):
            anomalies = _detect_anomalies(store, "owner/repo", zombie_minutes=60)
        zombie = [a for a in anomalies if a.kind == AnomalyKind.ZOMBIE_PR]
        assert len(zombie) == 0

    def test_merged_pr_not_zombie(self, store: BeadStore) -> None:
        _write_old_pr_bead(store, pr_number=201, issue_number=6, age_minutes=200, state="merged")
        with patch("breadforge.monitor.detect._gh", return_value=_mock_gh_empty()):
            anomalies = _detect_anomalies(store, "owner/repo", zombie_minutes=60)
        zombie = [a for a in anomalies if a.kind == AnomalyKind.ZOMBIE_PR]
        assert len(zombie) == 0

    def test_abandoned_pr_not_zombie(self, store: BeadStore) -> None:
        _write_old_pr_bead(store, pr_number=202, issue_number=7, age_minutes=200, state="abandoned")
        with patch("breadforge.monitor.detect._gh", return_value=_mock_gh_empty()):
            anomalies = _detect_anomalies(store, "owner/repo", zombie_minutes=60)
        zombie = [a for a in anomalies if a.kind == AnomalyKind.ZOMBIE_PR]
        assert len(zombie) == 0

    def test_zombie_passing_ci_not_flagged(self, store: BeadStore) -> None:
        _write_old_pr_bead(store, pr_number=203, issue_number=8, age_minutes=200)
        ci_data = json.dumps({
            "statusCheckRollup": [
                {"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "SUCCESS"}
            ]
        })
        with patch("breadforge.monitor.detect._gh") as mock_gh:
            mock_gh.return_value = MagicMock(returncode=0, stdout=ci_data)
            anomalies = _detect_anomalies(store, "owner/repo", zombie_minutes=60)
        zombie = [a for a in anomalies if a.kind == AnomalyKind.ZOMBIE_PR and a.pr_number == 203]
        assert len(zombie) == 0

    def test_zombie_empty_ci_not_flagged(self, store: BeadStore) -> None:
        _write_old_pr_bead(store, pr_number=204, issue_number=9, age_minutes=200)
        ci_data = json.dumps({"statusCheckRollup": []})
        with patch("breadforge.monitor.detect._gh") as mock_gh:
            mock_gh.return_value = MagicMock(returncode=0, stdout=ci_data)
            anomalies = _detect_anomalies(store, "owner/repo", zombie_minutes=60)
        zombie = [a for a in anomalies if a.kind == AnomalyKind.ZOMBIE_PR and a.pr_number == 204]
        assert len(zombie) == 0

    def test_zombie_bad_json_skipped(self, store: BeadStore) -> None:
        _write_old_pr_bead(store, pr_number=205, issue_number=10, age_minutes=200)
        with patch("breadforge.monitor.detect._gh") as mock_gh:
            mock_gh.return_value = MagicMock(returncode=0, stdout="not-json")
            anomalies = _detect_anomalies(store, "owner/repo", zombie_minutes=60)
        # Must not raise; just skip the anomaly
        zombie = [a for a in anomalies if a.kind == AnomalyKind.ZOMBIE_PR]
        assert len(zombie) == 0

    def test_startup_failure_is_zombie(self, store: BeadStore) -> None:
        _write_old_pr_bead(store, pr_number=206, issue_number=11, age_minutes=200)
        ci_data = json.dumps({
            "statusCheckRollup": [
                {"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "STARTUP_FAILURE"}
            ]
        })
        with patch("breadforge.monitor.detect._gh") as mock_gh:
            mock_gh.return_value = MagicMock(returncode=0, stdout=ci_data)
            anomalies = _detect_anomalies(store, "owner/repo", zombie_minutes=60)
        zombie = [a for a in anomalies if a.kind == AnomalyKind.ZOMBIE_PR and a.pr_number == 206]
        assert len(zombie) == 1


# ---------------------------------------------------------------------------
# Conflict PR detection
# ---------------------------------------------------------------------------


class TestConflictPRDetection:
    def test_detects_conflicting_pr(self, store: BeadStore) -> None:
        bead = PRBead(pr_number=300, repo="owner/repo", issue_number=20, branch="20-b")
        store.write_pr_bead(bead)
        with patch("breadforge.monitor.detect._gh") as mock_gh:
            mock_gh.return_value = MagicMock(returncode=0, stdout=json.dumps({"mergeable": "CONFLICTING"}))
            anomalies = _detect_anomalies(store, "owner/repo")
        conflict = [a for a in anomalies if a.kind == AnomalyKind.CONFLICT_PR]
        assert len(conflict) == 1
        assert conflict[0].pr_number == 300
        assert conflict[0].repair_tier == "auto"

    def test_mergeable_pr_not_flagged(self, store: BeadStore) -> None:
        bead = PRBead(pr_number=301, repo="owner/repo", issue_number=21, branch="21-b")
        store.write_pr_bead(bead)
        with patch("breadforge.monitor.detect._gh") as mock_gh:
            mock_gh.return_value = MagicMock(returncode=0, stdout=json.dumps({"mergeable": "MERGEABLE"}))
            anomalies = _detect_anomalies(store, "owner/repo")
        conflict = [a for a in anomalies if a.kind == AnomalyKind.CONFLICT_PR and a.pr_number == 301]
        assert len(conflict) == 0

    def test_already_conflict_state_skipped(self, store: BeadStore) -> None:
        bead = PRBead(pr_number=302, repo="owner/repo", issue_number=22, branch="22-b")
        bead.state = "conflict"  # type: ignore
        store.write_pr_bead(bead)
        with patch("breadforge.monitor.detect._gh") as mock_gh:
            mock_gh.return_value = MagicMock(returncode=0, stdout=json.dumps({"mergeable": "CONFLICTING"}))
            anomalies = _detect_anomalies(store, "owner/repo")
        conflict = [a for a in anomalies if a.kind == AnomalyKind.CONFLICT_PR and a.pr_number == 302]
        assert len(conflict) == 0

    def test_merged_pr_skipped_for_conflict(self, store: BeadStore) -> None:
        bead = PRBead(pr_number=303, repo="owner/repo", issue_number=23, branch="23-b")
        bead.state = "merged"  # type: ignore
        store.write_pr_bead(bead)
        with patch("breadforge.monitor.detect._gh") as mock_gh:
            mock_gh.return_value = MagicMock(returncode=0, stdout=json.dumps({"mergeable": "CONFLICTING"}))
            anomalies = _detect_anomalies(store, "owner/repo")
        conflict = [a for a in anomalies if a.kind == AnomalyKind.CONFLICT_PR and a.pr_number == 303]
        assert len(conflict) == 0

    def test_conflict_gh_failure_skipped(self, store: BeadStore) -> None:
        bead = PRBead(pr_number=304, repo="owner/repo", issue_number=24, branch="24-b")
        store.write_pr_bead(bead)
        with patch("breadforge.monitor.detect._gh") as mock_gh:
            mock_gh.return_value = MagicMock(returncode=1, stdout="")
            anomalies = _detect_anomalies(store, "owner/repo")
        conflict = [a for a in anomalies if a.kind == AnomalyKind.CONFLICT_PR and a.pr_number == 304]
        assert len(conflict) == 0


# ---------------------------------------------------------------------------
# Stale label detection
# ---------------------------------------------------------------------------


class TestStaleLabelDetection:
    def test_detects_stale_label(self, store: BeadStore) -> None:
        with patch("breadforge.monitor.detect._gh") as mock_gh:
            mock_gh.return_value = MagicMock(
                returncode=0,
                stdout=json.dumps([{"number": 99, "title": "Orphan"}]),
            )
            anomalies = _detect_anomalies(store, "owner/repo")
        stale = [a for a in anomalies if a.kind == AnomalyKind.STALE_LABEL]
        assert len(stale) == 1
        assert stale[0].issue_number == 99
        assert stale[0].repair_tier == "auto"

    def test_claimed_bead_not_stale(self, store: BeadStore) -> None:
        bead = WorkBead(issue_number=42, repo="owner/repo", title="Claimed")
        bead.state = "claimed"  # type: ignore
        store.write_work_bead(bead)
        with patch("breadforge.monitor.detect._gh") as mock_gh:
            mock_gh.return_value = MagicMock(
                returncode=0,
                stdout=json.dumps([{"number": 42, "title": "Claimed"}]),
            )
            anomalies = _detect_anomalies(store, "owner/repo")
        stale = [a for a in anomalies if a.kind == AnomalyKind.STALE_LABEL]
        assert len(stale) == 0

    def test_gh_failure_no_stale(self, store: BeadStore) -> None:
        with patch("breadforge.monitor.detect._gh") as mock_gh:
            mock_gh.return_value = MagicMock(returncode=1, stdout="")
            anomalies = _detect_anomalies(store, "owner/repo")
        stale = [a for a in anomalies if a.kind == AnomalyKind.STALE_LABEL]
        assert len(stale) == 0

    def test_bad_json_stale_labels_skipped(self, store: BeadStore) -> None:
        with patch("breadforge.monitor.detect._gh") as mock_gh:
            mock_gh.return_value = MagicMock(returncode=0, stdout="not-json")
            anomalies = _detect_anomalies(store, "owner/repo")
        stale = [a for a in anomalies if a.kind == AnomalyKind.STALE_LABEL]
        assert len(stale) == 0

    def test_empty_store_and_no_gh_issues(self, store: BeadStore) -> None:
        with patch("breadforge.monitor.detect._gh") as mock_gh:
            mock_gh.return_value = MagicMock(returncode=0, stdout="[]")
            anomalies = _detect_anomalies(store, "owner/repo")
        assert anomalies == []
