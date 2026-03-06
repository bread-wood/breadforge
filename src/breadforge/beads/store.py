"""BeadStore — atomic reads/writes for all bead types.

Layout:
  ~/.breadforge/beads/<owner>/<repo>/work/<N>.json       WorkBead
  ~/.breadforge/beads/<owner>/<repo>/prs/pr-<N>.json     PRBead
  ~/.breadforge/beads/<owner>/<repo>/merge-queue.json    MergeQueue
  ~/.breadforge/beads/<owner>/<repo>/campaign.json       CampaignBead
  ~/.breadforge/beads/<owner>/<repo>/graph/<node-id>.json  GraphNode (new)
  ~/.breadforge/beads/<owner>/<repo>/research/<node-id>.md findings (new)

All writes use write-to-tmp + os.replace (atomic on POSIX).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from breadforge.beads.types import (
    CampaignBead,
    GraphNode,
    MergeQueue,
    MergeQueueItem,
    NodeState,
    NodeType,
    PRBead,
    PRState,
    WorkBead,
    WorkState,
)


class BeadStore:
    """Atomic bead reads/writes using write-to-tmp + os.replace."""

    def __init__(self, beads_dir: Path, repo: str) -> None:
        owner, name = repo.split("/", 1)
        self._root = beads_dir / owner / name
        self._work_dir = self._root / "work"
        self._prs_dir = self._root / "prs"
        self._graph_dir = self._root / "graph"
        self._research_dir = self._root / "research"
        self._root.mkdir(parents=True, exist_ok=True)
        self._work_dir.mkdir(exist_ok=True)
        self._prs_dir.mkdir(exist_ok=True)
        self._graph_dir.mkdir(exist_ok=True)
        self._research_dir.mkdir(exist_ok=True)

    # --- Work beads ---

    def _work_path(self, issue_number: int) -> Path:
        return self._work_dir / f"{issue_number}.json"

    def write_work_bead(self, bead: WorkBead) -> None:
        bead.touch()
        self._atomic_write(self._work_path(bead.issue_number), bead.model_dump(mode="json"))

    def read_work_bead(self, issue_number: int) -> WorkBead | None:
        path = self._work_path(issue_number)
        if not path.exists():
            return None
        return WorkBead.model_validate(self._read_json(path))

    def list_work_beads(
        self,
        state: WorkState | None = None,
        milestone: str | None = None,
    ) -> list[WorkBead]:
        beads = []
        for p in self._work_dir.glob("*.json"):
            try:
                b = WorkBead.model_validate(self._read_json(p))
                if state and b.state != state:
                    continue
                if milestone and b.milestone != milestone:
                    continue
                beads.append(b)
            except Exception:
                pass
        return beads

    # --- PR beads ---

    def _pr_path(self, pr_number: int) -> Path:
        return self._prs_dir / f"pr-{pr_number}.json"

    def write_pr_bead(self, bead: PRBead) -> None:
        bead.touch()
        self._atomic_write(self._pr_path(bead.pr_number), bead.model_dump(mode="json"))

    def read_pr_bead(self, pr_number: int) -> PRBead | None:
        path = self._pr_path(pr_number)
        if not path.exists():
            return None
        return PRBead.model_validate(self._read_json(path))

    def list_pr_beads(self, state: PRState | None = None) -> list[PRBead]:
        beads = []
        for p in self._prs_dir.glob("pr-*.json"):
            try:
                b = PRBead.model_validate(self._read_json(p))
                if state and b.state != state:
                    continue
                beads.append(b)
            except Exception:
                pass
        return beads

    # --- Merge queue ---

    def _mq_path(self) -> Path:
        return self._root / "merge-queue.json"

    def read_merge_queue(self) -> MergeQueue:
        path = self._mq_path()
        if not path.exists():
            repo = str(self._root.parent.name) + "/" + str(self._root.name)
            return MergeQueue(repo=repo)
        return MergeQueue.model_validate(self._read_json(path))

    def write_merge_queue(self, queue: MergeQueue) -> None:
        self._atomic_write(self._mq_path(), queue.model_dump(mode="json"))

    def enqueue_merge(self, item: MergeQueueItem) -> None:
        q = self.read_merge_queue()
        q.enqueue(item)
        self.write_merge_queue(q)

    # --- Campaign bead ---

    def _campaign_path(self) -> Path:
        return self._root / "campaign.json"

    def read_campaign_bead(self) -> CampaignBead | None:
        path = self._campaign_path()
        if not path.exists():
            return None
        return CampaignBead.model_validate(self._read_json(path))

    def write_campaign_bead(self, bead: CampaignBead) -> None:
        bead.touch()
        self._atomic_write(self._campaign_path(), bead.model_dump(mode="json"))

    # --- Graph nodes ---

    def _node_path(self, node_id: str) -> Path:
        return self._graph_dir / f"{node_id}.json"

    def write_node(self, node: GraphNode) -> None:
        self._atomic_write(self._node_path(node.id), node.model_dump(mode="json"))

    def claim_node(self, node: GraphNode) -> bool:
        """Atomically transition a node from pending → running on disk.

        Uses fcntl.flock to serialize concurrent claimants (multiple executor
        instances or a running executor + manual dispatch).  Returns True if the
        claim succeeded (node was pending on disk and is now written as running).
        Returns False if the node was already claimed by another process.
        """
        import fcntl

        path = self._node_path(node.id)
        lock_path = path.with_suffix(".lock")
        lock_path.touch()
        with lock_path.open() as lf:
            fcntl.flock(lf, fcntl.LOCK_EX)
            try:
                if not path.exists():
                    # Node not yet on disk — newly created in-memory; write it directly.
                    self._atomic_write(path, node.model_dump(mode="json"))
                    return True
                on_disk = self._read_json(path)
                if on_disk.get("state") != "pending":
                    return False  # already claimed by another process
                self._atomic_write(path, node.model_dump(mode="json"))
                return True
            finally:
                fcntl.flock(lf, fcntl.LOCK_UN)

    def read_node(self, node_id: str) -> GraphNode | None:
        path = self._node_path(node_id)
        if not path.exists():
            return None
        return GraphNode.model_validate(self._read_json(path))

    def list_nodes(
        self,
        type: NodeType | None = None,
        state: NodeState | None = None,
    ) -> list[GraphNode]:
        nodes = []
        for p in self._graph_dir.glob("*.json"):
            try:
                n = GraphNode.model_validate(self._read_json(p))
                if type and n.type != type:
                    continue
                if state and n.state != state:
                    continue
                nodes.append(n)
            except Exception:
                pass
        return nodes

    # --- Research findings ---

    def store_research_findings(self, node_id: str, markdown: str) -> Path:
        """Write research findings markdown. Returns path written."""
        path = self._research_dir / f"{node_id}.md"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(markdown, encoding="utf-8")
        os.replace(tmp, path)
        return path

    def read_research_findings(self, node_id: str) -> str | None:
        path = self._research_dir / f"{node_id}.md"
        if not path.exists():
            return None
        return path.read_text(encoding="utf-8")

    # --- Internal helpers ---

    def _atomic_write(self, path: Path, data: dict) -> None:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
        os.replace(tmp, path)

    def _read_json(self, path: Path) -> dict:
        return json.loads(path.read_text(encoding="utf-8"))
