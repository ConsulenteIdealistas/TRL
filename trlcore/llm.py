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

# 各段の連続失敗がこの回数に達したら、その実行ではその段を飛ばして次段へ
# 行く (段別ブレーカー)。根拠は実測 (2026-11-03): 主モデルが終日不調の日は
# 成功0件で、抽出・採点の全コールが「主モデルの失敗待ち→代替で再実行」の
# 通行料を払った。3回連続の失敗は瞬断では起きにくく、成功で即リセットする。
# 最終段は飛ばさない (飛ばすと評価が必ず失敗するため)
MAIN_FAIL_LIMIT = 3


def _fallback_model(model, fallback_model=None):
    """代替モデル名。未指定なら環境変数 TRL_LLM_FALLBACK_MODEL、主モデルと同じなら無効。"""
    fb = fallback_model or os.environ.get("TRL_LLM_FALLBACK_MODEL") or None
    return fb if fb and fb != model else None


def _fallback_models(model, fallback_model=None, fallback_model_2=None):
    """代替モデルの階段 (近い順、最大2段)。明示指定が無ければ環境変数
    TRL_LLM_FALLBACK_MODEL → TRL_LLM_FALLBACK_MODEL_2 の順で集める。
    主モデルと同名・重複は除く (同じ段を二度試す無駄と、ブレーカーの誤作動を防ぐ)。"""
    out = []
    for fb in (fallback_model or os.environ.get("TRL_LLM_FALLBACK_MODEL"),
               fallback_model_2 or os.environ.get("TRL_LLM_FALLBACK_MODEL_2")):
        if fb and fb != model and fb not in out:
            out.append(fb)
    return out


class Anthropic:
    name = "anthropic"

    def __init__(self, model=None, http=None, fallback_model=None, fallback_model_2=None):
        self.model = model or os.environ.get("TRL_LLM_MODEL") or "claude-haiku-5-5"
        self.fallback_models = _fallback_models(self.model, fallback_model, fallback_model_2)
        self.http = http or http_fetch
        self.timeout, self.retries = 120, 2  # configure_llm() で scoring.json の llm 設定に上書き
        self.call_budget = 240  # 同上 (1回の呼び出し全体の時間上限・監査 A-2)
        self.key = os.environ.get("ANTHROPIC_API_KEY")
        if not self.key:
            raise LLMError("ANTHROPIC_API_KEY is not set")
        self._tls = threading.local()  # last_model はスレッドごとに保持する (評価は並列実行)
        self.recorder = None  # 計装レコーダ (run 側が設定する。None なら記録しない)
        self._stage_fails = [0, 0, 0]  # 段ごとの連続失敗数 (段別ブレーカー用)
        self._stage_lock = threading.Lock()

    @property
    def fallback_model(self):
        """互換用: 代替モデルの1段目 (無ければ None)。階段全体は fallback_models。"""
        return self.fallback_models[0] if self.fallback_models else None

    @property
    def last_model(self):
        """直近の complete() が実際に応答したモデル名 (フォールバック時は代替モデル)。
        評価の来歴と抽出キャッシュのキーに使う。未呼び出しなら None。"""
        return getattr(self._tls, "model", None)

    def complete(self, prompt, purpose=None, candidate_id=None):
        # 階段: 主モデル → 代替1 → 代替2。一時的失敗 (503/429 等) のときだけ
        # 次段へ降りる。恒常的な失敗 (404/401 等) と時間上限の到達はその場で
        # 投げる (従来どおり)。連続失敗中の段はブレーカーで飛ばす
        stages = [self.model] + self.fallback_models
        main_skipped = self._stage_open(0)
        last_err = None
        for i, stage in enumerate(stages):
            if i < len(stages) - 1 and self._stage_open(i):
                continue  # この段は連続失敗中: 失敗待ち (通行料) を払わず次段へ
            try:
                out = self._timed(stage, prompt, purpose, candidate_id,
                                  main_skipped=main_skipped and i > 0)
            except HttpError as e:
                self._note_stage_result(i, False)
                last_err = e
                if not e.transient or e.deadline_exceeded or i == len(stages) - 1:
                    raise
                continue
            self._note_stage_result(i, True)
            self._tls.model = stage
            return out
        raise last_err if last_err is not None else LLMError("no LLM stage available")

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

    def _stage_open(self, i):
        return i < len(self._stage_fails) and self._stage_fails[i] >= MAIN_FAIL_LIMIT

    def _note_stage_result(self, i, ok):
        with self._stage_lock:
            if i < len(self._stage_fails):
                self._stage_fails[i] = 0 if ok else self._stage_fails[i] + 1

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

    def __init__(self, model=None, http=None, fallback_model=None, fallback_model_2=None):
        self.model = model or os.environ.get("TRL_LLM_MODEL") or "gemini-3.8-flash"
        self.fallback_models = _fallback_models(self.model, fallback_model, fallback_model_2)
        self.http = http or http_fetch
        self.timeout, self.retries = 120, 2  # configure_llm() で scoring.json の llm 設定に上書き
        self.call_budget = 240  # 同上 (1回の呼び出し全体の時間上限・監査 A-2)
        self.key = os.environ.get("GEMINI_API_KEY")
        if not self.key:
            raise LLMError("GEMINI_API_KEY is not set")
        self._tls = threading.local()  # last_model はスレッドごとに保持する (評価は並列実行)
        self.recorder = None  # 計装レコーダ (run 側が設定する。None なら記録しない)
        self._stage_fails = [0, 0, 0]  # 段ごとの連続失敗数 (段別ブレーカー用)
        self._stage_lock = threading.Lock()

    @property
    def fallback_model(self):
        """互換用: 代替モデルの1段目 (無ければ None)。階段全体は fallback_models。"""
        return self.fallback_models[0] if self.fallback_models else None

    @property
    def last_model(self):
        """直近の complete() が実際に応答したモデル名 (フォールバック時は代替モデル)。
        評価の来歴と抽出キャッシュのキーに使う。未呼び出しなら None。"""
        return getattr(self._tls, "model", None)

    def complete(self, prompt, purpose=None, candidate_id=None):
        # 階段: 主モデル → 代替1 → 代替2。一時的失敗 (503/429 等) のときだけ
        # 次段へ降りる。恒常的な失敗 (404/401 等) と時間上限の到達はその場で
        # 投げる (従来どおり)。連続失敗中の段はブレーカーで飛ばす
        stages = [self.model] + self.fallback_models
        main_skipped = self._stage_open(0)
        last_err = None
        for i, stage in enumerate(stages):
            if i < len(stages) - 1 and self._stage_open(i):
                continue  # この段は連続失敗中: 失敗待ち (通行料) を払わず次段へ
            try:
                out = self._timed(stage, prompt, purpose, candidate_id,
                                  main_skipped=main_skipped and i > 0)
            except HttpError as e:
                self._note_stage_result(i, False)
                last_err = e
                if not e.transient or e.deadline_exceeded or i == len(stages) - 1:
                    raise
                continue
            self._note_stage_result(i, True)
            self._tls.model = stage
            return out
        raise last_err if last_err is not None else LLMError("no LLM stage available")

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

    def _stage_open(self, i):
        return i < len(self._stage_fails) and self._stage_fails[i] >= MAIN_FAIL_LIMIT

    def _note_stage_result(self, i, ok):
        with self._stage_lock:
            if i < len(self._stage_fails):
                self._stage_fails[i] = 0 if ok else self._stage_fails[i] + 1

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


# Cloudflare Workers AI の既定モデル。根拠 (2026-11-03 の無料枠調査):
# 10,000 ニューロン/日の無料枠で、25K入力+1K出力の1コールが約254ニューロン
# (1日約39コール分) と予算内に収まり、日本語が無料モデルの中で最強の部類で
# 262K級コンテキスト。モデル名は環境変数 TRL_LLM_CF_MODEL で差し替え可能
CF_DEFAULT_MODEL = "@cf/google/gemma-4-26b-a4b-it"


class OpenAICompatible:
    """OpenAI 互換の chat/completions を話すプロバイダ。第2プロバイダ
    (Cloudflare Workers AI) 用で、base_url・キー・モデルを変えれば
    Mistral / OpenRouter 等も同じ形で足せる。モデル内の階段は持たない
    単一段だが、計装と last_model の形は Gemini / Anthropic と揃える。"""

    def __init__(self, name, base_url, key, model, http=None):
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.key = key
        self.model = model
        self.fallback_models = []  # 段の互換用 (単一段なので常に空)
        self.http = http or http_fetch
        self.timeout, self.retries = 120, 2  # configure_llm() で scoring.json の llm 設定に上書き
        self.call_budget = 240  # 同上
        self._tls = threading.local()
        self.recorder = None

    @property
    def fallback_model(self):
        return None

    @property
    def last_model(self):
        return getattr(self._tls, "model", None)

    def complete(self, prompt, purpose=None, candidate_id=None):
        out = self._timed(self.model, prompt, purpose, candidate_id)
        self._tls.model = self.model
        return out

    def _timed(self, model, prompt, purpose, candidate_id, main_skipped=False):
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
        # response_format は送らない: 対応がモデル依存で、未対応モデルでは
        # エラーになり得る。JSON の強制はプロンプト側 + extract_json の
        # 寛容な抽出で担保する (Gemini の responseMimeType との違い)
        body = json.dumps({
            "model": model, "temperature": 0,
            "messages": [{"role": "system", "content": SYSTEM},
                         {"role": "user", "content": prompt}]}).encode()
        raw = self.http(f"{self.base_url}/chat/completions", data=body, headers={
            "authorization": f"Bearer {self.key}", "content-type": "application/json"},
            timeout=self.timeout, retries=self.retries, deadline=self.call_budget)
        try:
            data = json.loads(raw)
            text = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, ValueError, TypeError):
            raise ResponseShapeError(f"unexpected {self.name} response shape") from None
        u = data.get("usage") or {}
        self._tls.usage = (u.get("prompt_tokens"), u.get("completion_tokens"))
        return text


class ProviderChain:
    """プロバイダの階段。第1プロバイダ (内部にモデル階段を持つ) が一時的失敗で
    全段尽きたら第2プロバイダへ降りる。恒常的な失敗と時間上限の到達は、段の
    階段と同じくその場で投げる (設定ミスを別プロバイダで隠さない・締め切りを
    倍化させない)。recorder と timeout 等の設定は全プロバイダへ行き渡らせる。"""

    def __init__(self, providers):
        self.providers = list(providers)
        self._tls = threading.local()

    @property
    def name(self):
        return "+".join(p.name for p in self.providers)

    @property
    def model(self):
        return self.providers[0].model

    @property
    def fallback_models(self):
        return getattr(self.providers[0], "fallback_models", [])

    @property
    def fallback_model(self):
        return getattr(self.providers[0], "fallback_model", None)

    @property
    def last_model(self):
        """直近の complete() に実際に応答したモデル名 (応答したプロバイダ側の記録)。"""
        return getattr(self._tls, "model", None)

    @property
    def recorder(self):
        return getattr(self.providers[0], "recorder", None)

    @recorder.setter
    def recorder(self, fn):
        for p in self.providers:
            p.recorder = fn

    # configure_llm() は属性代入で設定するため、プロパティで全段へ行き渡らせる
    @property
    def timeout(self):
        return self.providers[0].timeout

    @timeout.setter
    def timeout(self, v):
        for p in self.providers:
            if hasattr(p, "timeout"):
                p.timeout = v

    @property
    def retries(self):
        return self.providers[0].retries

    @retries.setter
    def retries(self, v):
        for p in self.providers:
            if hasattr(p, "retries"):
                p.retries = v

    @property
    def call_budget(self):
        return self.providers[0].call_budget

    @call_budget.setter
    def call_budget(self, v):
        for p in self.providers:
            if hasattr(p, "call_budget"):
                p.call_budget = v

    def complete(self, prompt, purpose=None, candidate_id=None):
        last_err = None
        for p in self.providers:
            try:
                out = p.complete(prompt, purpose=purpose, candidate_id=candidate_id)
            except HttpError as e:
                last_err = e
                if not e.transient or e.deadline_exceeded:
                    raise
                continue
            self._tls.model = getattr(p, "last_model", None) or getattr(p, "model", None)
            return out
        raise last_err if last_err is not None else LLMError("no LLM provider available")


def _cloudflare(http=None):
    """Cloudflare Workers AI のプロバイダ。資格情報 (CLOUDFLARE_ACCOUNT_ID /
    CLOUDFLARE_API_TOKEN) が両方そろっているときだけ作り、片方でも欠ければ
    None (第2プロバイダ無しで従来どおり動く。片方だけの設定ミスも実行は
    止めず、不在は次回実行の llm_calls に Cloudflare の記録が無いことで
    気づける形にする)。"""
    account = os.environ.get("CLOUDFLARE_ACCOUNT_ID") or None
    token = os.environ.get("CLOUDFLARE_API_TOKEN") or None
    if not account or not token:
        return None
    model = os.environ.get("TRL_LLM_CF_MODEL") or CF_DEFAULT_MODEL
    return OpenAICompatible(
        "cloudflare",
        f"https://api.cloudflare.com/client/v4/accounts/{account}/ai/v1",
        token, model, http=http)


class Mock:
    """テスト用。fn(prompt) -> str。"""
    name = "mock"
    model = "mock-1"
    recorder = None

    def __init__(self, fn):
        self.fn = fn

    def complete(self, prompt, purpose=None, candidate_id=None):
        return self.fn(prompt)


def get_provider(name, model=None, http=None, fallback_model=None, fallback_model_2=None):
    name = (name or "none").lower()
    if name == "none":
        return None
    if name == "cloudflare":
        cf = _cloudflare(http)
        if cf is None:
            raise LLMError("CLOUDFLARE_ACCOUNT_ID / CLOUDFLARE_API_TOKEN is not set")
        return cf
    if name == "anthropic":
        primary = Anthropic(model, http, fallback_model, fallback_model_2)
    elif name == "gemini":
        primary = Gemini(model, http, fallback_model, fallback_model_2)
    else:
        raise LLMError(f"unknown provider: {name}")
    # 第2プロバイダ: Cloudflare の資格情報がそろっていれば階段の最終段に足す。
    # 未設定なら第1プロバイダ単独で従来どおり (段階的に有効化できる)
    cf = _cloudflare(http)
    if cf is not None:
        return ProviderChain([primary, cf])
    return primary


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
