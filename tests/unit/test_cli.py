"""Unit tests for breadforge CLI."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from breadforge.beads import BeadStore, GraphNode, WorkBead
from breadforge.cli import (
    _build_status_table,
    _ensure_milestone,
    _file_issue,
    _get_logger,
    _get_open_issues_for_milestone,
    _get_store,
    _require_repo,
    _seed_work_beads,
    app,
)
from breadforge.config import Config

runner = CliRunner()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def beads_dir(tmp_path: Path) -> Path:
    d = tmp_path / "beads"
    d.mkdir()
    return d


@pytest.fixture
def config(beads_dir: Path) -> Config:
    return Config(repo="owner/repo", beads_dir=beads_dir)


@pytest.fixture
def store(config: Config) -> BeadStore:
    return BeadStore(config.beads_dir, config.repo)


@pytest.fixture
def env_with_beads(beads_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BREADFORGE_BEADS_DIR", str(beads_dir))


# ---------------------------------------------------------------------------
# _require_repo
# ---------------------------------------------------------------------------


class TestRequireRepo:
    def test_explicit_repo(self) -> None:
        assert _require_repo("owner/repo") == "owner/repo"

    def test_detects_from_git_remote(self) -> None:
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "owner/detected\n"
        with patch("subprocess.run", return_value=mock_result):
            result = _require_repo(None)
        assert result == "owner/detected"

    def test_raises_when_no_repo(self) -> None:
        import typer as _typer

        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = ""
        with patch("subprocess.run", return_value=mock_result), pytest.raises(_typer.Exit):
            _require_repo(None)


# ---------------------------------------------------------------------------
# _seed_work_beads
# ---------------------------------------------------------------------------


class TestSeedWorkBeads:
    def test_creates_new_beads(self, store: BeadStore) -> None:
        issues = [
            {"number": 1, "title": "Fix bug"},
            {"number": 2, "title": "Add feature"},
        ]
        new = _seed_work_beads(store, issues, "v1", "spec.md", "owner/repo")
        assert new == [1, 2]
        assert store.read_work_bead(1) is not None
        assert store.read_work_bead(2) is not None

    def test_skips_existing_beads(self, store: BeadStore) -> None:
        bead = WorkBead(issue_number=5, repo="owner/repo", title="Old title")
        store.write_work_bead(bead)

        issues = [{"number": 5, "title": "Old title"}]
        new = _seed_work_beads(store, issues, "v1", None, "owner/repo")
        assert new == []  # no new beads

    def test_syncs_title_for_existing_bead(self, store: BeadStore) -> None:
        bead = WorkBead(issue_number=7, repo="owner/repo", title="Old title")
        store.write_work_bead(bead)

        issues = [{"number": 7, "title": "Renamed title"}]
        _seed_work_beads(store, issues, "v1", None, "owner/repo")

        updated = store.read_work_bead(7)
        assert updated is not None
        assert updated.title == "Renamed title"

    def test_sets_bead_fields(self, store: BeadStore) -> None:
        issues = [{"number": 10, "title": "My issue"}]
        _seed_work_beads(store, issues, "v2", "specs/v2.md", "owner/repo")
        bead = store.read_work_bead(10)
        assert bead is not None
        assert bead.milestone == "v2"
        assert bead.spec_file == "specs/v2.md"
        assert bead.repo == "owner/repo"


# ---------------------------------------------------------------------------
# _get_open_issues_for_milestone
# ---------------------------------------------------------------------------


class TestGetOpenIssues:
    def test_returns_parsed_issues(self) -> None:
        data = [{"number": 1, "title": "Issue", "labels": []}]
        mock_r = MagicMock()
        mock_r.returncode = 0
        mock_r.stdout = json.dumps(data)
        with patch("subprocess.run", return_value=mock_r):
            result = _get_open_issues_for_milestone("owner/repo", "v1")
        assert result == data

    def test_returns_empty_on_failure(self) -> None:
        mock_r = MagicMock()
        mock_r.returncode = 1
        with patch("subprocess.run", return_value=mock_r):
            result = _get_open_issues_for_milestone("owner/repo", "v1")
        assert result == []

    def test_returns_empty_on_invalid_json(self) -> None:
        mock_r = MagicMock()
        mock_r.returncode = 0
        mock_r.stdout = "not-json"
        with patch("subprocess.run", return_value=mock_r):
            result = _get_open_issues_for_milestone("owner/repo", "v1")
        assert result == []


# ---------------------------------------------------------------------------
# _file_issue
# ---------------------------------------------------------------------------


class TestFileIssue:
    def test_returns_issue_number_from_url(self) -> None:
        mock_r = MagicMock()
        mock_r.returncode = 0
        mock_r.stdout = "https://github.com/owner/repo/issues/42\n"
        with patch("subprocess.run", return_value=mock_r):
            result = _file_issue("owner/repo", "Title", "Body", "v1", ["stage/impl"])
        assert result == 42

    def test_returns_none_on_failure(self) -> None:
        mock_r = MagicMock()
        mock_r.returncode = 1
        with patch("subprocess.run", return_value=mock_r):
            result = _file_issue("owner/repo", "Title", "Body", "v1", [])
        assert result is None

    def test_returns_none_on_bad_url(self) -> None:
        mock_r = MagicMock()
        mock_r.returncode = 0
        mock_r.stdout = "not-a-url\n"
        with patch("subprocess.run", return_value=mock_r):
            result = _file_issue("owner/repo", "Title", "Body", "v1", [])
        assert result is None

    def test_passes_labels_in_command(self) -> None:
        mock_r = MagicMock()
        mock_r.returncode = 0
        mock_r.stdout = "https://github.com/owner/repo/issues/5\n"
        with patch("subprocess.run", return_value=mock_r) as mock_sub:
            _file_issue("owner/repo", "T", "B", "v1", ["stage/impl", "P2"])
        cmd = mock_sub.call_args[0][0]
        assert "--label" in cmd
        assert "stage/impl" in cmd
        assert "P2" in cmd


# ---------------------------------------------------------------------------
# _ensure_milestone
# ---------------------------------------------------------------------------


class TestEnsureMilestone:
    def test_returns_true_when_exists(self) -> None:
        mock_r = MagicMock()
        mock_r.returncode = 0
        mock_r.stdout = "1\n"
        with patch("subprocess.run", return_value=mock_r):
            assert _ensure_milestone("owner/repo", "v1") is True

    def test_creates_when_missing(self) -> None:
        check_r = MagicMock()
        check_r.returncode = 0
        check_r.stdout = "0\n"
        create_r = MagicMock()
        create_r.returncode = 0
        with patch("subprocess.run", side_effect=[check_r, create_r]):
            assert _ensure_milestone("owner/repo", "v1") is True

    def test_returns_false_on_create_failure(self) -> None:
        check_r = MagicMock()
        check_r.returncode = 0
        check_r.stdout = "0\n"
        create_r = MagicMock()
        create_r.returncode = 1
        with patch("subprocess.run", side_effect=[check_r, create_r]):
            assert _ensure_milestone("owner/repo", "v1") is False


# ---------------------------------------------------------------------------
# _get_store and _get_logger
# ---------------------------------------------------------------------------


class TestGetStoreAndLogger:
    def test_get_store_returns_bead_store(self, config: Config) -> None:
        s = _get_store(config)
        assert isinstance(s, BeadStore)
        assert (config.beads_dir / "owner" / "repo").exists()

    def test_get_logger_creates_log_dir(self, config: Config) -> None:
        log_dir = config.beads_dir / "logs"
        assert not log_dir.exists()
        _get_logger(config)
        assert log_dir.exists()


# ---------------------------------------------------------------------------
# _build_status_table
# ---------------------------------------------------------------------------


class TestBuildStatusTable:
    def test_empty_store(self, store: BeadStore) -> None:
        # Should not raise; returns a Group (even if empty)
        result = _build_status_table(store, "owner/repo", None)
        assert result is not None

    def test_includes_work_beads(self, store: BeadStore) -> None:
        bead = WorkBead(issue_number=1, repo="owner/repo", title="Test issue", milestone="v1")
        store.write_work_bead(bead)
        result = _build_status_table(store, "owner/repo", "v1")
        assert result is not None

    def test_milestone_filter(self, store: BeadStore) -> None:
        for i, ms in enumerate(["v1", "v2"]):
            b = WorkBead(issue_number=i + 1, repo="owner/repo", title=f"Issue {i}", milestone=ms)
            store.write_work_bead(b)
        # Should not raise; milestone filtering is applied
        result = _build_status_table(store, "owner/repo", "v1")
        assert result is not None

    def test_includes_graph_nodes(self, store: BeadStore) -> None:
        node = GraphNode(id="v1-plan", type="plan", state="done")
        store.write_node(node)
        result = _build_status_table(store, "owner/repo", "v1")
        assert result is not None


# ---------------------------------------------------------------------------
# CLI commands via CliRunner
# ---------------------------------------------------------------------------


class TestStatusCommand:
    def test_no_beads_message(self, beads_dir: Path, env_with_beads: None) -> None:
        result = runner.invoke(app, ["status", "--repo", "owner/repo"])
        assert result.exit_code == 0
        assert "No beads" in result.output

    def test_shows_beads(self, beads_dir: Path, env_with_beads: None) -> None:
        store = BeadStore(beads_dir, "owner/repo")
        bead = WorkBead(issue_number=1, repo="owner/repo", title="Fix bug", milestone="v1")
        store.write_work_bead(bead)
        result = runner.invoke(app, ["status", "--repo", "owner/repo"])
        assert result.exit_code == 0

    def test_milestone_filter(self, beads_dir: Path, env_with_beads: None) -> None:
        result = runner.invoke(app, ["status", "--repo", "owner/repo", "--milestone", "v1"])
        assert result.exit_code == 0


class TestBeadsCommand:
    def test_empty_store(self, beads_dir: Path, env_with_beads: None) -> None:
        result = runner.invoke(app, ["beads", "--repo", "owner/repo"])
        assert result.exit_code == 0
        assert "Work Beads" in result.output

    def test_shows_work_beads(self, beads_dir: Path, env_with_beads: None) -> None:
        store = BeadStore(beads_dir, "owner/repo")
        bead = WorkBead(issue_number=3, repo="owner/repo", title="Add feature")
        store.write_work_bead(bead)
        result = runner.invoke(app, ["beads", "--repo", "owner/repo"])
        assert result.exit_code == 0
        assert "Add feature" in result.output

    def test_state_filter(self, beads_dir: Path, env_with_beads: None) -> None:
        store = BeadStore(beads_dir, "owner/repo")
        open_bead = WorkBead(issue_number=1, repo="owner/repo", title="Open issue")
        closed_bead = WorkBead(issue_number=2, repo="owner/repo", title="Closed issue")
        closed_bead.state = "closed"  # type: ignore
        store.write_work_bead(open_bead)
        store.write_work_bead(closed_bead)
        result = runner.invoke(app, ["beads", "--repo", "owner/repo", "--state", "open"])
        assert result.exit_code == 0


class TestRepoCommands:
    def test_repo_list_empty(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        result = runner.invoke(app, ["repo", "list"])
        assert result.exit_code == 0
        assert "No repos" in result.output

    def test_repo_remove_not_found(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        result = runner.invoke(app, ["repo", "remove", "nonexistent/repo"])
        assert result.exit_code == 0
        assert "Not found" in result.output


# ---------------------------------------------------------------------------
# graph nodes command
# ---------------------------------------------------------------------------


class TestGraphNodes:
    def test_empty_store(self, beads_dir: Path, env_with_beads: None) -> None:
        result = runner.invoke(app, ["graph", "nodes", "--repo", "owner/repo"])
        assert result.exit_code == 0
        assert "No graph nodes" in result.output

    def test_lists_nodes(self, beads_dir: Path, env_with_beads: None) -> None:
        store = BeadStore(beads_dir, "owner/repo")
        node = GraphNode(id="v1-plan", type="plan", state="done")
        store.write_node(node)
        result = runner.invoke(app, ["graph", "nodes", "--repo", "owner/repo"])
        assert result.exit_code == 0
        assert "v1-plan" in result.output

    def test_milestone_filter(self, beads_dir: Path, env_with_beads: None) -> None:
        store = BeadStore(beads_dir, "owner/repo")
        store.write_node(GraphNode(id="v1-plan", type="plan", state="done"))
        store.write_node(GraphNode(id="v2-plan", type="plan", state="pending"))
        result = runner.invoke(app, ["graph", "nodes", "--repo", "owner/repo", "--milestone", "v1"])
        assert result.exit_code == 0
        assert "v1-plan" in result.output
        assert "v2-plan" not in result.output

    def test_state_filter(self, beads_dir: Path, env_with_beads: None) -> None:
        store = BeadStore(beads_dir, "owner/repo")
        store.write_node(GraphNode(id="v1-plan", type="plan", state="done"))
        store.write_node(GraphNode(id="v1-build-mod", type="build", state="failed"))
        result = runner.invoke(app, ["graph", "nodes", "--repo", "owner/repo", "--state", "done"])
        assert result.exit_code == 0
        assert "v1-plan" in result.output
        assert "v1-build-mod" not in result.output

    def test_state_filter_no_match(self, beads_dir: Path, env_with_beads: None) -> None:
        store = BeadStore(beads_dir, "owner/repo")
        store.write_node(GraphNode(id="v1-plan", type="plan", state="done"))
        result = runner.invoke(
            app, ["graph", "nodes", "--repo", "owner/repo", "--state", "running"]
        )
        assert result.exit_code == 0
        assert "No graph nodes" in result.output

    def test_shows_node_count(self, beads_dir: Path, env_with_beads: None) -> None:
        store = BeadStore(beads_dir, "owner/repo")
        for i in range(3):
            store.write_node(GraphNode(id=f"v1-build-mod{i}", type="build", state="done"))
        result = runner.invoke(app, ["graph", "nodes", "--repo", "owner/repo"])
        assert result.exit_code == 0
        assert "3 node" in result.output

    def test_shows_cost_when_present(self, beads_dir: Path, env_with_beads: None) -> None:
        store = BeadStore(beads_dir, "owner/repo")
        node = GraphNode(id="v1-plan", type="plan", state="done", output={"cost_usd": 0.0123})
        store.write_node(node)
        result = runner.invoke(app, ["graph", "nodes", "--repo", "owner/repo"])
        assert result.exit_code == 0
        assert "0.0123" in result.output


# ---------------------------------------------------------------------------
# graph node command
# ---------------------------------------------------------------------------


class TestGraphNode:
    def test_node_not_found(self, beads_dir: Path, env_with_beads: None) -> None:
        result = runner.invoke(app, ["graph", "node", "v1-plan", "--repo", "owner/repo"])
        assert result.exit_code == 1
        assert "not found" in result.output

    def test_shows_node_details(self, beads_dir: Path, env_with_beads: None) -> None:
        store = BeadStore(beads_dir, "owner/repo")
        node = GraphNode(
            id="v1-plan",
            type="plan",
            state="done",
            depends_on=["v1-research"],
            assigned_model="claude-sonnet-4-6",
            retry_count=1,
            context={"milestone": "v1"},
        )
        store.write_node(node)
        result = runner.invoke(app, ["graph", "node", "v1-plan", "--repo", "owner/repo"])
        assert result.exit_code == 0
        assert "v1-plan" in result.output
        assert "plan" in result.output
        assert "done" in result.output
        assert "claude-sonnet-4-6" in result.output
        assert "v1-research" in result.output

    def test_shows_context_and_output(self, beads_dir: Path, env_with_beads: None) -> None:
        store = BeadStore(beads_dir, "owner/repo")
        node = GraphNode(
            id="v1-build-core",
            type="build",
            state="done",
            context={"module": "core", "files": ["a.py"]},
            output={"model": "claude-sonnet-4-6", "cost_usd": 0.05},
        )
        store.write_node(node)
        result = runner.invoke(app, ["graph", "node", "v1-build-core", "--repo", "owner/repo"])
        assert result.exit_code == 0
        assert "Context" in result.output
        assert "Output" in result.output

    def test_artifact_in_output_shown_as_placeholder(
        self, beads_dir: Path, env_with_beads: None
    ) -> None:
        store = BeadStore(beads_dir, "owner/repo")
        node = GraphNode(
            id="v1-plan",
            type="plan",
            state="done",
            output={"artifact": {"modules": ["core"]}, "model": "gpt-4"},
        )
        store.write_node(node)
        result = runner.invoke(app, ["graph", "node", "v1-plan", "--repo", "owner/repo"])
        assert result.exit_code == 0
        assert "present" in result.output
        # Raw artifact dict should not be dumped directly
        assert "modules" not in result.output

    def test_node_with_no_context_or_output(self, beads_dir: Path, env_with_beads: None) -> None:
        store = BeadStore(beads_dir, "owner/repo")
        node = GraphNode(id="v1-merge", type="merge", state="pending")
        store.write_node(node)
        result = runner.invoke(app, ["graph", "node", "v1-merge", "--repo", "owner/repo"])
        assert result.exit_code == 0
        assert "v1-merge" in result.output


# ---------------------------------------------------------------------------
# graph retry command
# ---------------------------------------------------------------------------


class TestGraphRetry:
    def test_node_not_found(self, beads_dir: Path, env_with_beads: None) -> None:
        result = runner.invoke(app, ["graph", "retry", "v1-plan", "--repo", "owner/repo"])
        assert result.exit_code == 1
        assert "not found" in result.output

    def test_retries_failed_node(self, beads_dir: Path, env_with_beads: None) -> None:
        store = BeadStore(beads_dir, "owner/repo")
        node = GraphNode(id="v1-build-core", type="build", state="failed")
        store.write_node(node)
        result = runner.invoke(app, ["graph", "retry", "v1-build-core", "--repo", "owner/repo"])
        assert result.exit_code == 0
        assert "pending" in result.output
        updated = store.read_node("v1-build-core")
        assert updated is not None
        assert updated.state == "pending"

    def test_retries_abandoned_node(self, beads_dir: Path, env_with_beads: None) -> None:
        store = BeadStore(beads_dir, "owner/repo")
        node = GraphNode(id="v1-build-core", type="build", state="abandoned")
        store.write_node(node)
        result = runner.invoke(app, ["graph", "retry", "v1-build-core", "--repo", "owner/repo"])
        assert result.exit_code == 0
        updated = store.read_node("v1-build-core")
        assert updated is not None
        assert updated.state == "pending"

    def test_rejects_non_failed_without_force(self, beads_dir: Path, env_with_beads: None) -> None:
        store = BeadStore(beads_dir, "owner/repo")
        node = GraphNode(id="v1-plan", type="plan", state="done")
        store.write_node(node)
        result = runner.invoke(app, ["graph", "retry", "v1-plan", "--repo", "owner/repo"])
        assert result.exit_code == 1
        assert "warning" in result.output or "not failed" in result.output
        # Node should remain done
        updated = store.read_node("v1-plan")
        assert updated is not None
        assert updated.state == "done"

    def test_force_resets_any_state(self, beads_dir: Path, env_with_beads: None) -> None:
        store = BeadStore(beads_dir, "owner/repo")
        node = GraphNode(id="v1-plan", type="plan", state="done")
        store.write_node(node)
        result = runner.invoke(
            app, ["graph", "retry", "v1-plan", "--repo", "owner/repo", "--force"]
        )
        assert result.exit_code == 0
        updated = store.read_node("v1-plan")
        assert updated is not None
        assert updated.state == "pending"

    def test_force_resets_running_node(self, beads_dir: Path, env_with_beads: None) -> None:
        store = BeadStore(beads_dir, "owner/repo")
        node = GraphNode(id="v1-build-mod", type="build", state="running")
        store.write_node(node)
        result = runner.invoke(
            app, ["graph", "retry", "v1-build-mod", "--repo", "owner/repo", "--force"]
        )
        assert result.exit_code == 0
        updated = store.read_node("v1-build-mod")
        assert updated is not None
        assert updated.state == "pending"

    def test_clears_timestamps_on_retry(self, beads_dir: Path, env_with_beads: None) -> None:
        store = BeadStore(beads_dir, "owner/repo")
        node = GraphNode(id="v1-build-mod", type="build", state="failed")
        node.touch_started()
        node.touch_completed()
        assert node.started_at is not None
        assert node.completed_at is not None
        store.write_node(node)

        result = runner.invoke(app, ["graph", "retry", "v1-build-mod", "--repo", "owner/repo"])
        assert result.exit_code == 0
        updated = store.read_node("v1-build-mod")
        assert updated is not None
        assert updated.started_at is None
        assert updated.completed_at is None
