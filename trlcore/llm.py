"""LLMプロバイダ。既定は none(評価をせず収集のみ)。課金が発生し得るため明示設定が必要。"""

import json
import os
import re
from .errors import LLMError
from .http import http_fetch
from .config import LLM_DEFAULTS


SYSTEM = "あなたは根拠を重視する投資分析アシスタントです。指定されたJSON形式のみで答え、推測で情報を補いません。"


def extract_json(text):
    t = (text or "").strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t)
    a, b = t.find("{"), t.rfind("}")
    if a < 0 or b <= a:
        raise LLMError("no JSON object in response")
    try:
        obj = json.loads(t[a:b + 1])
    except json.JSONDecodeError as e:
        raise LLMError(f"invalid JSON: {e.msg}") from None
    if not isinstance(obj, dict):
        raise LLMError("JSON root must be an object")
    return obj


class Anthropic:
    name = "anthropic"

    def __init__(self, model=None, http=None):
        self.model = model or os.environ.get("TRL_LLM_MODEL") or "claude-haiku-5-5"
        self.http = http or http_fetch
        self.timeout, self.retries = 120, 2  # configure_llm() で scoring.json の llm 設定に上書き
        self.key = os.environ.get("ANTHROPIC_API_KEY")
        if not self.key:
            raise LLMError("ANTHROPIC_API_KEY is not set")

    def complete(self, prompt):
        body = json.dumps({"model": self.model, "max_tokens": 3000, "system": SYSTEM,
                           "messages": [{"role": "user", "content": prompt}]}).encode()
        raw = self.http("https://api.anthropic.com/v1/messages", data=body, headers={
            "x-api-key": self.key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            timeout=self.timeout, retries=self.retries)
        try:
            return "".join(b.get("text", "") for b in json.loads(raw)["content"])
        except (KeyError, ValueError, TypeError):
            raise LLMError("unexpected Anthropic response shape") from None


class Gemini:
    name = "gemini"

    def __init__(self, model=None, http=None):
        self.model = model or os.environ.get("TRL_LLM_MODEL") or "gemini-3.8-flash"
        self.http = http or http_fetch
        self.timeout, self.retries = 120, 2  # configure_llm() で scoring.json の llm 設定に上書き
        self.key = os.environ.get("GEMINI_API_KEY")
        if not self.key:
            raise LLMError("GEMINI_API_KEY is not set")

    def complete(self, prompt):
        body = json.dumps({
            "systemInstruction": {"parts": [{"text": SYSTEM}]},
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"responseMimeType": "application/json", "temperature": 0}}).encode()
        raw = self.http(f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent",
                        data=body, headers={"x-goog-api-key": self.key, "content-type": "application/json"},
                        timeout=self.timeout, retries=self.retries)
        try:
            parts = json.loads(raw)["candidates"][0]["content"]["parts"]
            return "".join(p.get("text", "") for p in parts)
        except (KeyError, IndexError, ValueError, TypeError):
            raise LLMError("unexpected Gemini response shape") from None


class Mock:
    """テスト用。fn(prompt) -> str。"""
    name = "mock"
    model = "mock-1"

    def __init__(self, fn):
        self.fn = fn

    def complete(self, prompt):
        return self.fn(prompt)


def get_provider(name, model=None, http=None):
    name = (name or "none").lower()
    if name == "none":
        return None
    if name == "anthropic":
        return Anthropic(model, http)
    if name == "gemini":
        return Gemini(model, http)
    raise LLMError(f"unknown provider: {name}")


def llm_settings(cfg):
    """scoring.json の llm セクション(無ければ既定値)。"""
    d = dict(LLM_DEFAULTS)
    d.update(cfg["scoring"].get("llm") or {})
    return d


def configure_llm(llm, cfg):
    """LLMのタイムアウト・再試行回数を設定に合わせる(Mock等は対象外)。"""
    if llm is not None and hasattr(llm, "timeout"):
        st = llm_settings(cfg)
        llm.timeout, llm.retries = st["timeout_sec"], st["retries"]
    return llm
