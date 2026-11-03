"""LLMプロバイダ。既定は none(評価をせず収集のみ)。課金が発生し得るため明示設定が必要。"""

import json
import os
import re
import threading
import time
from .errors import HttpError, LLMError, ResponseShapeError
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
    except json.JSONDecodeError:
        # 前後の説明文(括弧を含む文)が混じると最初{〜最後}の切り出しは壊れる。
        # 先頭のオブジェクトだけを読む方法にフォールバックする。
        try:
            obj, _ = json.JSONDecoder().raw_decode(t[a:])
        except json.JSONDecodeError as e:
            raise LLMError(f"invalid JSON: {e.msg}") from None
    if not isinstance(obj, dict):
        raise LLMError("JSON root must be an object")
    return obj


def _emit_call(provider, rec):
    """LLM コールの計装レコードをレコーダへ渡す。記録の失敗で呼び出し自体を落とさない。"""
    fn = getattr(provider, "recorder", None)
    if fn is None:
        return
    try:
        fn(rec)
    except Exception:
        pass

# 主モデルの連続失敗がこの回数に達したら、その実行では代替モデルへ直行する
# (モデル版ブレーカー)。根拠は実測 (2026-11-03): 主モデルが終日不調の日は
# 成功0件で、抽出・採点の全コールが「主モデルの失敗待ち→代替で再実行」の
# 通行料を払った。3回連続の失敗は瞬断では起きにくく、成功で即リセットする
MAIN_FAIL_LIMIT = 3


def _fallback_model(model, fallback_model=None):
    """代替モデル名。未指定なら環境変数 TRL_LLM_FALLBACK_MODEL、主モデルと同じなら無効。"""
    fb = fallback_model or os.environ.get("TRL_LLM_FALLBACK_MODEL") or None
    return fb if fb and fb != model else None


class Anthropic:
    name = "anthropic"

    def __init__(self, model=None, http=None, fallback_model=None):
        self.model = model or os.environ.get("TRL_LLM_MODEL") or "claude-haiku-5-5"
        self.fallback_model = _fallback_model(self.model, fallback_model)
        self.http = http or http_fetch
        self.timeout, self.retries = 120, 2  # configure_llm() で scoring.json の llm 設定に上書き
        self.call_budget = 240  # 同上 (1回の呼び出し全体の時間上限・監査 A-2)
        self.key = os.environ.get("ANTHROPIC_API_KEY")
        if not self.key:
            raise LLMError("ANTHROPIC_API_KEY is not set")
        self._tls = threading.local()  # last_model はスレッドごとに保持する (評価は並列実行)
        self.recorder = None  # 計装レコーダ (run 側が設定する。None なら記録しない)
        self._main_fails = [0]  # 主モデルの連続失敗数 (ブレーカー用)
        self._main_lock = threading.Lock()

    @property
    def last_model(self):
        """直近の complete() が実際に応答したモデル名 (フォールバック時は代替モデル)。
        評価の来歴と抽出キャッシュのキーに使う。未呼び出しなら None。"""
        return getattr(self._tls, "model", None)

    def complete(self, prompt, purpose=None, candidate_id=None):
        if self._breaker_open():
            # ブレーカー作動中: 主モデルの失敗待ち (通行料) を払わず代替へ直行する
            out = self._timed(self.fallback_model, prompt, purpose, candidate_id,
                              main_skipped=True)
            self._tls.model = self.fallback_model
            return out
        try:
            out = self._timed(self.model, prompt, purpose, candidate_id)
            self._note_main_result(True)
            self._tls.model = self.model
            return out
        except HttpError as e:
            self._note_main_result(False)
            # 503/429 等の一時的失敗はモデル側の過負荷であることが多く、同一プロバイダの
            # 別モデルでは応答することがある。恒常的な失敗(404/401等)では切り替えない。
            if not (self.fallback_model and e.transient) or e.deadline_exceeded:
                raise
            out = self._timed(self.fallback_model, prompt, purpose, candidate_id)
            self._tls.model = self.fallback_model
            return out

    def _timed(self, model, prompt, purpose, candidate_id, main_skipped=False):
        """_complete を計測つきで呼ぶ。成否・実モデル・秒数・入出力サイズ・
        トークンをレコーダに残す (後から見返せるものは残す方針)。"""
        t0 = time.monotonic()
        try:
            out = self._complete(model, prompt)
        except Exception as e:
            self._emit(purpose, candidate_id, model, time.monotonic() - t0, False, e, prompt, None,
                       main_skipped)
            raise
        self._emit(purpose, candidate_id, model, time.monotonic() - t0, True, None, prompt, out,
                   main_skipped)
        return out

    def _breaker_open(self):
        return self.fallback_model is not None and self._main_fails[0] >= MAIN_FAIL_LIMIT

    def _note_main_result(self, ok):
        with self._main_lock:
            self._main_fails[0] = 0 if ok else self._main_fails[0] + 1

    def _emit(self, purpose, candidate_id, model, seconds, ok, err, prompt, out,
              main_skipped=False):
        usage = getattr(self._tls, "usage", None)
        self._tls.usage = None
        _emit_call(self, {
            "purpose": purpose, "candidate_id": candidate_id, "provider": self.name,
            "model": model, "seconds": round(seconds, 3), "ok": ok,
            "error": f"{type(err).__name__}: {err}"[:300] if err is not None else None,
            "chars_in": len(prompt), "chars_out": len(out) if out is not None else None,
            "tokens_in": usage[0] if usage else None,
            "tokens_out": usage[1] if usage else None,
            "main_skipped": main_skipped})

    def _complete(self, model, prompt):
        body = json.dumps({"model": model, "max_tokens": 3000, "system": SYSTEM,
                           "messages": [{"role": "user", "content": prompt}]}).encode()
        raw = self.http("https://api.anthropic.com/v1/messages", data=body, headers={
            "x-api-key": self.key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            timeout=self.timeout, retries=self.retries, deadline=self.call_budget)
        try:
            data = json.loads(raw)
            text = "".join(b.get("text", "") for b in data["content"])
        except (KeyError, ValueError, TypeError, AttributeError):
            raise ResponseShapeError("unexpected Anthropic response shape") from None
        u = data.get("usage") or {}
        self._tls.usage = (u.get("input_tokens"), u.get("output_tokens"))
        return text


class Gemini:
    name = "gemini"

    def __init__(self, model=None, http=None, fallback_model=None):
        self.model = model or os.environ.get("TRL_LLM_MODEL") or "gemini-3.8-flash"
        self.fallback_model = _fallback_model(self.model, fallback_model)
        self.http = http or http_fetch
        self.timeout, self.retries = 120, 2  # configure_llm() で scoring.json の llm 設定に上書き
        self.call_budget = 240  # 同上 (1回の呼び出し全体の時間上限・監査 A-2)
        self.key = os.environ.get("GEMINI_API_KEY")
        if not self.key:
            raise LLMError("GEMINI_API_KEY is not set")
        self._tls = threading.local()  # last_model はスレッドごとに保持する (評価は並列実行)
        self.recorder = None  # 計装レコーダ (run 側が設定する。None なら記録しない)
        self._main_fails = [0]  # 主モデルの連続失敗数 (ブレーカー用)
        self._main_lock = threading.Lock()

    @property
    def last_model(self):
        """直近の complete() が実際に応答したモデル名 (フォールバック時は代替モデル)。
        評価の来歴と抽出キャッシュのキーに使う。未呼び出しなら None。"""
        return getattr(self._tls, "model", None)

    def complete(self, prompt, purpose=None, candidate_id=None):
        if self._breaker_open():
            # ブレーカー作動中: 主モデルの失敗待ち (通行料) を払わず代替へ直行する
            out = self._timed(self.fallback_model, prompt, purpose, candidate_id,
                              main_skipped=True)
            self._tls.model = self.fallback_model
            return out
        try:
            out = self._timed(self.model, prompt, purpose, candidate_id)
            self._note_main_result(True)
            self._tls.model = self.model
            return out
        except HttpError as e:
            self._note_main_result(False)
            # 503/429 等の一時的失敗はモデル側の過負荷であることが多く、同一プロバイダの
            # 別モデルでは応答することがある。恒常的な失敗(404/401等)では切り替えない。
            if not (self.fallback_model and e.transient) or e.deadline_exceeded:
                raise
            out = self._timed(self.fallback_model, prompt, purpose, candidate_id)
            self._tls.model = self.fallback_model
            return out

    def _timed(self, model, prompt, purpose, candidate_id, main_skipped=False):
        """_complete を計測つきで呼ぶ。成否・実モデル・秒数・入出力サイズ・
        トークンをレコーダに残す (後から見返せるものは残す方針)。"""
        t0 = time.monotonic()
        try:
            out = self._complete(model, prompt)
        except Exception as e:
            self._emit(purpose, candidate_id, model, time.monotonic() - t0, False, e, prompt, None,
                       main_skipped)
            raise
        self._emit(purpose, candidate_id, model, time.monotonic() - t0, True, None, prompt, out,
                   main_skipped)
        return out

    def _breaker_open(self):
        return self.fallback_model is not None and self._main_fails[0] >= MAIN_FAIL_LIMIT

    def _note_main_result(self, ok):
        with self._main_lock:
            self._main_fails[0] = 0 if ok else self._main_fails[0] + 1

    def _emit(self, purpose, candidate_id, model, seconds, ok, err, prompt, out,
              main_skipped=False):
        usage = getattr(self._tls, "usage", None)
        self._tls.usage = None
        _emit_call(self, {
            "purpose": purpose, "candidate_id": candidate_id, "provider": self.name,
            "model": model, "seconds": round(seconds, 3), "ok": ok,
            "error": f"{type(err).__name__}: {err}"[:300] if err is not None else None,
            "chars_in": len(prompt), "chars_out": len(out) if out is not None else None,
            "tokens_in": usage[0] if usage else None,
            "tokens_out": usage[1] if usage else None,
            "main_skipped": main_skipped})

    def _complete(self, model, prompt):
        body = json.dumps({
            "systemInstruction": {"parts": [{"text": SYSTEM}]},
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"responseMimeType": "application/json", "temperature": 0}}).encode()
        raw = self.http(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                        data=body, headers={"x-goog-api-key": self.key, "content-type": "application/json"},
                        timeout=self.timeout, retries=self.retries, deadline=self.call_budget)
        try:
            data = json.loads(raw)
            parts = data["candidates"][0]["content"]["parts"]
            text = "".join(p.get("text", "") for p in parts)
        except (KeyError, IndexError, ValueError, TypeError):
            raise ResponseShapeError("unexpected Gemini response shape") from None
        u = data.get("usageMetadata") or {}
        self._tls.usage = (u.get("promptTokenCount"), u.get("candidatesTokenCount"))
        return text


class Mock:
    """テスト用。fn(prompt) -> str。"""
    name = "mock"
    model = "mock-1"
    recorder = None

    def __init__(self, fn):
        self.fn = fn

    def complete(self, prompt, purpose=None, candidate_id=None):
        return self.fn(prompt)


def get_provider(name, model=None, http=None, fallback_model=None):
    name = (name or "none").lower()
    if name == "none":
        return None
    if name == "anthropic":
        return Anthropic(model, http, fallback_model)
    if name == "gemini":
        return Gemini(model, http, fallback_model)
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
        llm.call_budget = st["call_budget_sec"]
    return llm
