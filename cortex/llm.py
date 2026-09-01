"""Pluggable LLM access (NFR-3): any OpenAI-compatible endpoint (DeepSeek API,
local ollama). DISABLED until CORTEX_LLM_BASE_URL is configured — raw events
remain fully functional without it."""

from __future__ import annotations

import json
from typing import Any

import httpx

from .config import get_config
from .log import get_logger

log = get_logger(__name__)


async def chat(messages: list[dict[str, str]], *, max_tokens: int = 2000,
               json_mode: bool = False) -> str:
    """One chat completion against the configured OpenAI-compatible endpoint."""
    cfg = get_config()
    if not cfg.llm_enabled:
        raise RuntimeError("LLM extraction is disabled (CORTEX_LLM_BASE_URL empty)")
    body: dict[str, Any] = {
        "model": cfg.llm_model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": 0.1,
    }
    if json_mode and cfg.llm_json_mode:
        body["response_format"] = {"type": "json_object"}
    headers = {"Authorization": f"Bearer {cfg.llm_api_key}"} if cfg.llm_api_key else {}
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(f"{cfg.llm_base_url}/chat/completions",
                              json=body, headers=headers)
        r.raise_for_status()
        data = r.json()
    return data["choices"][0]["message"]["content"]


async def summarize_text(text: str) -> str:
    prompt = (
        "Summarize the following page for an AI agent's research archive. "
        "3–6 sentences, factual, keep names/identifiers. Output plain text only.\n\n" + text
    )
    return await chat([{"role": "user", "content": prompt}], max_tokens=400)


async def extract_json(system: str, user: str) -> dict[str, Any]:
    """JSON-schema-constrained extraction call (FR-5). Parses defensively."""
    raw = await chat(
        [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        max_tokens=2000,
        json_mode=True,
    )
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        # models sometimes wrap JSON in fences
        start, end = raw.find("{"), raw.rfind("}")
        if start >= 0 and end > start:
            return json.loads(raw[start : end + 1])
        raise
