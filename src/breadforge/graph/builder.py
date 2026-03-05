"""Graph builder — creates initial ExecutionGraph from a spec + milestone.

Core entry points:
  build_greenfield_graph       — new project, starts with a plan node
  build_feature_graph          — feature on existing codebase, starts with a plan node
  build_bug_graph              — bug fix, starts with a research node then plan

Cross-repo blocking:
  apply_cross_repo_blocking    — inject wait nodes for milestones that depend on
                                 other repos/milestones via CampaignBead.blocked_by.

Consensus/design-doc helpers:
  emit_consensus_node          — emit a consensus node that votes over proposals
  emit_design_doc_node         — emit a design_doc node for a given task
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from breadforge.beads.types import GraphNode
from breadforge.graph.executor import ExecutionGraph
from breadforge.graph.node import make_node

if TYPE_CHECKING:
    from breadforge.beads.store import BeadStore


def build_greenfield_graph(
    milestone: str,
    spec_file: str | Path,
    repo: str,
    repo_local_path: str | None = None,
    milestone_issue_number: int | None = None,
) -> ExecutionGraph:
    """Initial graph for a new project: single plan node."""
    plan_node = GraphNode(
        id=f"{milestone}-plan",
        type="plan",
        context={
            "milestone": milestone,
            "spec_file": str(spec_file),
            "repo": repo,
            "repo_local_path": repo_local_path or "",
            "research_node_ids": [],
            "milestone_issue_number": milestone_issue_number,
        },
    )
    return ExecutionGraph([plan_node])


def build_feature_graph(
    milestone: str,
    spec_file: str | Path,
    repo: str,
    repo_local_path: str | None = None,
) -> ExecutionGraph:
    """Initial graph for a feature on an existing codebase: single plan node."""
    plan_node = GraphNode(
        id=f"{milestone}-plan",
        type="plan",
        context={
            "milestone": milestone,
            "spec_file": str(spec_file),
            "repo": repo,
            "repo_local_path": repo_local_path or "",
            "research_node_ids": [],
        },
    )
    return ExecutionGraph([plan_node])


def build_bug_graph(
    milestone: str,
    spec_file: str | Path,
    repo: str,
    bug_description: str,
    repo_local_path: str | None = None,
) -> ExecutionGraph:
    """Initial graph for a bug fix: research node → plan-refine node."""
    research_id = f"{milestone}-research-bug"
    plan_id = f"{milestone}-plan"

    research_node = GraphNode(
        id=research_id,
        type="research",
        context={
            "milestone": milestone,
            "repo": repo,
            "unknowns": [bug_description],
        },
    )
    plan_node = GraphNode(
        id=plan_id,
        type="plan",
        depends_on=[research_id],
        context={
            "milestone": milestone,
            "spec_file": str(spec_file),
            "repo": repo,
            "repo_local_path": repo_local_path or "",
            "research_node_ids": [research_id],
        },
    )
    return ExecutionGraph([research_node, plan_node])


# ---------------------------------------------------------------------------
# Cross-repo blocking via CampaignBead
# ---------------------------------------------------------------------------


def apply_cross_repo_blocking(
    graph: ExecutionGraph,
    milestone: str,
    repo: str,
    store: BeadStore,
) -> ExecutionGraph:
    """Inject wait nodes for any cross-repo blockers declared in CampaignBead.

    Reads ``CampaignBead.milestones[milestone].blocked_by`` for *repo*.  Each
    ``"owner/repo:milestone"`` string becomes a ``wait`` node that is prepended
    as a dependency to every current pending node in the graph.

    Returns the same graph (mutated in-place) for chaining.

    Example
    -------
    If the campaign declares that ``owner/myrepo:v2`` is blocked by
    ``owner/otherrepo:v1``, this function adds::

        wait-v2-otherrepo-v1  (type="wait", max_retries=60)

    and rewires every existing pending node to depend on it.
    """
    campaign = store.read_campaign_bead()
    if not campaign:
        return graph

    plan = campaign.get_milestone(milestone, repo=repo)
    if not plan or not plan.blocked_by:
        return graph

    wait_nodes: list[GraphNode] = []
    for blocker_ref in plan.blocked_by:
        # Derive a stable node ID from the ref
        safe_ref = blocker_ref.replace("/", "-").replace(":", "-")
        node_id = f"wait-{milestone}-{safe_ref}"
        wait_node = make_node(
            id=node_id,
            type="wait",
            context={
                "blocking_milestones": [blocker_ref],
                "milestone": milestone,
                "repo": repo,
            },
            max_retries=60,  # ~1 hour at 60 s per retry
        )
        wait_nodes.append(wait_node)

    if not wait_nodes:
        return graph

    # Add wait nodes to the graph first
    graph.add_nodes(wait_nodes)
    wait_ids = [n.id for n in wait_nodes]

    # Prepend wait_ids as dependencies on all currently pending non-wait nodes
    for node in graph.all_nodes():
        if node.state != "pending":
            continue
        if node.id in wait_ids:
            continue  # don't create self-dependency
        for wid in wait_ids:
            if wid not in node.depends_on:
                node.depends_on.append(wid)

    return graph


# ---------------------------------------------------------------------------
# Consensus and design_doc node emitters
# ---------------------------------------------------------------------------


def emit_wait_node(
    milestone: str,
    blocking_milestones: list[str],
    depends_on: list[str] | None = None,
    max_retries: int = 60,
) -> GraphNode:
    """Create a single wait node that blocks on *blocking_milestones*."""
    safe = "-".join(r.replace("/", "-").replace(":", "-") for r in blocking_milestones[:2])
    return make_node(
        id=f"{milestone}-wait-{safe}",
        type="wait",
        depends_on=depends_on or [],
        context={
            "blocking_milestones": blocking_milestones,
            "milestone": milestone,
        },
        max_retries=max_retries,
    )


def emit_consensus_node(
    milestone: str,
    proposal_node_ids: list[str],
    selection_model: str | None = None,
) -> GraphNode:
    """Create a consensus node that selects among completed proposal nodes."""
    slug = "-".join(nid.split("-")[-1] for nid in proposal_node_ids[:3])
    context: dict = {
        "proposal_node_ids": proposal_node_ids,
        "milestone": milestone,
    }
    if selection_model:
        context["selection_model"] = selection_model
    return make_node(
        id=f"{milestone}-consensus-{slug}",
        type="consensus",
        depends_on=list(proposal_node_ids),
        context=context,
        max_retries=2,
    )


def emit_design_doc_node(
    milestone: str,
    title: str,
    requirements: str,
    constraints: str = "",
    depends_on: list[str] | None = None,
    design_model: str | None = None,
) -> GraphNode:
    """Create a design_doc node that generates a design document via LLM."""
    import re

    slug = re.sub(r"[^a-zA-Z0-9]", "-", title)[:30].strip("-").lower()
    context: dict = {
        "title": title,
        "requirements": requirements,
        "constraints": constraints,
        "milestone": milestone,
    }
    if design_model:
        context["design_model"] = design_model
    return make_node(
        id=f"{milestone}-design-doc-{slug}",
        type="design_doc",
        depends_on=depends_on or [],
        context=context,
        max_retries=2,
    )
