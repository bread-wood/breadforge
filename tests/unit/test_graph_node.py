"""Unit tests for GraphNode types, state transitions, and BeadStore graph ops."""

from pathlib import Path

import pytest

from breadforge.beads import BeadStore, GraphNode, NodeState, PlanArtifact

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def store(tmp_path: Path) -> BeadStore:
    return BeadStore(tmp_path / "beads", "owner/repo")


def make_node(
    id: str,
    type: str = "build",
    state: NodeState = "pending",
    depends_on: list[str] | None = None,
    files: list[str] | None = None,
) -> GraphNode:
    context = {}
    if files is not None:
        context["files"] = files
    return GraphNode(
        id=id,
        type=type,  # type: ignore[arg-type]
        state=state,
        depends_on=depends_on or [],
        context=context,
    )


# ---------------------------------------------------------------------------
# GraphNode construction
# ---------------------------------------------------------------------------


class TestGraphNode:
    def test_defaults(self) -> None:
        node = make_node("v1-plan", type="plan")
        assert node.state == "pending"
        assert node.depends_on == []
        assert node.retry_count == 0
        assert node.max_retries == 3
        assert node.started_at is None
        assert node.completed_at is None

    def test_touch_started(self) -> None:
        node = make_node("v1-plan", type="plan")
        assert node.started_at is None
        node.touch_started()
        assert node.started_at is not None

    def test_touch_completed(self) -> None:
        node = make_node("v1-plan", type="plan")
        node.touch_completed()
        assert node.completed_at is not None

    def test_context_files(self) -> None:
        node = make_node("v1-build-core", files=["src/core.py", "src/utils.py"])
        assert node.context["files"] == ["src/core.py", "src/utils.py"]


# ---------------------------------------------------------------------------
# PlanArtifact
# ---------------------------------------------------------------------------


class TestPlanArtifact:
    def test_low_confidence(self) -> None:
        artifact = PlanArtifact(
            milestone="v1.0",
            modules=["auth"],
            files_per_module={"auth": ["src/auth.py"]},
            approach="JWT auth",
            confidence=0.4,
            unknowns=["Which JWT library?"],
        )
        assert artifact.confidence < 0.6

    def test_high_confidence(self) -> None:
        artifact = PlanArtifact(
            milestone="v1.0",
            modules=["core"],
            files_per_module={"core": ["src/core.py"]},
            approach="Simple CRUD",
            confidence=0.9,
        )
        assert artifact.confidence >= 0.6
        assert artifact.unknowns == []
        assert artifact.risk_flags == []


# ---------------------------------------------------------------------------
# BeadStore graph node ops
# ---------------------------------------------------------------------------


class TestBeadStoreGraphNodes:
    def test_write_and_read(self, store: BeadStore) -> None:
        node = make_node("v1-plan", type="plan")
        store.write_node(node)
        result = store.read_node("v1-plan")
        assert result is not None
        assert result.id == "v1-plan"
        assert result.type == "plan"
        assert result.state == "pending"

    def test_read_missing(self, store: BeadStore) -> None:
        assert store.read_node("nonexistent") is None

    def test_list_nodes_no_filter(self, store: BeadStore) -> None:
        store.write_node(make_node("v1-plan", type="plan"))
        store.write_node(make_node("v1-build-core", type="build"))
        store.write_node(make_node("v1-merge-core", type="merge"))
        nodes = store.list_nodes()
        assert len(nodes) == 3

    def test_list_nodes_by_type(self, store: BeadStore) -> None:
        store.write_node(make_node("v1-plan", type="plan"))
        store.write_node(make_node("v1-build-core", type="build"))
        store.write_node(make_node("v1-build-api", type="build"))
        build_nodes = store.list_nodes(type="build")
        assert len(build_nodes) == 2
        plan_nodes = store.list_nodes(type="plan")
        assert len(plan_nodes) == 1

    def test_list_nodes_by_state(self, store: BeadStore) -> None:
        n1 = make_node("v1-build-a", type="build", state="pending")
        n2 = make_node("v1-build-b", type="build", state="done")
        store.write_node(n1)
        store.write_node(n2)
        pending = store.list_nodes(state="pending")
        assert len(pending) == 1
        assert pending[0].id == "v1-build-a"

    def test_update_node_state(self, store: BeadStore) -> None:
        node = make_node("v1-plan", type="plan")
        store.write_node(node)
        node.state = "running"  # type: ignore[assignment]
        node.touch_started()
        store.write_node(node)
        result = store.read_node("v1-plan")
        assert result is not None
        assert result.state == "running"
        assert result.started_at is not None

    def test_atomic_write_no_tmp_files(self, store: BeadStore) -> None:
        node = make_node("v1-plan", type="plan")
        store.write_node(node)
        tmp_files = list(store._graph_dir.glob("*.tmp"))
        assert len(tmp_files) == 0


# ---------------------------------------------------------------------------
# BeadStore research findings
# ---------------------------------------------------------------------------


class TestResearchFindings:
    def test_store_and_read(self, store: BeadStore) -> None:
        markdown = "# Research findings\n\nJWT libraries: PyJWT is standard."
        path = store.store_research_findings("v1-research-auth", markdown)
        assert path.exists()
        result = store.read_research_findings("v1-research-auth")
        assert result == markdown

    def test_read_missing(self, store: BeadStore) -> None:
        assert store.read_research_findings("nonexistent") is None

    def test_overwrite(self, store: BeadStore) -> None:
        store.store_research_findings("v1-research-db", "first")
        store.store_research_findings("v1-research-db", "second")
        result = store.read_research_findings("v1-research-db")
        assert result == "second"
