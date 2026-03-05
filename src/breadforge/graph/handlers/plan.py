"""PlanHandler — SDK call that reads spec + codebase + research, emits new nodes.

Uses Anthropic SDK directly (not subprocess) — plan output is structured JSON,
faster and cheaper than claude --print for structured extraction.

Confidence < PLAN_CONFIDENCE_THRESHOLD → emit research nodes first.
Confidence >= threshold → emit build + merge nodes directly.
"""

from __future__ import annotations

import contextlib
import json
from pathlib import Path
from typing import TYPE_CHECKING

from breadforge.beads.types import GraphNode, PlanArtifact
from breadforge.graph.node import NodeResult

if TYPE_CHECKING:
    from breadforge.beads.store import BeadStore
    from breadforge.config import Config
    from breadforge.logger import Logger

PLAN_CONFIDENCE_THRESHOLD = 0.6


def _read_codebase_summary(repo_local_path: str | None) -> str:
    """Assess the current codebase: what's already implemented vs what's missing.

    Produces a structured summary for the plan LLM covering:
    - CLAUDE.md project instructions
    - pyproject.toml dependencies and entry points
    - Source inventory: each Python file with its classes and top-level functions
    - Test coverage: which packages have tests and how many
    """
    if not repo_local_path:
        return ""
    import re

    root = Path(repo_local_path)
    parts: list[str] = []

    # 1. CLAUDE.md
    claude_md = root / "CLAUDE.md"
    if claude_md.exists():
        with contextlib.suppress(OSError):
            parts.append("=== CLAUDE.md ===\n" + claude_md.read_text(encoding="utf-8")[:2000])

    # 2. pyproject.toml — installed packages and dependencies
    for pyproject in sorted(root.rglob("pyproject.toml"))[:6]:
        with contextlib.suppress(OSError):
            text = pyproject.read_text(encoding="utf-8")[:800]
            parts.append(f"=== {pyproject.relative_to(root)} ===\n{text}")

    # 3. Source inventory — walk Python source files, extract symbols
    src_dirs = [d for name in ("src", "packages") if (d := root / name).is_dir()]
    if not src_dirs:
        src_dirs = [root]

    inventory_lines: list[str] = []
    py_files = sorted(
        f for sd in src_dirs for f in sd.rglob("*.py")
        if "test" not in f.parts and "__pycache__" not in f.parts
    )[:150]

    for py_file in py_files:
        with contextlib.suppress(OSError):
            text = py_file.read_text(encoding="utf-8")
        else:
            continue
        rel = py_file.relative_to(root)
        classes = re.findall(r"^class (\w+)", text, re.MULTILINE)
        funcs = re.findall(r"^(?:async )?def (\w+)", text, re.MULTILINE)
        # Skip private/dunder-only files
        public_funcs = [f for f in funcs if not f.startswith("_")]
        if not text.strip() or (not classes and not public_funcs):
            inventory_lines.append(f"  {rel}  (stub/empty)")
        else:
            parts_line = str(rel)
            if classes:
                parts_line += f"  classes=[{', '.join(classes[:6])}]"
            if public_funcs:
                parts_line += f"  fns=[{', '.join(public_funcs[:8])}]"
            inventory_lines.append(f"  {parts_line}")

    if inventory_lines:
        parts.append("=== Source inventory (what is already implemented) ===\n" + "\n".join(inventory_lines))

    # 4. Test coverage
    test_summary: list[str] = []
    for tests_dir in sorted(root.rglob("tests"))[:10]:
        if not tests_dir.is_dir():
            continue
        test_files = list(tests_dir.glob("test_*.py"))
        if test_files:
            test_summary.append(f"  {tests_dir.relative_to(root)}: {len(test_files)} test file(s)")
    if test_summary:
        parts.append("=== Test coverage ===\n" + "\n".join(test_summary))

    return "\n\n".join(parts)


def _gather_research_findings(
    research_node_ids: list[str],
    store: BeadStore | None,
) -> str:
    if not store or not research_node_ids:
        return ""
    parts = []
    for nid in research_node_ids:
        findings = store.read_research_findings(nid)
        if findings:
            parts.append(f"=== Research: {nid} ===\n{findings}")
    return "\n\n".join(parts)


async def _call_plan_llm(
    spec_text: str,
    codebase_ctx: str,
    research_findings: str,
    model: str,
) -> PlanArtifact:
    """Call Anthropic SDK to get a structured PlanArtifact."""
    from breadforge.agents.prompts import PLAN_PROMPT

    prompt = PLAN_PROMPT.format(
        spec_text=spec_text[:4000],
        codebase_context=codebase_ctx[:6000],
        research_findings=research_findings[:2000],
    )

    try:
        from breadmin_llm.registry import ProviderRegistry
        from breadmin_llm.types import LLMCall, LLMMessage, MessageRole

        registry = ProviderRegistry.default()
        call = LLMCall(
            model=model,
            messages=[LLMMessage(role=MessageRole.USER, content=prompt)],
            max_tokens=1500,
            caller="breadforge.plan",
        )
        response = await registry.complete(call)
        text = response.content
    except ImportError:
        import anthropic

        client = anthropic.AsyncAnthropic()
        response = await client.messages.create(
            model=model,
            max_tokens=1500,
            messages=[{"role": "user", "content": prompt}],
        )
        text = response.content[0].text  # type: ignore[union-attr]

    # Strip markdown fences
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        text = "\n".join(line for line in lines if not line.startswith("```")).strip()

    data = json.loads(text)
    return PlanArtifact.model_validate(data)


def _emit_research_nodes(
    unknowns: list[str],
    parent_node: GraphNode,
    milestone_slug: str,
    repo: str,
) -> list[GraphNode]:
    """Emit one research node covering all unknowns."""
    node_id = f"{milestone_slug}-research-{parent_node.id}"
    return [
        GraphNode(
            id=node_id,
            type="research",
            context={
                "milestone": milestone_slug,
                "repo": repo,
                "unknowns": unknowns,
            },
        )
    ]


def _emit_plan_refine_node(
    parent_node: GraphNode,
    artifact: PlanArtifact,
    research_nodes: list[GraphNode],
    spec_file: str,
    repo_local_path: str,
    repo: str,
    milestone_slug: str,
) -> GraphNode:
    """Emit a refined plan node that depends on the research nodes."""
    return GraphNode(
        id=f"{milestone_slug}-plan-refine",
        type="plan",
        depends_on=[n.id for n in research_nodes],
        context={
            "milestone": milestone_slug,
            "spec_file": spec_file,
            "repo": repo,
            "repo_local_path": repo_local_path,
            "research_node_ids": [n.id for n in research_nodes],
            "prior_artifact": artifact.model_dump(),
        },
    )


def _slug(s: str) -> str:
    """Sanitize a string for use in node IDs (no spaces, safe chars only)."""
    import re

    return re.sub(r"[^a-zA-Z0-9._-]", "-", s).strip("-")


def _comment_on_issue(repo: str, issue_number: int | None, body: str) -> None:
    """Post a progress comment on the milestone issue. No-op if no issue number."""
    if not issue_number:
        return
    import subprocess

    subprocess.run(
        ["gh", "issue", "comment", str(issue_number), "--repo", repo, "--body", body],
        capture_output=True,
        text=True,
    )


def _file_module_issue(repo: str, module: str, milestone_slug: str, artifact: PlanArtifact) -> int | None:
    """File a GitHub issue for a build module. Returns issue number or None on failure."""
    import subprocess

    files = artifact.files_per_module.get(module, [])
    body = (
        f"**Milestone:** {milestone_slug}\n"
        f"**Module:** `{module}`\n\n"
        f"**Approach:** {artifact.approach}\n\n"
        f"**Files to create/modify:**\n"
        + "\n".join(f"- `{f}`" for f in files)
    )
    result = subprocess.run(
        [
            "gh", "issue", "create",
            "--repo", repo,
            "--title", f"impl({milestone_slug}): {module} module",
            "--body", body,
            "--label", "stage/impl",
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    # gh issue create prints the URL on stdout; extract number from URL
    url = result.stdout.strip()
    try:
        return int(url.rstrip("/").split("/")[-1])
    except (ValueError, IndexError):
        return None


def _emit_build_nodes(
    artifact: PlanArtifact,
    repo: str,
    milestone_slug: str,
    milestone_issue_number: int | None = None,
) -> list[GraphNode]:
    """Emit one build node per module, filing a GH issue for each."""
    nodes = []
    for module in artifact.modules:
        files = artifact.files_per_module.get(module, [])
        module_slug = _slug(module)
        issue_number = _file_module_issue(repo, module, milestone_slug, artifact)
        context: dict = {
            "milestone": milestone_slug,
            "module": module,
            "files": files,
            "repo": repo,
            "plan_artifact": artifact.model_dump(),
            "milestone_issue_number": milestone_issue_number,
        }
        if issue_number:
            context["issue_number"] = issue_number
            context["issue_title"] = f"impl({milestone_slug}): {module} module"
        nodes.append(
            GraphNode(
                id=f"{milestone_slug}-build-{module_slug}",
                type="build",
                context=context,
            )
        )
    return nodes


def _emit_readme_node(
    merge_nodes: list[GraphNode],
    artifact: PlanArtifact,
    milestone_slug: str,
    repo: str,
) -> GraphNode:
    """Emit a readme node that runs after all merges complete."""
    return GraphNode(
        id=f"{milestone_slug}-readme",
        type="readme",
        depends_on=[n.id for n in merge_nodes],
        context={
            "milestone": milestone_slug,
            "repo": repo,
            "plan_artifact": artifact.model_dump(),
        },
    )


def _emit_merge_nodes(build_nodes: list[GraphNode]) -> list[GraphNode]:
    """Emit one merge node per build node, depending on it."""
    nodes = []
    for build_node in build_nodes:
        nodes.append(
            GraphNode(
                id=f"{build_node.id}-merge",
                type="merge",
                depends_on=[build_node.id],
                max_retries=20,  # CI may take up to ~20 minutes; handler sleeps 60s between attempts
                context={
                    "build_node_id": build_node.id,
                },
            )
        )
    return nodes


class PlanHandler:
    """Calls LLM to produce a PlanArtifact, then expands the graph."""

    def __init__(
        self,
        store: BeadStore | None = None,
        logger: Logger | None = None,
    ) -> None:
        self._store = store
        self._logger = logger

    async def execute(self, node: GraphNode, config: Config) -> NodeResult:
        spec_file = node.context.get("spec_file", "")
        repo_local_path = node.context.get("repo_local_path", "")
        research_node_ids: list[str] = node.context.get("research_node_ids", [])
        milestone = node.context.get("milestone", "")
        repo = config.repo

        # Read spec
        try:
            spec_text = Path(spec_file).read_text(encoding="utf-8") if spec_file else ""
        except OSError as e:
            return NodeResult(success=False, error=f"could not read spec: {e}")

        codebase_ctx = _read_codebase_summary(repo_local_path)
        research_findings = _gather_research_findings(research_node_ids, self._store)

        # Prefer opus for planning if model is sonnet (planning deserves capability)
        plan_model = config.model
        if plan_model == "claude-sonnet-4-6":
            plan_model = "claude-sonnet-4-6"  # keep sonnet as default; caller can override

        try:
            artifact = await _call_plan_llm(spec_text, codebase_ctx, research_findings, plan_model)
        except Exception as e:
            return NodeResult(success=False, error=f"plan LLM call failed: {e}")

        if self._logger:
            self._logger.info(
                f"plan {node.id}: confidence={artifact.confidence:.2f}, modules={artifact.modules}",
                node_id=node.id,
            )

        new_nodes: list[GraphNode] = []
        # Always use the slug from context for node IDs — the LLM may return the full title
        milestone_slug = milestone
        milestone_issue_number: int | None = node.context.get("milestone_issue_number")

        if artifact.confidence < PLAN_CONFIDENCE_THRESHOLD and artifact.unknowns:
            research_nodes = _emit_research_nodes(artifact.unknowns, node, milestone_slug, repo)
            refine_node = _emit_plan_refine_node(
                node, artifact, research_nodes, spec_file, repo_local_path, repo, milestone_slug
            )
            new_nodes = research_nodes + [refine_node]
            _comment_on_issue(
                repo,
                milestone_issue_number,
                f"**Plan:** low confidence ({artifact.confidence:.0%}) — filing research tasks first.\n\n"
                f"Unknowns: {', '.join(artifact.unknowns[:3])}",
            )
        else:
            build_nodes = _emit_build_nodes(
                artifact, repo, milestone_slug, milestone_issue_number
            )
            merge_nodes = _emit_merge_nodes(build_nodes)
            readme_node = _emit_readme_node(merge_nodes, artifact, milestone_slug, repo)
            new_nodes = build_nodes + merge_nodes + [readme_node]
            module_links = "\n".join(
                f"- `{m}` → #{n.context['issue_number']}"
                if n.context.get("issue_number")
                else f"- `{m}`"
                for m, n in zip(artifact.modules, build_nodes)
            )
            _comment_on_issue(
                repo,
                milestone_issue_number,
                f"**Plan complete** (confidence {artifact.confidence:.0%})\n\n"
                f"{artifact.approach}\n\n"
                f"**Modules:**\n{module_links}",
            )

        return NodeResult(
            success=True,
            output={
                "artifact": artifact.model_dump(),
                "new_nodes": [n.model_dump(mode="json") for n in new_nodes],
            },
        )
