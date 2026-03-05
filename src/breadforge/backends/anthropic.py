"""Anthropic Claude backend.

Uses the ``anthropic`` SDK (already a project dependency) for direct
text-completion calls.  This backend is used by plan nodes and, when
``BREADFORGE_RESEARCH_BACKEND=anthropic`` (the default), by research nodes
that prefer SDK calls over a subprocess.
"""

from __future__ import annotations

import os

from .base import BackendResponse


class AnthropicBackend:
    """LLM backend backed by Anthropic Claude models."""

    def __init__(
        self,
        model: str = "claude-sonnet-4-6",
        api_key: str | None = None,
    ) -> None:
        self._model = model
        self._api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")

    async def complete(
        self,
        prompt: str,
        *,
        max_tokens: int = 2048,
        system: str | None = None,
    ) -> BackendResponse:
        import anthropic

        client = anthropic.AsyncAnthropic(api_key=self._api_key)
        kwargs: dict = dict(
            model=self._model,
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )
        if system:
            kwargs["system"] = system

        response = await client.messages.create(**kwargs)
        text: str = response.content[0].text  # type: ignore[union-attr]
        return BackendResponse(
            content=text,
            model=response.model,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
        )
