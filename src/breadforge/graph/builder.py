"""Graph builder — creates initial ExecutionGraph from a spec + milestone.

Three entry points:
  build_greenfield_graph  — new project, starts with a plan node
  build_feature_graph     — feature on existing codebase, starts with a plan node
  build_bug_graph         — bug fix, starts with a research node then plan
"""

from __future__ import annotations

from pathlib import Path

from breadforge.beads.types import GraphNode
from breadforge.graph.executor import ExecutionGraph


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
