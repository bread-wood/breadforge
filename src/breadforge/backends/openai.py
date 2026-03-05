"""OpenAI GPT backend.

Requires ``openai`` (not a project dependency by default).

Install::

    pip install openai
"""

from __future__ import annotations

import os

from .base import BackendResponse


class OpenAIBackend:
    """LLM backend backed by OpenAI GPT models."""

    def __init__(
        self,
        model: str = "gpt-4.1",
        api_key: str | None = None,
    ) -> None:
        self._model = model
        self._api_key = api_key or os.environ.get("OPENAI_API_KEY")

    async def complete(
        self,
        prompt: str,
        *,
        max_tokens: int = 2048,
        system: str | None = None,
    ) -> BackendResponse:
        try:
            import openai
        except ImportError as exc:
            raise RuntimeError("openai is not installed; run: pip install openai") from exc

        client = openai.AsyncOpenAI(api_key=self._api_key)
        messages: list[dict] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        response = await client.chat.completions.create(
            model=self._model,
            max_tokens=max_tokens,
            messages=messages,
        )
        choice = response.choices[0]
        usage = response.usage
        return BackendResponse(
            content=choice.message.content or "",
            model=response.model,
            input_tokens=usage.prompt_tokens if usage else 0,
            output_tokens=usage.completion_tokens if usage else 0,
        )
