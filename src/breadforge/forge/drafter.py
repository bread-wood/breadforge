"""Spec drafter — LLM-based spec generation."""

from __future__ import annotations

_DRAFT_PROMPT = """You are a platform spec writer for the bread-wood platform.

Platform context:
{context}

Feature request: {description}

Draft a milestone spec following this exact structure:

# <Project> v<X.Y.Z> — <Milestone Name>

## Overview
<1-3 paragraphs: what, why, key architectural decisions>

## Success Criteria
- [ ] <measurable acceptance criterion>
- [ ] <...>

## Scope
### Included
- <concrete deliverable>

### Excluded
- <explicit non-goal>

## Key Unknowns
- **[P1]** <open question that must be answered before impl>

## Modules
- <module-name>: <one-line description of what it does>

Rules:
- Identify which existing repo(s) this belongs to, or propose a new one
- Keep it product-focused (what + why), not technical (not how)
- Success Criteria must be testable/verifiable
- Key Unknowns must have P0-P4 priority labels
- If the feature spans multiple repos, produce one spec per repo
"""


async def _draft_spec(description: str, context: str) -> str:
    """Draft a spec via LLM."""
    prompt = _DRAFT_PROMPT.format(description=description, context=context[:8000])

    try:
        from breadmin_llm.registry import ProviderRegistry
        from breadmin_llm.types import LLMCall, LLMMessage, MessageRole

        registry = ProviderRegistry.default()
        call = LLMCall(
            model="claude-sonnet-4-6",
            messages=[LLMMessage(role=MessageRole.USER, content=prompt)],
            max_tokens=4000,
            caller="breadforge.forge",
        )
        result = await registry.complete(call)
        return result.content
    except ImportError:
        pass

    import anthropic

    client = anthropic.AsyncAnthropic()
    response = await client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=4000,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text  # type: ignore[union-attr]
