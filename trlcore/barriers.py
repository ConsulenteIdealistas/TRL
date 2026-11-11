"""関門導出レイヤー (barriers/v1)。

最新 long 評価について、イノベーションの3関門 (Devil River / Valley of
Death / Darwinian Sea) への該当を、一次情報の証拠条件から決定的に導く。
TRL の位置だけで関門を割り当てる方式は採らない。設計の正本は
~/workspace/trl-phases/DESIGN-V2.md。

位置づけは tiers と同じ表示専用の導出レイヤー: 導出は export 時に DB
から行い DB には保存しない。評価・選定・tier 導出・監査には一切使わない。
stage (人間が付けた企業分類) は一次情報ではないため入力に使わない。

共通の約束 (DESIGN-V2 §4):
- 各関門は「該当 (hit) / 該当なし (clear) / 判定しない (unknown)」の
  3値を返し、ゲート (TRL 条件) の外は "na" (対象外) とする。材料不足は
  必ず unknown で、clear に倒さない
- TRL はゲート (文脈の絞り込み) にだけ使い、関門は必ず一次情報の証拠
  条件が主条件になる。同じ TRL でも証拠が違えば結果が違う
- 該当のときは、判断に使った証拠 (数値・状態と出典材料) を導出側が
  文字列で生成する (表示側で数値を発明しない)
- 関門の定義は NEDO「NEDO によるプロジェクトマネジメントの進化に
  向けて」による (criteria.html に出典つきで掲載)

規則 (barriers/v1、決め事 V2-1〜V2-7 は推奨どおりで確定):
- Devil River (技術単位): ゲート=代表 TRL ≤ 3 (V2-1)。証拠条件=その
  評価の根拠 claim が参照する治験 (NCT 番号で引く) のうち、Phase 1 以上
  かつ開始済み (RECRUITING / ACTIVE_NOT_RECRUITING / COMPLETED /
  ENROLLING_BY_INVITATION) のものが1件も無いこと。大企業の別製品の
  治験では解除されない (技術単位)
- Valley of Death (企業単位): ゲート=代表 TRL ≥ 4。証拠条件=規程
  (EVALUATION_CODE 第12条5) の定義で計算したランウェイ < 1年 (V2-2。
  1年は継続企業評価の地平であり、規程自身が財務の持続性を1年の地平で
  見る定義と一致する)。売上のある企業も対象に含める (V2-3)。黒字で
  burn が無い場合は clear、財務が抜けなければ unknown
- Darwinian Sea (企業単位): ゲート=代表 TRL ≥ 7 (泰崇の追加判断で
  当初の ≥8 から変更。ダーウィンの海は TRL7-8 でも起こりうるためで、
  判別の本体は売上の条件が担う)。証拠条件=通期売上が前年度より減少。
  比較できる2年度分の通期売上が無い場合は unknown (V2-4: 規則だけ置き、
  年度データの蓄積で自動発火させる)
- 関門フィルタは置かない (V2-5)。表示スイッチは廃止し常時表示 (V2-6)。
  stage は入力に使わない (V2-7)
"""

import json
import re
from datetime import date

BARRIER_RULES_VERSION = "barriers/v1"

BARRIER_ORDER = ("devil_river", "valley_of_death", "darwinian_sea")
BARRIER_LABELS = {"devil_river": "Devil River",
                  "valley_of_death": "Valley of Death",
                  "darwinian_sea": "Darwinian Sea"}

# NEDO の定義に基づく関門の説明 (表示側の転記と criteria.html が正)
BARRIER_DEFS = {
    "devil_river": "「魔の川」と呼ばれる研究開発段階の不確実性（NEDO）。研究から開発へ進めるかどうかの関門。",
    "valley_of_death": "「死の谷」と呼ばれる、研究開発後の事業化に要する調達、生産、流通の体制構築や資金調達の困難（NEDO）。本規則で機械的に観測するのは資金調達の困難の側のみ。",
    "darwinian_sea": "「ダーウィンの海」と呼ばれる市場獲得における生存競争の困難（NEDO）。",
}

# 開始済みとみなす治験ステータス (DESIGN-V2 §4-1 の列挙どおり)
STARTED_TRIAL_STATUSES = frozenset({
    "RECRUITING", "ACTIVE_NOT_RECRUITING", "COMPLETED",
    "ENROLLING_BY_INVITATION"})

# Valley の閾値 (年)。継続企業評価の地平であり新規の数字ではない (V2-2)
RUNWAY_THRESHOLD_YEARS = 1.0

# Darwinian Sea のゲート (泰崇の追加判断で ≥8 から ≥7 に変更)
SEA_MIN_TRL = 7

# 財務スナップショットの出典優先度 (規程第6条の証拠の権威階層:
# 規制当局・公的登録簿 > 企業の公式開示・財務数値 > 集約サービス)。
# 同一期間・同等の完全性なら上位の出典を採るための順序づけにだけ使う
_SOURCE_PRIORITY = {"edinet": 0, "edgar": 0, "esef": 0, "roic": 1,
                    "simfin": 2, "alphavantage": 3, "yahoo": 4,
                    "eqs": 5, "gnw": 5, "swiss_eqs": 5, "swiss_gnw": 5}

_NCT_RE = re.compile(r"NCT\d{8}")
_STATUS_RE = re.compile(r"Status:\s*([A-Z_]+)")
_PHASE_RE = re.compile(r"Phase:\s*([A-Z0-9_,/ ]+)")


# ---------------------------------------------------------------- 治験

def parse_trial_status(text):
    """治験候補の abstract 末尾の固定書式 (Status: / Phase:) を抜く。

    戻り値: {"status": str|None, "max_phase": int|None}。Phase が "NA" 等で
    数値を持たない場合は max_phase=None (Phase 1 以上とは言えない)。
    """
    text = text or ""
    m = _STATUS_RE.search(text)
    status = m.group(1) if m else None
    max_phase = None
    pm = _PHASE_RE.search(text)
    if pm:
        nums = [int(n) for n in re.findall(r"PHASE\s*(\d)", pm.group(1))]
        if nums:
            max_phase = max(nums)
    return {"status": status, "max_phase": max_phase}


def trial_started_phase1(trial):
    """開始済みかつ Phase 1 以上の治験か (Devil River の証拠条件)。"""
    if not trial:
        return False
    return (trial.get("status") in STARTED_TRIAL_STATUSES
            and isinstance(trial.get("max_phase"), int)
            and trial["max_phase"] >= 1)


def referenced_ncts(extraction):
    """評価の根拠 claim (facts / claims) が参照する NCT 番号の集合。

    v1 世代の extraction は facts[]={source, statement} で source URL に
    NCT 番号を含む。claim 封筒 (v3 世代) でも、根拠リストの中の文字列を
    再帰的に走査して NCT 番号を拾う (識別子は形式が固定のため)。
    根拠リストが無い形では extraction 全体を走査する。
    """
    if not isinstance(extraction, dict):
        return []
    roots = []
    for key in ("facts", "claims"):
        if isinstance(extraction.get(key), list):
            roots.extend(extraction[key])
    if not roots:
        roots = [extraction]
    out = set()

    def walk(x):
        if isinstance(x, str):
            out.update(_NCT_RE.findall(x))
        elif isinstance(x, dict):
            for v in x.values():
                walk(v)
        elif isinstance(x, (list, tuple)):
            for v in x:
                walk(v)

    for r in roots:
        walk(r)
    return sorted(out)


# ---------------------------------------------------------------- 財務

# 金額の単位 → 百万 (同一通貨) への換算倍率と通貨名の切り出し。
# 財務テキストの金額は「△1,277百万米ドル」「230,485百万円」の形で、
# 単位は同一テキスト内では一貫する (DESIGN-V2 §2-1)
_UNIT_RE = r"兆円|億円|百万円|万円|百万[^\s、。,（）()]+|億[^\s、。,（）()]+|円"
_AMOUNT_RE = r"\s*(△|▲|-|−)?\s*([0-9][0-9,]*(?:\.[0-9]+)?)\s*(" + _UNIT_RE + r")?"


def _money(segment, label):
    """segment 内の「ラベル 金額」から金額を抜く。

    戻り値: {"value": 百万単位に換算した値, "currency": 通貨名} または
    None。ラベルの直後の最初の金額だけを読むため、EDINET 形式の
    「(前期 ...)」の比較値は拾わない。
    """
    i = (segment or "").find(label)
    if i < 0:
        return None
    m = re.match(_AMOUNT_RE, segment[i + len(label):])
    if not m or m.group(3) is None:
        return None
    value = float(m.group(2).replace(",", ""))
    if m.group(1):
        value = -value
    unit = m.group(3)
    if unit.startswith("兆"):
        return {"value": value * 1_000_000, "currency": "円"}
    if unit.startswith("億"):
        cur = unit[1:] or "円"
        return {"value": value * 100, "currency": cur}
    if unit.startswith("百万"):
        cur = unit[2:] or "円"
        return {"value": value, "currency": cur}
    if unit.startswith("万"):
        return {"value": value * 0.01, "currency": "円"}
    return {"value": value * 0.000001, "currency": "円"}


_HEADER_RE = re.compile(
    r"(直近四半期|直近通期|通期|直近TTM)\((\d{4}-\d{2}-\d{2})(?:まで)(?:、[^)]*)?\)"
    r"|(貸借対照表)\((\d{4}-\d{2}-\d{2})(?:時点)\)"
    r"|(最新指標)(?=\s)")


def _segments(text):
    """財務テキストを期間見出しごとのセグメントに割る。

    戻り値: [(kind, end_date|None, segment_text), ...]。kind は
    quarterly / annual / balance / latest。見出しの無いテキスト
    (EDINET 形式) は空リスト (generic として別に扱う)。
    """
    out = []
    matches = list(_HEADER_RE.finditer(text or ""))
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        seg = text[m.end():end].strip(" 。、")
        if m.group(1):
            kind = "quarterly" if m.group(1) == "直近四半期" else "annual"
            out.append((kind, m.group(2), seg))
        elif m.group(3):
            out.append(("balance", m.group(4), seg))
        else:
            out.append(("latest", None, seg))
    return out


def _pl_fields(seg):
    """損益セグメントから売上高・純利益・営業CFを抜く。"""
    return {"revenue": _money(seg, "売上高"),
            "net_income": _money(seg, "純利益"),
            "operating_cf": _money(seg, "営業キャッシュフロー")}


def parse_financial_text(text, title=None):
    """財務候補の abstract (固定書式テキスト) を構造化する。

    戻り値: {"source_label": str|None,
             "annual": {期間末日: {revenue, net_income, operating_cf}},
             "balance": {"date", "cash", "short_investments",
                         "debt_due_1y"} | None,
             "latest_cash": 現預金 (広義) | None,
             "generic_annual": EDINET 形式の年次損益 | None}

    通期売上の系列 (Darwinian Sea) には、期間末日が明記された通期
    セグメント (annual) だけを使う。EDINET 形式の「(前期 ...)」比較値は
    前期の期間末日がテキスト内に明記されないため、系列には入れない
    (generic_annual は年次損益としてランウェイの burn にだけ使う)。
    """
    text = text or ""
    src = re.search(r"財務数値\(([^)]+)\)", text)
    out = {"source_label": src.group(1) if src else None,
           "annual": {}, "balance": None, "latest_cash": None,
           "generic_annual": None}
    segs = _segments(text)
    for kind, end, seg in segs:
        if kind == "annual":
            out["annual"][end] = _pl_fields(seg)
        elif kind == "balance":
            bal = {"date": end,
                   "cash": _money(seg, "現金及び現金同等物"),
                   "short_investments": _money(seg, "短期有価証券"),
                   "debt_due_1y": None}
            for lab in ("1年以内返済予定の長期借入金", "1年以内返済予定の社債",
                        "1年以内に返済期限が来る債務"):
                due = _money(seg, lab)
                if due is not None:
                    bal["debt_due_1y"] = due
                    break
            if out["balance"] is None or end >= out["balance"]["date"]:
                out["balance"] = bal
        elif kind == "latest":
            cash = _money(seg, "現預金")
            if cash is not None:
                out["latest_cash"] = cash
    if not segs:
        # EDINET 形式: 見出しが無く「財務数値(EDINET XBRL): 項目 値
        # (前期 値)、...」の形。対象期間が約1年 (年次報告) のときだけ
        # 年次損益として扱う (四半期累計を年次と誤らないため)
        m = re.search(r"対象期間:\s*(\d{4}-\d{2}-\d{2})〜(\d{4}-\d{2}-\d{2})",
                      text)
        if not m and title:
            m2 = re.search(r"\((\d{4})/(\d{2})/(\d{2})[－-](\d{4})/(\d{2})/(\d{2})\)",
                           title)
            if m2:
                m = (f"{m2.group(1)}-{m2.group(2)}-{m2.group(3)}",
                     f"{m2.group(4)}-{m2.group(5)}-{m2.group(6)}")
        if m:
            try:
                start = date.fromisoformat(m[0] if isinstance(m, tuple) else m.group(1))
                end = date.fromisoformat(m[1] if isinstance(m, tuple) else m.group(2))
            except ValueError:
                start = end = None
            # 年次の判定幅は ESEF の年次 flow 判定 (300〜400日) と同じ
            if start and end and 300 <= (end - start).days <= 400:
                body = text.split(":", 1)[-1]
                out["generic_annual"] = dict(
                    _pl_fields(body), end=end.isoformat())
    return out


def _burn(annual_fields):
    """規程第12条5の burn: 年間の純損失を主とし、営業CFマイナスが
    大きければそちら。戻り値: (burn 金額|None, burn が無い=True|False)。

    純利益・営業CFのどちらかが見えていて両方ともマイナスでなければ
    「burn 無し」(黒字側)。どちらも見えなければ burn は不明 (None)。
    """
    ni = (annual_fields or {}).get("net_income")
    ocf = (annual_fields or {}).get("operating_cf")
    if ni is None and ocf is None:
        return None, False
    burn = 0.0
    if ni is not None:
        burn = max(burn, -ni["value"])
    if ocf is not None:
        burn = max(burn, -ocf["value"])
    if burn <= 0:
        return 0.0, True
    return burn, False


def _snapshot(parsed, source, published, candidate_id):
    """1件の財務候補テキストからランウェイ計算用のスナップショットを作る。

    原資と burn は同一テキスト (同一時点の開示) から取る。通貨が混ざる
    ものは計算しない。戻り値: dict | None (原資か burn が抜けない場合)。
    あわせて、その候補の通期セグメント (annual) も保持する。
    """
    annual = parsed.get("annual") or {}
    annual_end, annual_fields = None, None
    if annual:
        annual_end = max(annual)
        annual_fields = annual[annual_end]
    elif parsed.get("generic_annual"):
        annual_fields = parsed["generic_annual"]
        annual_end = annual_fields.get("end")
    if annual_fields is None:
        return None
    # 原資: 広義の現預金 (最新指標) があればそれ自体が現預金+短期投資。
    # 無ければ貸借対照表の現金+短期有価証券 (短期投資が無い表示は 0 扱い)
    resources, res_currency = None, None
    if parsed.get("latest_cash") is not None:
        resources = parsed["latest_cash"]["value"]
        res_currency = parsed["latest_cash"]["currency"]
    elif parsed.get("balance") and parsed["balance"].get("cash") is not None:
        bal = parsed["balance"]
        resources = bal["cash"]["value"]
        res_currency = bal["cash"]["currency"]
        if bal.get("short_investments") is not None:
            if bal["short_investments"]["currency"] != res_currency:
                return None
            resources += bal["short_investments"]["value"]
        # 1年以内に返済期限が来る債務は原資から差し引く (規程第12条5)
        if bal.get("debt_due_1y") is not None:
            if bal["debt_due_1y"]["currency"] != res_currency:
                return None
            resources -= bal["debt_due_1y"]["value"]
    if resources is None:
        return None
    burn, no_burn = _burn(annual_fields)
    if burn is None:
        return None
    # burn 側の通貨が原資と違うスナップショットでは割り算をしない
    for f in (annual_fields.get("net_income"), annual_fields.get("operating_cf")):
        if f is not None and f["currency"] != res_currency:
            return None
    completeness = sum(1 for v in list(annual_fields.values())
                       if isinstance(v, dict)) \
        + (1 if parsed.get("balance") else 0) \
        + (1 if parsed.get("latest_cash") is not None else 0)
    return {"runway": (None if no_burn else resources / burn),
            "no_burn": no_burn, "resources": resources, "burn": burn,
            "currency": res_currency, "annual_end": annual_end,
            "balance_date": (parsed.get("balance") or {}).get("date"),
            "source": source, "source_label": parsed.get("source_label"),
            "published": published, "candidate_id": candidate_id,
            "completeness": completeness, "annual": annual}


def select_snapshot(records):
    """企業の財務候補群から、ランウェイに使う最新スナップショットを選ぶ。

    選択規則: 期間 (貸借対照表日と通期末日の新しい方) が最新のものを
    主とし、同期間では完全性 (抜けた項目の数) → 出典の権威階層
    (_SOURCE_PRIORITY) → 公表日 → 候補 id の順で決定的に選ぶ。
    """
    snaps = []
    for r in records or []:
        parsed = parse_financial_text(r.get("abstract") or r.get("text"),
                                      r.get("title"))
        s = _snapshot(parsed, r.get("source"), r.get("published"),
                      r.get("id") or r.get("candidate_id"))
        if s is not None:
            snaps.append(s)
    if not snaps:
        return None

    def key(s):
        period = max(s.get("balance_date") or "", s.get("annual_end") or "")
        return (period, s["completeness"],
                -_SOURCE_PRIORITY.get(s["source"], 9),
                s.get("published") or "", s.get("candidate_id") or "")

    return max(snaps, key=key)


def annual_revenue_series(records):
    """企業の財務候補群から通期売上の系列 [(期間末日, 金額, 通貨), ...]
    を期間末日の昇順で作る (Darwinian Sea 用)。

    期間末日が明記された通期セグメントだけを使う (parse_financial_text
    の annual)。同一末日に複数出典があれば、出典の権威階層で上位を採る。
    """
    by_end = {}
    for r in records or []:
        parsed = parse_financial_text(r.get("abstract") or r.get("text"),
                                      r.get("title"))
        for end, fields in (parsed.get("annual") or {}).items():
            rev = fields.get("revenue")
            if rev is None:
                continue
            cur = by_end.get(end)
            rank = -_SOURCE_PRIORITY.get(r.get("source"), 9)
            if cur is None or rank > cur[0]:
                by_end[end] = (rank, rev)
    return [(end, by_end[end][1]["value"], by_end[end][1]["currency"])
            for end in sorted(by_end)]


def compute_runway(records):
    """企業の財務候補群 → ランウェイ (年) と計算の内訳。

    戻り値: {"runway": float|None, "no_burn": bool, "snapshot": ...} 。
    スナップショット自体が作れなければ runway=None かつ snapshot=None
    (判定しない側)。黒字で burn が無い場合は no_burn=True。
    """
    snap = select_snapshot(records)
    if snap is None:
        return {"runway": None, "no_burn": False, "snapshot": None}
    return {"runway": snap["runway"], "no_burn": snap["no_burn"],
            "snapshot": snap}


# ---------------------------------------------------------------- 導出

def _fmt_money(value, currency):
    """百万単位の値を根拠文用に整形する (例: 89百万スイスフラン)。"""
    unit = "百万円" if currency == "円" else f"百万{currency}"
    if abs(value - round(value)) < 0.05:
        return f"{round(value):,}{unit}"
    return f"{value:,.1f}{unit}"


def barriers_for_evaluation(trl_level, extraction, financial_records,
                            trials_by_nct):
    """1件の long 評価の関門を導く。

    trl_level: 代表 TRL (整数 1〜9 でなければ全関門 na)。
    extraction: 評価の extraction (dict)。Devil River の NCT 参照に使う。
    financial_records: その企業の財務候補 (dict のリスト)。企業が一意に
      定まらない評価では None を渡し、企業単位の関門は unknown になる。
    trials_by_nct: {NCT番号: parse_trial_status の戻り値}。
    戻り値: {"v", "trl", 関門キー: {"status", "evidence"}} の形で、
    evidence は hit のときだけ付く (根拠文は導出側が生成する)。
    """
    out = {"v": BARRIER_RULES_VERSION, "trl": None}
    if not isinstance(trl_level, int) or isinstance(trl_level, bool) \
            or not 1 <= trl_level <= 9:
        for k in BARRIER_ORDER:
            out[k] = {"status": "na", "evidence": None}
        return out
    out["trl"] = trl_level

    # --- Devil River (技術単位) ---
    if trl_level > 3:
        out["devil_river"] = {"status": "na", "evidence": None}
    else:
        started = any(trial_started_phase1((trials_by_nct or {}).get(n))
                      for n in referenced_ncts(extraction))
        if started:
            out["devil_river"] = {"status": "clear", "evidence": None}
        else:
            out["devil_river"] = {
                "status": "hit",
                "evidence": f"TRL {trl_level}。根拠 claim が参照する治験に、"
                            "開始済みの Phase 1 以上なし"}

    # --- Valley of Death (企業単位) ---
    if trl_level < 4:
        out["valley_of_death"] = {"status": "na", "evidence": None}
    elif financial_records is None:
        out["valley_of_death"] = {"status": "unknown", "evidence": None}
    else:
        rw = compute_runway(financial_records)
        snap = rw["snapshot"]
        if snap is None:
            out["valley_of_death"] = {"status": "unknown", "evidence": None}
        elif rw["no_burn"]:
            out["valley_of_death"] = {"status": "clear", "evidence": None}
        elif rw["runway"] < RUNWAY_THRESHOLD_YEARS:
            src = snap.get("source_label") or snap.get("source") or "財務"
            period = snap.get("annual_end") or ""
            out["valley_of_death"] = {
                "status": "hit",
                "evidence": f"ランウェイ {rw['runway']:.1f}年"
                            f"（原資 {_fmt_money(snap['resources'], snap['currency'])}、"
                            f"年間 burn {_fmt_money(snap['burn'], snap['currency'])}、"
                            f"{src} 財務 {period} 通期）"}
        else:
            out["valley_of_death"] = {"status": "clear", "evidence": None}

    # --- Darwinian Sea (企業単位) ---
    if trl_level < SEA_MIN_TRL:
        out["darwinian_sea"] = {"status": "na", "evidence": None}
    elif financial_records is None:
        out["darwinian_sea"] = {"status": "unknown", "evidence": None}
    else:
        series = annual_revenue_series(financial_records)
        res = {"status": "unknown", "evidence": None}
        if len(series) >= 2:
            (prev_end, prev_rev, prev_cur), (last_end, last_rev, last_cur) = \
                series[-2], series[-1]
            try:
                gap = (date.fromisoformat(last_end)
                       - date.fromisoformat(prev_end)).days
            except ValueError:
                gap = None
            # 前年度と比較できるのは、末日の間隔が1会計年度分 (年次 flow
            # の判定幅 300〜400日、ESEF と同じ) の組だけ
            if gap is not None and 300 <= gap <= 400 \
                    and prev_cur == last_cur:
                if last_rev == 0:
                    res = {"status": "clear", "evidence": None}  # 売上なし=市場に出ていない
                elif prev_rev > 0 and last_rev < prev_rev:
                    pct = (prev_rev - last_rev) / prev_rev * 100
                    cur = last_cur
                    res = {"status": "hit",
                           "evidence": f"通期売上 前年比 -{pct:.1f}%"
                                       f"（{prev_end} {_fmt_money(prev_rev, cur)} → "
                                       f"{last_end} {_fmt_money(last_rev, cur)}）"}
                else:
                    res = {"status": "clear", "evidence": None}
        out["darwinian_sea"] = res
    return out


# ---------------------------------------------------------------- DB 入力

def load_barrier_inputs(store):
    """export 用に、治験 (NCT 単位) と企業ごとの財務候補を DB から読む。

    戻り値: {"trials": {NCT: parse_trial_status の戻り値},
             "financials": {entity_id: [財務候補 dict, ...]}}。
    読み取りのみ。治験は同一 NCT に複数候補があれば新しい方を採る。
    財務候補は strong な企業リンクを持つ disclosure のうち財務数値
    マーカーを含むもの (related_financials と同じ母集団) で、重複統合
    された行は除く。
    """
    trials = {}
    trial_rank = {}
    for cid, source_id, abstract, published, retrieved in store.conn.execute(
            "SELECT id, source_id, abstract, published, retrieved_at "
            "FROM candidates WHERE source_id LIKE 'NCT%'"):
        if not source_id or not _NCT_RE.fullmatch(source_id.strip()):
            continue
        nct = source_id.strip()
        rank = (published or "", retrieved or "", cid)
        if nct not in trial_rank or rank > trial_rank[nct]:
            trial_rank[nct] = rank
            trials[nct] = parse_trial_status(abstract)
    financials = {}
    for eid, cid, source, title, abstract, published in store.conn.execute(
            "SELECT ce.entity_id, c.id, c.source, c.title, c.abstract, "
            "c.published FROM candidates c "
            "JOIN candidate_entities ce ON ce.candidate_id = c.id "
            "AND ce.link_type = 'entity' AND ce.strength = 'strong' "
            "WHERE c.info_type = 'disclosure' "
            "AND c.abstract LIKE '%財務数値(%' "
            "AND NOT (c.status = 'rejected' "
            "AND c.screen_reason LIKE 'duplicate_of:%')"):
        financials.setdefault(eid, []).append(
            {"id": cid, "source": source, "title": title,
             "abstract": abstract, "published": published})
    return {"trials": trials, "financials": financials}


def barriers_for_store_evaluation(store, inputs, candidate_id, ev_row):
    """build_site_data 用: 評価行と候補から関門を導く薄いラッパ。

    企業単位の関門は、候補への strong な企業リンクが一意のときだけ
    その企業の財務で判定し、一意でなければ unknown にする (tier の
    帰属条件と同じ置き方。別企業の財務を貼らない)。
    """
    extra = {}
    extraction = None
    if ev_row is not None:
        def _col(name):
            try:
                return ev_row[name]
            except (KeyError, IndexError, TypeError):
                return None
        try:
            extra = json.loads(_col("extra_json") or "{}")
        except (TypeError, ValueError):
            extra = {}
        try:
            extraction = json.loads(_col("extraction_json") or "null")
        except (TypeError, ValueError):
            extraction = None
    trl = extra.get("trl_level") if isinstance(extra, dict) else None
    level = trl.get("level") if isinstance(trl, dict) else None
    strong = {eid for (eid,) in store.conn.execute(
        "SELECT DISTINCT entity_id FROM candidate_entities "
        "WHERE candidate_id = ? AND link_type = 'entity' "
        "AND strength = 'strong'", (candidate_id,))}
    records = None
    if len(strong) == 1:
        records = inputs["financials"].get(next(iter(strong)), [])
    return barriers_for_evaluation(level, extraction, records,
                                   inputs["trials"])
