"""EDINET API v2 (金融庁)。上場企業の開示書類メタデータを取得する。

クエリ文字列は証券コード (例: "9348"、"186A")。EDINET の secCode は末尾に 0 を
付けた5桁 (例: "93480") なので変換して照合する。書類一覧APIは日単位のため、
lookback_days 分の日次リストを取得し、プロセス内キャッシュで複数クエリから再利用する。

EDINET_API_KEY 未設定なら何も取得しない (空リストを返す)。v1 はメタデータ
(提出者・書類名・提出日時・対象期間) を根拠テキスト化するもので、XBRL の
財務数値抽出は行わない。
"""

import json
import os
import time
from datetime import timedelta
from ..http import http_fetch


BASE = "https://api.edinet-fsa.go.jp/api/v2/documents.json"
PDF_URL = "https://disclosure2dl.edinet-fsa.go.jp/searchdocument/pdf/{doc_id}.pdf"

_day_cache = {}


def sec_code(ticker):
    t = str(ticker).strip().upper()
    return t + "0" if len(t) == 4 else t


def parse_results(results, ticker):
    sec = sec_code(ticker)
    out = []
    for r in results or []:
        if r.get("secCode") != sec or r.get("withdrawalStatus") == "1":
            continue
        doc_id = r.get("docID")
        filer = (r.get("filerName") or "").strip()
        desc = (r.get("docDescription") or "").strip()
        if not doc_id or not filer or not desc:
            continue
        submitted = (r.get("submitDateTime") or "")[:10] or None
        period = ""
        if r.get("periodStart") and r.get("periodEnd"):
            period = f"対象期間: {r['periodStart']}〜{r['periodEnd']}。"
        abstract = (f"{filer}がEDINETに{desc}を提出した。"
                    f"提出日時: {r.get('submitDateTime') or '不明'}。{period}"
                    f"証券コード: {r.get('secCode')}、提出者EDINETコード: {r.get('edinetCode') or '不明'}。")
        out.append({
            "source": "edinet", "source_id": doc_id,
            "title": f"{filer} {desc}",
            "abstract": abstract,
            "url": PDF_URL.format(doc_id=doc_id),
            "published": submitted,
            "fields": ["EDINET", desc],
            "affiliations": [filer],
            "info_type": "disclosure",
        })
    return out


def _day_results(day, http, key, sleep):
    if day in _day_cache:
        return _day_cache[day]
    raw = http(BASE, params={"date": day.isoformat(), "type": 2},
               headers={"Ocp-Apim-Subscription-Key": key})
    results = json.loads(raw).get("results") or []
    _day_cache[day] = results
    sleep(0.2)  # 日次リストは再利用される。取得時だけ間隔を空ける
    return results


def edinet_fetch(query, max_results, lookback_days, as_of, http=None, sleep=time.sleep):
    key = os.environ.get("EDINET_API_KEY")
    if not key:
        return []
    http = http or http_fetch
    out = []
    for offset in range(lookback_days + 1):
        day = as_of - timedelta(days=offset)
        out.extend(parse_results(_day_results(day, http, key, sleep), query))
    out.sort(key=lambda r: r["published"] or "", reverse=True)
    return out[:max_results]
