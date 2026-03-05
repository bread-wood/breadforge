"""Unit tests for monitor anomaly detection."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from breadforge.beads import BeadStore, WorkBead
from breadforge.monitor import AnomalyKind, AnomalyStore, _detect_anomalies


@pytest.fixture
def store(tmp_path: Path) -> BeadStore:
    return BeadStore(tmp_path / "beads", "owner/repo")


@pytest.fixture
def astore(tmp_path: Path) -> AnomalyStore:
    return AnomalyStore(tmp_path / "beads", "owner/repo")


class TestAnomalyStore:
    def test_write_and_read(self, astore: AnomalyStore) -> None:
        from breadforge.monitor import AnomalyBead

        bead = AnomalyBead(
            anomaly_id="test-001",
            repo="owner/repo",
            kind=AnomalyKind.STUCK_ISSUE,
            issue_number=42,
        )
        astore.write(bead)
        result = astore.read("test-001")
        assert result is not None
        assert result.anomaly_id == "test-001"
        assert result.kind == AnomalyKind.STUCK_ISSUE

    def test_list_open_excludes_resolved(self, astore: AnomalyStore) -> None:
        from breadforge.monitor import AnomalyBead

        open_bead = AnomalyBead(
            anomaly_id="open-001",
            repo="owner/repo",
            kind=AnomalyKind.STUCK_ISSUE,
        )
        resolved_bead = AnomalyBead(
            anomaly_id="resolved-001",
            repo="owner/repo",
            kind=AnomalyKind.ZOMBIE_PR,
            resolved=True,
        )
        astore.write(open_bead)
        astore.write(resolved_bead)
        open_list = astore.list_open()
        assert len(open_list) == 1
        assert open_list[0].anomaly_id == "open-001"


class TestDetectAnomalies:
    def test_detects_stuck_issue(self, store: BeadStore) -> None:
        import json
        from datetime import UTC, datetime, timedelta

        bead = WorkBead(issue_number=10, repo="owner/repo", title="Stuck issue")
        bead.state = "claimed"  # type: ignore
        bead.updated_at = datetime.now(UTC) - timedelta(minutes=200)
        # Write directly to avoid touch() resetting updated_at
        data = bead.model_dump(mode="json")
        store._work_dir.mkdir(parents=True, exist_ok=True)
        (store._work_dir / "10.json").write_text(json.dumps(data))

        with patch("breadforge.monitor.detect._gh") as mock_gh:
            mock_gh.return_value = MagicMock(returncode=0, stdout="[]")
            anomalies = _detect_anomalies(store, "owner/repo", stuck_minutes=60)

        stuck = [a for a in anomalies if a.kind == AnomalyKind.STUCK_ISSUE]
        assert len(stuck) == 1
        assert stuck[0].issue_number == 10

    def test_no_anomaly_for_fresh_claimed(self, store: BeadStore) -> None:
        bead = WorkBead(issue_number=11, repo="owner/repo", title="Fresh issue")
        bead.state = "claimed"  # type: ignore
        store.write_work_bead(bead)

        with patch("breadforge.monitor.detect._gh") as mock_gh:
            mock_gh.return_value = MagicMock(returncode=0, stdout="[]")
            anomalies = _detect_anomalies(store, "owner/repo", stuck_minutes=120)

        stuck = [a for a in anomalies if a.kind == AnomalyKind.STUCK_ISSUE]
        assert len(stuck) == 0

    def test_detects_stale_label(self, store: BeadStore) -> None:
        # No claimed beads, but GitHub returns an in-progress issue
        with patch("breadforge.monitor.detect._gh") as mock_gh:
            import json

            def gh_side_effect(*args):
                result = MagicMock()
                result.returncode = 0
                if "in-progress" in args:
                    result.stdout = json.dumps([{"number": 99, "title": "Orphan"}])
                elif "checks" in args:
                    result.stdout = "[]"
                else:
                    result.stdout = json.dumps({"mergeable": "MERGEABLE"})
                return result

            mock_gh.side_effect = gh_side_effect
            anomalies = _detect_anomalies(store, "owner/repo")

        stale = [a for a in anomalies if a.kind == AnomalyKind.STALE_LABEL]
        assert len(stale) == 1
        assert stale[0].issue_number == 99
