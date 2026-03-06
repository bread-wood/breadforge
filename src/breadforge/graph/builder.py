"""Graph builder — creates initial ExecutionGraph from a spec + milestone.

Core entry points:
  build_greenfield_graph       — new project, starts with a plan node
  build_feature_graph          — feature on existing codebase, starts with a plan node
  build_bug_graph              — bug fix, starts with a research node then plan

Cross-repo blocking:
  apply_cross_repo_blocking    — inject wait nodes for milestones that depend on
                                 other repos/milestones via CampaignBead.blocked_by.
  build_graph_with_blocking    — wraps the above, inserting wait nodes for any
                                 upstream milestones listed in CampaignBead.blocked_by
                                 that have not yet shipped.

Consensus/design-doc helpers:
  emit_consensus_node          — emit a consensus node that votes over proposals
  emit_design_doc_node         — emit a design_doc node for a given task

Validate/bug helpers:
  emit_validate_node           — emit a validate node that runs spec assertions
  emit_bug_node                — emit a bug node for a failed validation assertion
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from breadforge.beads.types import CampaignBead, GraphNode
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
    """Initial graph for a new project: plan node → ... → readme → validate."""
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
    readme_node_id = f"{milestone}-readme"
    validate_node = emit_validate_node(
        milestone=milestone,
        depends_on=[readme_node_id],
        spec_file=str(spec_file),
    )
    return ExecutionGraph([plan_node, validate_node])


def build_feature_graph(
    milestone: str,
    spec_file: str | Path,
    repo: str,
    repo_local_path: str | None = None,
) -> ExecutionGraph:
    """Initial graph for a feature on an existing codebase: plan node → ... → readme → validate."""
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
    readme_node_id = f"{milestone}-readme"
    validate_node = emit_validate_node(
        milestone=milestone,
        depends_on=[readme_node_id],
        spec_file=str(spec_file),
    )
    return ExecutionGraph([plan_node, validate_node])


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
# Cross-repo blocking support (campaign-aware wrapper)
# ---------------------------------------------------------------------------

_GraphType = Literal["greenfield", "feature", "bug"]


def build_graph_with_blocking(
    milestone: str,
    spec_file: str | Path,
    repo: str,
    campaign: CampaignBead,
    repo_local_path: str | None = None,
    milestone_issue_number: int | None = None,
    graph_type: _GraphType = "greenfield",
    bug_description: str | None = None,
) -> ExecutionGraph:
    """Build an execution graph that respects cross-repo blocking deps.

    Checks CampaignBead for any upstream milestones that must ship before
    ``milestone`` can begin.  For each unshipped blocker, a ``wait`` node is
    prepended to the graph and the plan node is made to depend on it.

    Args:
        milestone: The milestone slug being built.
        spec_file: Path to the spec file.
        repo: ``owner/repo`` string.
        campaign: CampaignBead that carries ``blocked_by`` metadata.
        repo_local_path: Local checkout path (greenfield / bug only).
        milestone_issue_number: GitHub issue number for progress comments.
        graph_type: One of ``"greenfield"``, ``"feature"``, ``"bug"``.
        bug_description: Required when ``graph_type="bug"``.

    Returns:
        An :class:`ExecutionGraph` with wait nodes prepended for any
        unshipped upstream milestones.
    """
    # Build the base graph for this milestone type
    if graph_type == "bug":
        if not bug_description:
            raise ValueError("bug_description is required when graph_type='bug'")
        base = build_bug_graph(milestone, spec_file, repo, bug_description, repo_local_path)
    elif graph_type == "feature":
        base = build_feature_graph(milestone, spec_file, repo, repo_local_path)
    else:
        base = build_greenfield_graph(
            milestone, spec_file, repo, repo_local_path, milestone_issue_number
        )

    # Find unshipped upstream milestones
    unshipped = _unshipped_blockers(campaign, milestone, repo)
    if not unshipped:
        return base

    # Create one wait node per unshipped blocker
    wait_nodes = [_make_blocker_wait_node(blocker, milestone) for blocker in unshipped]
    wait_ids = [n.id for n in wait_nodes]

    # Patch the plan node (and any root research node) to depend on wait nodes
    for node in base.all_nodes():
        if node.type in ("plan", "research") and not node.depends_on:
            node.depends_on = list(node.depends_on) + wait_ids

    base.add_nodes(wait_nodes)
    return base


def _unshipped_blockers(
    campaign: CampaignBead,
    milestone: str,
    repo: str | None = None,
) -> list[str]:
    """Return 'owner/repo:milestone' blockers that are not yet shipped."""
    plan = campaign.get_milestone(milestone, repo)
    if plan is None:
        return []

    unshipped = []
    for blocker in plan.blocked_by:
        if _is_blocker_unshipped(blocker, campaign):
            unshipped.append(blocker)
    return unshipped


def _is_blocker_unshipped(blocker: str, campaign: CampaignBead) -> bool:
    """Return True if the blocking milestone has not yet shipped."""
    if ":" not in blocker:
        return True  # malformed — treat as blocking
    repo_part, milestone_part = blocker.rsplit(":", 1)
    upstream = campaign.get_milestone(milestone_part, repo_part)
    if upstream is None:
        return True  # unknown upstream — treat as blocking
    return upstream.status != "shipped"


def _make_blocker_wait_node(blocker: str, milestone: str) -> GraphNode:
    """Return a wait GraphNode for a cross-repo blocking dep.

    Uses ``model_construct`` to bypass the NodeType Literal check since
    ``"wait"`` is not in the standard NodeType enumeration.
    """
    safe = blocker.replace("/", "-").replace(":", "-")
    return GraphNode.model_construct(
        id=f"{milestone}-wait-blocker-{safe}",
        type="wait",
        state="pending",
        depends_on=[],
        context={"condition": "always_true", "blocker": blocker},
        output=None,
        assigned_model=None,
        retry_count=0,
        max_retries=3,
        created_at=datetime.now(UTC),
        started_at=None,
        completed_at=None,
    )


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


# ---------------------------------------------------------------------------
# Validate and bug node emitters
# ---------------------------------------------------------------------------


def emit_validate_node(
    milestone: str,
    depends_on: list[str] | None = None,
    assertions: list[str] | None = None,
    spec_markdown: str = "",
    spec_file: str = "",
    tracking_issue_number: int | None = None,
) -> GraphNode:
    """Create a validate node that runs spec assertions after build completes.

    The validate handler accepts assertions via three sources (in priority order):
    1. ``assertions`` — pre-parsed list of shell commands
    2. ``spec_markdown`` — raw spec text to parse at execution time
    3. ``spec_file`` — path stored in context for the handler to read

    Args:
        milestone: Milestone slug used to generate the node id.
        depends_on: Node ids this validate node waits on (typically the readme node).
        assertions: Pre-parsed assertion shell commands.
        spec_markdown: Raw spec text from which assertions are extracted.
        spec_file: Path to the spec file (stored in context for reference).
        tracking_issue_number: GitHub issue number to close when all assertions pass.
    """
    context: dict = {
        "milestone": milestone,
        "fix_cycles": {},
    }
    if assertions:
        context["assertions"] = assertions
    if spec_markdown:
        context["spec_markdown"] = spec_markdown
    if spec_file:
        context["spec_file"] = spec_file
    if tracking_issue_number is not None:
        context["tracking_issue_number"] = tracking_issue_number
    return make_node(
        id=f"{milestone}-validate",
        type="validate",
        depends_on=depends_on or [],
        context=context,
        max_retries=3,
    )


def emit_bug_node(
    milestone: str,
    command: str,
    exit_code: int,
    stdout: str,
    stderr: str,
    depends_on: list[str] | None = None,
    module: str = "",
    files: list[str] | None = None,
    issue_title: str = "",
) -> GraphNode:
    """Create a bug node for a failed validation assertion.

    The bug handler reads context keys ``command``, ``stdout``, ``stderr``,
    ``exit_code``, ``milestone``, ``module``, and ``files`` to file a GitHub
    issue and emit a remedial build node.

    Args:
        milestone: Milestone slug; used for the node id and GitHub milestone label.
        command: The shell command that failed (e.g. ``"uv run pytest"``).
        exit_code: Exit code returned by the failing command.
        stdout: Captured stdout (truncated to 2000 chars in the node context).
        stderr: Captured stderr (truncated to 2000 chars in the node context).
        depends_on: Node ids this bug node waits on.
        module: Module label for the filed GitHub issue and build node scope.
        files: File scope forwarded to the remedial build node context.
        issue_title: Override for the filed issue title; defaults to a generated title.
    """
    import re

    slug = re.sub(r"[^a-zA-Z0-9]", "-", command)[:30].strip("-").lower()
    context: dict = {
        "command": command,
        "exit_code": exit_code,
        # Truncate to avoid bloating the bead store with huge outputs.
        "stdout": stdout[:2000],
        "stderr": stderr[:2000],
        "milestone": milestone,
        "module": module,
        "files": files or [],
    }
    if issue_title:
        context["issue_title"] = issue_title
    return make_node(
        id=f"{milestone}-bug-{slug}",
        type="bug",
        depends_on=depends_on or [],
        context=context,
        max_retries=3,
    )
