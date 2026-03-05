"""DesignDocHandler — generates a Markdown design document from a PlanArtifact.

Given a PlanArtifact in node.context['plan_artifact'], this handler calls
an LLM to produce a Markdown design document covering architecture, modules,
and implementation approach.

Context keys:
    plan_artifact (dict): serialised PlanArtifact (required)
    milestone (str): milestone slug
    repo (str): owner/repo (falls back to config.repo)
    output_path (str): optional filesystem path to write the doc

The generated document is returned in NodeResult.output['doc'] and optionally
written to the path specified by context['output_path'].
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from breadforge.beads.types import GraphNode, PlanArtifact
from breadforge.graph.node import NodeResult

if TYPE_CHECKING:
    from breadforge.config import Config
    from breadforge.logger import Logger


_DESIGN_DOC_PROMPT = """\
Generate a concise design document for milestone '{milestone}' in repo '{repo}'.

## Plan Overview
{approach}

## Modules
{modules}

## Files per Module
{files_per_module}

Write a Markdown design document that covers:
1. Overview and goals
2. Architecture and key decisions
3. Module breakdown (one section per module)
4. Data flow
5. Risk flags and mitigations

Be concise. Output only the Markdown document, no preamble.
"""


class DesignDocHandler:
    """Generates a Markdown design document from a PlanArtifact.

    Routes to the same LLM as the plan handler (breadmin_llm if available,
    otherwise Anthropic SDK directly).  The generated document is returned
    in NodeResult.output['doc'] and optionally written to output_path.
    """

    def __init__(
        self,
        store=None,
        logger: Logger | None = None,
    ) -> None:
        self._store = store
        self._logger = logger

    async def execute(self, node: GraphNode, config: Config) -> NodeResult:
        plan_artifact_data = node.context.get("plan_artifact")
        milestone: str = node.context.get("milestone", "")
        repo: str = node.context.get("repo") or config.repo
        output_path: str | None = node.context.get("output_path")

        if not plan_artifact_data:
            return NodeResult(
                success=False,
                error="design_doc node requires context['plan_artifact']",
            )

        try:
            artifact = PlanArtifact.model_validate(plan_artifact_data)
        except Exception as e:
            return NodeResult(success=False, error=f"invalid plan_artifact: {e}")

        try:
            doc_content = await self._generate(artifact, milestone, repo, config)
        except Exception as e:
            return NodeResult(success=False, error=f"design doc generation failed: {e}")

        if output_path:
            from pathlib import Path

            try:
                dest = Path(output_path)
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text(doc_content, encoding="utf-8")
            except OSError as e:
                return NodeResult(success=False, error=f"could not write design doc: {e}")

        if self._logger:
            self._logger.info(
                f"design_doc {node.id}: {len(doc_content)} chars",
                node_id=node.id,
                milestone=milestone,
            )

        return NodeResult(
            success=True,
            output={
                "doc": doc_content,
                "milestone": milestone,
                "output_path": output_path,
            },
        )

    def recover(self, node: GraphNode, config: Config) -> NodeResult | None:
        """Re-generate on restart — design docs are idempotent."""
        return None

    async def _generate(
        self,
        artifact: PlanArtifact,
        milestone: str,
        repo: str,
        config: Config,
    ) -> str:
        modules_text = "\n".join(f"- {m}" for m in artifact.modules)
        files_text = "\n".join(
            f"  {m}: {', '.join(files)}" for m, files in artifact.files_per_module.items()
        )
        prompt = _DESIGN_DOC_PROMPT.format(
            milestone=milestone,
            repo=repo,
            approach=artifact.approach,
            modules=modules_text,
            files_per_module=files_text,
        )

        model = config.model

        try:
            from breadmin_llm.registry import ProviderRegistry
            from breadmin_llm.types import LLMCall, LLMMessage, MessageRole

            registry = ProviderRegistry.default()
            call = LLMCall(
                model=model,
                messages=[LLMMessage(role=MessageRole.USER, content=prompt)],
                max_tokens=2000,
                caller="breadforge.design_doc",
            )
            response = await registry.complete(call)
            return response.content
        except ImportError:
            import anthropic

            client = anthropic.AsyncAnthropic()
            response = await client.messages.create(
                model=model,
                max_tokens=2000,
                messages=[{"role": "user", "content": prompt}],
            )
            return response.content[0].text  # type: ignore[union-attr]
