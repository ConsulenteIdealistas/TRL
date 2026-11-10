"""HTTP取得(リトライ・秘密情報のマスク付き)。標準ライブラリのみ。"""

import os
import random
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from .errors import HttpError


SECRET_ENV = ("ANTHROPIC_API_KEY", "GEMINI_API_KEY", "CLOUDFLARE_API_TOKEN", "NCBI_API_KEY", "EDINET_API_KEY", "OPENALEX_API_KEY", "DEEPL_API_KEY", "AZURE_TRANSLATOR_KEY", "GOOGLE_TRANSLATE_API_KEY", "GITHUB_TOKEN", "GH_TOKEN", "TOKEN", "ROIC_API_KEY", "SIMFIN_API_KEY")


_PARAM_RE = re.compile(r"(?i)(api[_-]?key|key|token)=([^&\s]+)")


def redact(text):
    text = str(text)
    for name in SECRET_ENV:
        v = os.environ.get(name)
        if v and len(v) >= 6:
            text = text.replace(v, "***")
    return _PARAM_RE.sub(lambda m: m.group(1) + "=***", text)


def _retry_after_seconds(headers, cap=60.0):
    """Retry-After(秒指定のみ)を読む。日付形式や不正値は None。上限 cap 秒。"""
    try:
        v = float((headers or {}).get("Retry-After"))
    except (TypeError, ValueError, AttributeError):
        return None
    return min(max(v, 0.0), cap) if v == v else None


def backoff_delay(attempt, retry_after=None, rand=random.random):
    """attempt は 1 から。Retry-After があれば優先。無ければ 15→30→30秒
    (最大20%のジッターつき)、4回目以降は60秒。503/429 のような一時的な
    過負荷・レート制限は数秒では回復しないため、初回から少し長めに置く
    (旧仕様の 5→15→30 より一段長く、全面30秒固定よりは他ソースへの影響が小さい)。"""
    if retry_after is not None:
        return retry_after
    base = {1: 15, 2: 30, 3: 30}.get(attempt, 60)
    return base * (1 + 0.2 * rand())


MAX_RESPONSE_BYTES = 32 * 1024 * 1024  # 異常な巨大応答でメモリを食い潰さないための上限


def http_fetch(url, params=None, headers=None, data=None, timeout=30, retries=2,
               opener=None, sleep=time.sleep, deadline=None, retry_429=True):
    """deadline は呼び出し開始からの秒数 (任意)。次の再試行待ちが上限を超える
    場合は再試行せず、その失敗を deadline_exceeded つきで送出する。実行中の
    リクエスト自体は timeout が上限になる (監査 A-2: 1回の呼び出しが呼び出し側の
    フェーズ予算を際限なく食わないようにする)。
    retry_429=False のとき 429 は再試行せず即座に送出する (LLM 経路用:
    枯れた枠への聞き直しは時間と枠の両方を無駄にするため、段の切替で対処する)。"""
    opener = opener or urllib.request.urlopen
    full = url + ("?" + urllib.parse.urlencode(params) if params else "")
    hdrs = {"User-Agent": "trl-analysis/0.1"}
    hdrs.update(headers or {})
    attempt = 0
    t0 = time.monotonic()

    while True:
        req = urllib.request.Request(full, data=data, headers=hdrs)
        retry_after = None
        try:
            with opener(req, timeout=timeout) as resp:
                payload = resp.read(MAX_RESPONSE_BYTES + 1)
                if len(payload) > MAX_RESPONSE_BYTES:
                    raise HttpError(f"response too large for {url}", None, None)
                return payload
        except urllib.error.HTTPError as e:
            status = e.code
            retryable = e.code in (500, 502, 503, 504) or (retry_429 and e.code == 429)
            msg = f"HTTP {e.code}"
            retry_after = _retry_after_seconds(getattr(e, "headers", None))
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            status, retryable = None, True
            msg = f"network error: {type(e).__name__}"

        attempt += 1
        if not retryable or attempt > retries:
            # URLのクエリ(APIキーが入り得る)はメッセージに含めない
            raise HttpError(redact(f"{msg} for {url}"), status, retry_after)

        delay = backoff_delay(attempt, retry_after)
        if deadline is not None and (time.monotonic() - t0) + delay > deadline:
            raise HttpError(redact(f"{msg} for {url} (call deadline {deadline}s exceeded)"),
                            status, retry_after, deadline_exceeded=True)
        sleep(delay)
