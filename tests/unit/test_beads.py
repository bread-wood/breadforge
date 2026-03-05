"""Unit tests for the bead system."""

from pathlib import Path

import pytest

from breadforge.beads import (
    BeadStore,
    CampaignBead,
    MergeQueueItem,
    MilestonePlan,
    PRBead,
    WorkBead,
)


@pytest.fixture
def store(tmp_path: Path) -> BeadStore:
    return BeadStore(tmp_path / "beads", "owner/repo")


class TestWorkBead:
    def test_write_and_read(self, store: BeadStore) -> None:
        bead = WorkBead(issue_number=1, repo="owner/repo", title="Fix bug")
        store.write_work_bead(bead)
        result = store.read_work_bead(1)
        assert result is not None
        assert result.issue_number == 1
        assert result.title == "Fix bug"
        assert result.state == "open"

    def test_state_transition(self, store: BeadStore) -> None:
        bead = WorkBead(issue_number=2, repo="owner/repo", title="Add feature")
        store.write_work_bead(bead)
        bead.state = "claimed"  # type: ignore
        bead.branch = "2-add-feature"
        store.write_work_bead(bead)
        result = store.read_work_bead(2)
        assert result is not None
        assert result.state == "claimed"
        assert result.branch == "2-add-feature"

    def test_list_by_state(self, store: BeadStore) -> None:
        for i in range(3):
            b = WorkBead(issue_number=i + 10, repo="owner/repo", title=f"Issue {i}")
            if i == 0:
                b.state = "claimed"  # type: ignore
            store.write_work_bead(b)
        claimed = store.list_work_beads(state="claimed")
        assert len(claimed) == 1
        assert claimed[0].issue_number == 10

    def test_list_by_milestone(self, store: BeadStore) -> None:
        b1 = WorkBead(issue_number=20, repo="owner/repo", title="A", milestone="v1.0")
        b2 = WorkBead(issue_number=21, repo="owner/repo", title="B", milestone="v2.0")
        store.write_work_bead(b1)
        store.write_work_bead(b2)
        results = store.list_work_beads(milestone="v1.0")
        assert len(results) == 1
        assert results[0].issue_number == 20

    def test_read_missing(self, store: BeadStore) -> None:
        assert store.read_work_bead(9999) is None

    def test_atomic_write(self, store: BeadStore) -> None:
        """Atomic write should not leave tmp files behind."""
        bead = WorkBead(issue_number=30, repo="owner/repo", title="Test")
        store.write_work_bead(bead)
        tmp_files = list((store._work_dir).glob("*.tmp"))
        assert len(tmp_files) == 0


class TestPRBead:
    def test_write_and_read(self, store: BeadStore) -> None:
        bead = PRBead(pr_number=100, repo="owner/repo", issue_number=1, branch="1-fix")
        store.write_pr_bead(bead)
        result = store.read_pr_bead(100)
        assert result is not None
        assert result.pr_number == 100
        assert result.state == "open"

    def test_list_by_state(self, store: BeadStore) -> None:
        b1 = PRBead(pr_number=101, repo="owner/repo", issue_number=1, branch="1-a")
        b2 = PRBead(pr_number=102, repo="owner/repo", issue_number=2, branch="2-b")
        b2.state = "merged"  # type: ignore
        store.write_pr_bead(b1)
        store.write_pr_bead(b2)
        open_prs = store.list_pr_beads(state="open")
        assert len(open_prs) == 1
        assert open_prs[0].pr_number == 101


class TestMergeQueue:
    def test_enqueue_dequeue(self, store: BeadStore) -> None:
        item = MergeQueueItem(pr_number=1, issue_number=10, branch="10-foo")
        store.enqueue_merge(item)
        queue = store.read_merge_queue()
        assert len(queue.items) == 1
        assert queue.peek() is not None
        assert queue.peek().pr_number == 1

    def test_dedup_enqueue(self, store: BeadStore) -> None:
        item = MergeQueueItem(pr_number=1, issue_number=10, branch="10-foo")
        store.enqueue_merge(item)
        store.enqueue_merge(item)  # duplicate
        queue = store.read_merge_queue()
        assert len(queue.items) == 1

    def test_ordered_dequeue(self, store: BeadStore) -> None:
        for i in range(3):
            store.enqueue_merge(
                MergeQueueItem(pr_number=i + 1, issue_number=i + 10, branch=f"{i}-b")
            )
        queue = store.read_merge_queue()
        first = queue.dequeue()
        assert first is not None
        assert first.pr_number == 1
        store.write_merge_queue(queue)
        queue2 = store.read_merge_queue()
        assert len(queue2.items) == 2


class TestCampaignBead:
    def test_write_and_read(self, store: BeadStore) -> None:
        campaign = CampaignBead(repo="owner/repo")
        campaign.milestones.append(MilestonePlan(milestone="v1.0", repo="owner/repo"))
        store.write_campaign_bead(campaign)
        result = store.read_campaign_bead()
        assert result is not None
        assert len(result.milestones) == 1
        assert result.milestones[0].milestone == "v1.0"

    def test_read_missing(self, store: BeadStore) -> None:
        assert store.read_campaign_bead() is None

    def test_get_milestone(self, store: BeadStore) -> None:
        campaign = CampaignBead(repo="owner/repo")
        campaign.milestones.append(MilestonePlan(milestone="v1.0", repo="owner/repo", wave=0))
        campaign.milestones.append(MilestonePlan(milestone="v2.0", repo="owner/repo", wave=1))
        store.write_campaign_bead(campaign)
        result = store.read_campaign_bead()
        assert result is not None
        m = result.get_milestone("v1.0")
        assert m is not None
        assert m.wave == 0

    def test_all_shipped_in_wave(self) -> None:
        campaign = CampaignBead(repo="owner/repo")
        campaign.milestones.append(
            MilestonePlan(milestone="v1.0", repo="owner/repo", wave=0, status="shipped")
        )
        campaign.milestones.append(
            MilestonePlan(milestone="v1.1", repo="owner/repo", wave=0, status="shipped")
        )
        campaign.milestones.append(MilestonePlan(milestone="v2.0", repo="owner/repo", wave=1))
        assert campaign.all_shipped_in_wave(0) is True
        assert campaign.all_shipped_in_wave(1) is False
