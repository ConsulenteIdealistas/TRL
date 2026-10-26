"""HTTP取得(リトライ・秘密情報のマスク付き)。標準ライブラリのみ。"""

import os
import random
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from .errors import HttpError


SECRET_ENV = ("ANTHROPIC_API_KEY", "GEMINI_API_KEY", "NCBI_API_KEY", "EDINET_API_KEY", "GITHUB_TOKEN", "GH_TOKEN", "TOKEN")


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
    """attempt は 1 から。Retry-After があれば優先。無ければ指数バックオフ
    5秒→15秒→30秒→60秒(上限)に最大20%のジッター。503/429 のような一時的な
    過負荷は数秒では回復しないことが多いため、再試行間隔を段階的に延ばす。"""
    if retry_after is not None:
        return retry_after
    base = {1: 5, 2: 15, 3: 30}.get(attempt, 60)
    return base * (1 + 0.2 * rand())


def http_fetch(url, params=None, headers=None, data=None, timeout=30, retries=2,
               opener=None, sleep=time.sleep):
    opener = opener or urllib.request.urlopen
    full = url + ("?" + urllib.parse.urlencode(params) if params else "")
    hdrs = {"User-Agent": "trl-analysis/0.1"}
    hdrs.update(headers or {})
    attempt = 0

    while True:
        req = urllib.request.Request(full, data=data, headers=hdrs)
        retry_after = None
        try:
            with opener(req, timeout=timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            status = e.code
            retryable = e.code == 429 or e.code in (500, 502, 503, 504)
            msg = f"HTTP {e.code}"
            retry_after = _retry_after_seconds(getattr(e, "headers", None))
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            status, retryable = None, True
            msg = f"network error: {type(e).__name__}"

        attempt += 1
        if not retryable or attempt > retries:
            # URLのクエリ(APIキーが入り得る)はメッセージに含めない
            raise HttpError(redact(f"{msg} for {url}"), status, retry_after)

        sleep(backoff_delay(attempt, retry_after))
