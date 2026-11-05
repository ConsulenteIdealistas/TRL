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
# 行く (段別ブレーカー)。最終段は飛ばさない (飛ばすと評価が必ず失敗するため)。
# 当初は 3回 (2026-11-03 の実測: 終日不調の日は成功0件で、全コールが
# 「主モデルの失敗待ち→代替で再実行」の通行料を払ったため)。
# 2026-11-04 の再実測 (llm_calls、11/3 の6実行) で、最初の失敗より後の
# 試行は 25回中1回しか成功しない (4%) と判明した。不調は瞬断ではなく
# 時間帯で固まる性質 (regime) で、2回目以降の探りは通行料 (約84秒/回)
# ばかりが高い。泰崇の判断で 1回に切り下げる。切り捨てた段の分の評価は
# 次段が救済し、実モデルは来歴 (llm_model) に残るため観測は汚れない。
MAIN_FAIL_LIMIT = 1


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


class _CandidateBudget:
    """候補1件あたりの総時間予算 (extract + assess + 全段を通算) の仕組み。

    予算はワーカースレッドごとの締め切りとして _tls に持ち、http の
    deadline を残り時間でキャップする。予算切れは deadline_exceeded で
    即座に投げ、段を進めて倍化させない (監査 A-2 の候補単位版)。
    pipeline が候補の評価開始時に set_candidate_budget で設定する。"""

    def set_candidate_budget(self, seconds):
        self._tls.candidate_end = (time.monotonic() + seconds) if seconds else None

    def _check_candidate_budget(self):
        end = getattr(self._tls, "candidate_end", None)
        if end is not None and time.monotonic() >= end:
            raise HttpError("候補の時間予算を使い切りました", None, None,
                            deadline_exceeded=True)

    def _deadline(self):
        end = getattr(self._tls, "candidate_end", None)
        if end is None:
            return self.call_budget
        return min(self.call_budget, max(0.0, end - time.monotonic()))


class Gemini(_CandidateBudget):
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
            self._check_candidate_budget()
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
                        timeout=self.timeout, retries=self.retries, deadline=self._deadline(),
                        retry_429=False)
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


def get_provider(name, model=None, http=None, fallback_model=None, fallback_model_2=None):
    name = (name or "none").lower()
    if name == "none":
        return None
    if name == "gemini":
        return Gemini(model, http, fallback_model, fallback_model_2)
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
