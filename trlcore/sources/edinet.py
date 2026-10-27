"""EDINET API v2 (金融庁)。上場企業の開示書類メタデータと財務数値を取得する。

クエリ文字列は証券コード (例: "9348"、"186A")。EDINET の secCode は末尾に 0 を
付けた5桁 (例: "93480") なので変換して照合する。書類一覧APIは日単位のため、
lookback_days 分の日次リストを取得し、プロセス内キャッシュで複数クエリから再利用する。

EDINET_API_KEY 未設定なら何も取得しない (空リストを返す)。財務書類 (有価証券報告書・
四半期報告書・半期報告書とその訂正) については、直近のものから XBRL_PER_QUERY 件まで
書類取得APIで財務数値を抽出し (type=5 の CSV 形式を優先し、取れなければ type=1 の
XBRL 本体を読む)、根拠テキストに "財務数値(EDINET XBRL)" として付ける。この数値付き
開示が、評価時の「関連財務根拠」プール (Store.related_financials) の材料になる。
数値抽出に失敗した書類はメタデータのみの従来動作にフォールバックする。
"""

import csv
import io
import json
import os
import time
import zipfile
import xml.etree.ElementTree as ET
from datetime import timedelta
from ..http import http_fetch


BASE = "https://api.edinet-fsa.go.jp/api/v2/documents.json"
DOC_URL = "https://api.edinet-fsa.go.jp/api/v2/documents/{doc_id}"
PDF_URL = "https://disclosure2dl.edinet-fsa.go.jp/searchdocument/pdf/{doc_id}.pdf"

FINANCIAL_DOC_TYPES = {"120", "130", "140", "150", "160", "170"}
XBRL_PER_QUERY = 2
FINANCIAL_MARKER = "財務数値(EDINET XBRL)"

_day_cache = {}
_doc_cache = {}

# (表示名, 要素名候補 (要約サフィックスを除いたローカル名), 期間項目か, 種別)
_FIN_ITEMS = [
    ("売上高", ("NetSales", "Revenue", "TotalRevenue", "OperatingRevenue", "OperatingRevenues"), True, "money"),
    ("営業利益", ("OperatingIncomeLoss",), True, "money"),
    ("経常利益", ("OrdinaryIncomeLoss",), True, "money"),
    ("純利益", ("ProfitLossAttributableToOwnersOfParent", "NetIncomeLoss"), True, "money"),
    ("営業キャッシュフロー", ("NetCashProvidedByUsedInOperatingActivities",), True, "money"),
    ("現金及び現金同等物", ("CashAndCashEquivalents",), False, "money"),
    ("純資産", ("NetAssets",), False, "money"),
    ("総資産", ("TotalAssets",), False, "money"),
    ("自己資本比率", ("EquityToAssetRatio",), False, "ratio"),
]


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


# ---------------------------------------------------------------- XBRL / CSV 抽出

def _base_name(el):
    if el.endswith("SummaryOfBusinessResults"):
        el = el[: -len("SummaryOfBusinessResults")]
    return el


def _num(s):
    """XBRL/CSV の数値文字列を float にする。△・括弧はマイナス、ダッシュ類は None。"""
    if s is None:
        return None
    s = str(s).strip().replace(",", "").replace("，", "").replace(" ", "")
    if s in ("", "-", "－", "―", "—", "–"):
        return None
    neg = False
    if s[:1] in ("△", "▲"):
        neg, s = True, s[1:]
    if s.startswith("(") and s.endswith(")"):
        neg, s = True, s[1:-1]
    try:
        v = float(s)
    except ValueError:
        return None
    return -v if neg else v


def parse_facts_csv(data):
    """EDINET の CSV 形式 XBRL (UTF-16・タブ区切り・全項目クォート) を fact 化する。

    fact: {"el", "year"(当期/前期/その他), "consol"(連結か), "duration"(期間項目か), "value"}
    """
    if isinstance(data, (bytes, bytearray)):
        text = None
        for enc in ("utf-16", "utf-8-sig", "shift_jis"):
            try:
                text = bytes(data).decode(enc)
                break
            except (UnicodeDecodeError, ValueError):
                continue
        if text is None:
            return []
    else:
        text = str(data)
    rows = list(csv.reader(io.StringIO(text), delimiter="\t"))
    if not rows:
        return []
    header = [h.strip() for h in rows[0]]
    try:
        i_el = header.index("要素ID")
        i_year = header.index("相対年度")
        i_cons = header.index("連結・個別")
        i_dur = header.index("期間・時点")
        i_val = header.index("値")
    except ValueError:
        return []
    i_cur = header.index("通貨") if "通貨" in header else -1
    need = max(i_el, i_year, i_cons, i_dur, i_val, i_cur)
    facts = []
    for r in rows[1:]:
        if len(r) <= need:
            continue
        el = _base_name(r[i_el].split(":")[-1].strip())
        v = _num(r[i_val])
        if v is None:
            continue
        if i_cur >= 0 and r[i_cur].strip() and r[i_cur].strip() != "JPY" and el != "EquityToAssetRatio":
            continue  # 外貨建ての金額は混ぜない (比率は通貨なし)
        facts.append({"el": el, "year": r[i_year].strip(), "consol": "連結" in r[i_cons],
                      "duration": r[i_dur].strip() == "期間", "value": v})
    return facts


def _local(tag):
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def parse_facts_xbrl(data):
    """XBRL インスタンス文書 (XML) を fact 化する。CSV が無い場合のフォールバック。"""
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return []
    ctx = {}
    for c in root.iter():
        if _local(c.tag) != "context":
            continue
        cid = c.get("id") or ""
        nonconsol = any("NonConsolidatedMember" in (ch.text or "")
                        for ch in c.iter() if _local(ch.tag) == "explicitMember")
        year = "当期" if cid.startswith("CurrentYear") else "前期" if cid.startswith("PriorYear") else None
        ctx[cid] = {"year": year, "consol": not nonconsol, "duration": "Duration" in cid}
    facts = []
    for el in root.iter():
        ref = el.get("contextRef")
        if not ref or ref not in ctx or el.get("{http://www.w3.org/2001/XMLSchema-instance}nil") == "true":
            continue
        v = _num(el.text)
        if v is None:
            continue
        base = _base_name(_local(el.tag))
        unit = el.get("unitRef") or ""
        if unit and unit != "JPY" and base != "EquityToAssetRatio":
            continue
        info = ctx[ref]
        facts.append({"el": base, "year": info["year"], "consol": info["consol"],
                      "duration": info["duration"], "value": v})
    return facts


def _el_match(el, cand):
    if el == cand:
        return True
    if cand == "CashAndCashEquivalents" and el.startswith("CashAndCashEquiv"):
        return True  # 表記ゆれを吸収
    return False


def select_financials(facts):
    """fact 群から主要項目の当期・前期値を選ぶ。連結を優先する。"""
    sel = {}
    for label, cands, is_dur, _kind in _FIN_ITEMS:
        per_year = {}
        for year in ("当期", "前期"):
            best = None
            for f in facts:
                if f["year"] != year or f["duration"] != is_dur:
                    continue
                if not any(_el_match(f["el"], c) for c in cands):
                    continue
                score = 2 if f["consol"] else 0
                if best is None or score > best[0]:
                    best = (score, f["value"])
            if best is not None:
                per_year[year] = best[1]
        if per_year:
            sel[label] = per_year
    return sel


def _fmt_money(v):
    m = v / 1_000_000
    return ("△" if m < 0 else "") + f"{abs(m):,.0f}百万円"


def format_financials(sel):
    """選択済み財務値を根拠テキスト化する。2項目未満なら空 (抽出失敗扱い)。"""
    parts = []
    for label, _cands, _dur, kind in _FIN_ITEMS:
        d = sel.get(label) or {}
        cur, prev = d.get("当期"), d.get("前期")
        if cur is None:
            continue
        if kind == "ratio":
            t = f"{label} {cur * 100:.1f}%"
            if prev is not None:
                t += f"(前期 {prev * 100:.1f}%)"
        else:
            t = f"{label} {_fmt_money(cur)}"
            if prev is not None:
                t += f"(前期 {_fmt_money(prev)})"
        parts.append(t)
    if len(parts) < 2:
        return ""
    return "、".join(parts) + "。"


def _facts_from_zip(data):
    facts = []
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        names = zf.namelist()
        for name in names:
            if name.lower().endswith(".csv"):
                facts.extend(parse_facts_csv(zf.read(name)))
        if not facts:
            for name in names:
                if name.lower().endswith(".xbrl") and "PublicDoc" in name:
                    facts.extend(parse_facts_xbrl(zf.read(name)))
    return facts


def fetch_financial_text(doc_id, http, key):
    """書類取得APIから財務数値テキストを得る。type=5 (CSV形式) → type=1 (XBRL) の順に試す。"""
    if doc_id in _doc_cache:
        return _doc_cache[doc_id]
    text = ""
    for typ in (5, 1):
        try:
            raw = http(DOC_URL.format(doc_id=doc_id), params={"type": typ},
                       headers={"Ocp-Apim-Subscription-Key": key})
        except Exception:
            continue
        if not isinstance(raw, (bytes, bytearray, str)) or not raw:
            continue
        try:
            if isinstance(raw, (bytes, bytearray)) and bytes(raw)[:2] == b"PK":
                facts = _facts_from_zip(bytes(raw))
            else:
                facts = parse_facts_csv(raw)
            text = format_financials(select_financials(facts))
        except Exception:
            text = ""
        if text:
            break
    _doc_cache[doc_id] = text
    return text


# ---------------------------------------------------------------- 取得

def _day_results(day, http, key, sleep):
    if day in _day_cache:
        return _day_cache[day]
    raw = http(BASE, params={"date": day.isoformat(), "type": 2},
               headers={"Ocp-Apim-Subscription-Key": key})
    results = json.loads(raw).get("results") or []
    _day_cache[day] = results
    sleep(0.2)  # 日次リストは再利用される。取得時だけ間隔を空ける
    return results


def _enrich_financials(out, days, ticker, http, key):
    """直近の財務書類 (XBRL_PER_QUERY 件まで) の abstract に財務数値を付ける。"""
    sec = sec_code(ticker)
    by_id = {r["source_id"]: r for r in out}
    fins, seen = [], set()
    for day in days:
        for r in _day_cache.get(day) or []:
            doc_id = r.get("docID")
            if (r.get("secCode") != sec or r.get("docTypeCode") not in FINANCIAL_DOC_TYPES
                    or r.get("xbrlFlag") != "1" or r.get("withdrawalStatus") == "1"
                    or doc_id not in by_id or doc_id in seen):
                continue
            seen.add(doc_id)
            fins.append(r)
    fins.sort(key=lambda r: r.get("submitDateTime") or "", reverse=True)
    for r in fins[:XBRL_PER_QUERY]:
        text = fetch_financial_text(r["docID"], http, key)
        if text:
            rec = by_id[r["docID"]]
            rec["abstract"] = rec["abstract"] + f" {FINANCIAL_MARKER}: {text}"


def edinet_fetch(query, max_results, lookback_days, as_of, http=None, sleep=time.sleep):
    key = os.environ.get("EDINET_API_KEY")
    if not key:
        return []
    http = http or http_fetch
    out, days = [], []
    for offset in range(lookback_days + 1):
        day = as_of - timedelta(days=offset)
        days.append(day)
        out.extend(parse_results(_day_results(day, http, key, sleep), query))
    out.sort(key=lambda r: r["published"] or "", reverse=True)
    try:
        _enrich_financials(out, days, query, http, key)
    except Exception:
        pass  # 数値抽出の失敗でメタデータ取得まで落とさない
    return out[:max_results]
