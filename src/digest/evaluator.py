"""Gemini API による自動評価。失敗時は None を返し、呼び出し側でフォールバックする。"""
from __future__ import annotations

import logging
import os

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from .sanitize import sanitize_markdown_html

log = logging.getLogger("evaluator")

GEMINI_ENDPOINT = (
    "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
)


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=2, min=2, max=20))
def _call_gemini(api_key: str, model: str, prompt: str, cfg: dict) -> str:
    url = GEMINI_ENDPOINT.format(model=model)
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": float(cfg.get("temperature", 0.3)),
            "maxOutputTokens": int(cfg.get("max_output_tokens", 8192)),
        },
    }
    r = httpx.post(
        url,
        headers={
            "x-goog-api-key": api_key,
            "Content-Type": "application/json",
        },
        json=body,
        timeout=float(cfg.get("timeout_seconds", 120)),
    )
    r.raise_for_status()
    data = r.json()
    parts = data.get("candidates", [{}])[0].get("content", {}).get("parts", [])
    text = "\n".join(p.get("text", "") for p in parts if p.get("text"))
    if not text.strip():
        raise ValueError("Gemini returned empty text")
    return text


def evaluate(prompt_input: str, evaluator_cfg: dict) -> str | None:
    """設定に応じて自動評価を実行する。失敗時は None。"""
    provider = evaluator_cfg.get("provider", "manual")
    if provider != "gemini":
        log.info("evaluator skipped provider=%s", provider)
        return None

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        log.warning("GEMINI_API_KEY is not set; skip auto evaluation")
        return None

    gcfg = evaluator_cfg.get("gemini", {})
    model = gcfg.get("model", "gemini-2.5-flash")
    try:
        raw = _call_gemini(api_key, model, prompt_input, gcfg)
        return sanitize_markdown_html(raw)
    except Exception as e:
        log.warning("gemini evaluation failed: %s", e)
        return None