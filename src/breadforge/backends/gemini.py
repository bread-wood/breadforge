"""Google Gemini backend.

Requires ``google-generativeai`` (not a project dependency by default).
The SDK is synchronous, so calls are offloaded to a thread pool via
``asyncio.to_thread`` to stay compatible with the async handler pipeline.

Install::

    pip install google-generativeai
"""

from __future__ import annotations

import asyncio
import os

from .base import BackendResponse


class GeminiBackend:
    """LLM backend backed by Google Gemini models."""

    def __init__(
        self,
        model: str = "gemini-2.0-flash",
        api_key: str | None = None,
    ) -> None:
        self._model = model
        self._api_key = api_key or os.environ.get("GOOGLE_API_KEY")

    async def complete(
        self,
        prompt: str,
        *,
        max_tokens: int = 2048,
        system: str | None = None,
    ) -> BackendResponse:
        try:
            import google.generativeai as genai
        except ImportError as exc:
            raise RuntimeError(
                "google-generativeai is not installed; run: pip install google-generativeai"
            ) from exc

        genai.configure(api_key=self._api_key)
        full_prompt = f"{system}\n\n{prompt}" if system else prompt
        gen_model = genai.GenerativeModel(self._model)
        generation_config = genai.types.GenerationConfig(max_output_tokens=max_tokens)

        response = await asyncio.to_thread(
            gen_model.generate_content,
            full_prompt,
            generation_config=generation_config,
        )
        return BackendResponse(
            content=response.text,
            model=self._model,
        )
