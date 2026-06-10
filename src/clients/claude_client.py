"""Anthropic Claude client.

The system prompt (from prompts/system_report.md) is stable across runs and is
sent with cache_control {"type": "ephemeral"} so it can be served from the
prompt cache. The volatile per-day data goes in the USER message, which keeps
the cached prefix byte-identical between runs.
"""
from __future__ import annotations

import logging

import anthropic

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-sonnet-4-6"
DEFAULT_MAX_TOKENS = 2000


class ClaudeClient:
    def __init__(self, api_key: str, model: str = DEFAULT_MODEL) -> None:
        if not api_key:
            raise ValueError("api_key is required")
        self._client = anthropic.Anthropic(api_key=api_key)
        self._model = model or DEFAULT_MODEL

    def generate(
        self,
        system_prompt: str,
        user_content: str,
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> str:
        if not system_prompt:
            raise ValueError("system_prompt is required")
        try:
            response = self._client.messages.create(
                model=self._model,
                max_tokens=max_tokens,
                system=[
                    {
                        "type": "text",
                        "text": system_prompt,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                messages=[{"role": "user", "content": user_content}],
            )
        except anthropic.APIError as exc:
            logger.error("Claude API call failed: %s", exc)
            raise RuntimeError("Failed to generate report with Claude") from exc

        usage = getattr(response, "usage", None)
        if usage is not None:
            logger.info(
                "Claude usage: input=%s cache_read=%s cache_write=%s output=%s",
                getattr(usage, "input_tokens", None),
                getattr(usage, "cache_read_input_tokens", None),
                getattr(usage, "cache_creation_input_tokens", None),
                getattr(usage, "output_tokens", None),
            )

        text = "\n".join(
            block.text
            for block in response.content
            if getattr(block, "type", None) == "text"
        ).strip()
        if not text:
            raise RuntimeError("Claude returned an empty response")
        return text
