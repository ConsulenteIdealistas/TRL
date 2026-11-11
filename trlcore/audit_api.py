"""評価監査の API 照合層 (第3の監査者)。

規則監査 (audit.py / audit_positive.py) が保存済みレコードだけを決定的に
照合するのに対し、本層は評価の根拠が参照する外部レジストリを実際に
引いて、登録事実と評価の記録を突き合わせる。使うのはキー不要の2つだけ:
ClinicalTrials.gov API v2 と Crossref。LLM は使わない。対象は
監査要項 (~/workspace/trl-audit/AUDIT_REQUIREMENTS.md) の Phase 2
(出典・帰属・時点の検証) と Phase 3 (反証の収集) に対応する。

規則監査との違いは「鮮度モデル」。規則は版 (rules の sha) が変われば
再監査になるが、レジストリの事実は時間で変わる (治験は後から中止に
なる) ため、本層は「最後の API 監査から FRESH_DAYS 日以上経過した
評価」を対象に再照合する。30日は月次観測のリズムに合わせた運用値。
監査結果は audit_results に auditor_provider="api" /
auditor_model="api/v1" で記録し、規則側の冪等キーとは衝突しない。
prompt_sha は照合設定 (チェック定義+鮮度窓) のハッシュに実行日を加えた
世代印: 鮮度モデルでは同じ評価を繰り返し照合するため、世代が進めば
新しい行として記録できる必要がある。

失敗の扱い (静かな0件の教訓): ネットワークエラー・タイムアウト・
429・5xx は「照合できなかった」のであって所見ではない。その評価は
何も記録せず、次回に再試行できる状態で残し、run の集計では skipped
に数える。逆に 404 (識別子が実在しない) はレジストリからの確定した
応答なので、所見として flag にする。なお DOI の実在判定は Crossref
だけでは不十分 (他レジストリ登録分が漏れる) なため、CR1 は DOI
ハンドルとの2段確認にする (詳細は _check_dois)。

flags の形は規則監査と同じ {check, item, verdict, rule, detail} に、
照合の来歴として source_api / fetched_at / snapshot (フラグの根拠に
なった登録事実) を追加する。サイト表示 (site._audit_entries) は
既知キーだけを読むため、追加キーは表示に影響しない。
"""
import hashlib
import json
import re
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

API_VERSION = "v1"
API_PROVIDER = "api"
API_MODEL = "api/v1"
FRESH_DAYS = 30
API_CHECKS = ("CR1", "CT1", "CT2")
STOP_STATUSES = ("TERMINATED", "WITHDRAWN", "SUSPENDED")
REQUEST_INTERVAL_SEC = 0.3
TIMEOUT_SEC = 20
MAX_IDS_PER_KIND = 10  # 1評価あたりの照合上限 (NCT / DOI それぞれ)

_CT_API = "https://clinicaltrials.gov/api/v2/studies"
_CR_API = "https://api.crossref.org/works"
_HANDLE_API = "https://doi.org/api/handles"
_USER_AGENT = "TRL-audit/1.0"

_NCT_RE = re.compile(r"NCT\d{8}", re.IGNORECASE)
_DOI_RE = re.compile(r"10\.\d{4,9}/[^\s\"'<>\)\]]+")

# CT1: 反証側に中止系の旨が記録されているとみなす語彙。「終了」は
# completed (治験の正常完了) とも読めるため含めない。
_TERM_WORDS = ("中止", "打ち切り", "打切り", "中断", "撤回",
               "terminated", "termination", "withdrawn", "withdrawal",
               "suspended", "suspension")


class FetchFailed(Exception):
    """照合リクエストの失敗 (未実施扱いにするための送出)。"""


def _now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _api_prompt_sha(run_day):
    """照合設定のハッシュ + 実行日 (世代印)。run_day は UTC の
    YYYY-MM-DD。同じ日に同じ設定で再実行しても冪等キーが衝突して
    二重記録にはならず、日付が進めば新しい世代として記録できる。"""
    cfg_obj = {"version": API_VERSION, "checks": list(API_CHECKS),
               "fresh_days": FRESH_DAYS, "stop_statuses": list(STOP_STATUSES)}
    base = json.dumps(cfg_obj, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(f"{base}|{run_day}".encode("utf-8")).hexdigest()[:12]


def _fetch_json(url):
    """GET して (status, json|None) を返す。404 は確定応答として
    (404, None)。それ以外の HTTP エラー (429・5xx 含む) と通信失敗・
    不正な JSON は FetchFailed (未実施扱い)。リトライはしない:
    照合は次回の監査実行で再試行されるため、ここで粘らない。"""
    req = urllib.request.Request(
        url, headers={"User-Agent": _USER_AGENT, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SEC) as resp:
            status = resp.getcode()
            body = resp.read()
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return 404, None
        raise FetchFailed(f"HTTP {e.code} for {url}")
    except (urllib.error.URLError, TimeoutError, ConnectionError,
            OSError) as e:
        raise FetchFailed(f"{type(e).__name__} for {url}")
    try:
        return status, json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise FetchFailed(f"invalid JSON for {url}")


# ---------------------------------------------------------------------------
# 識別子の抽出
# ---------------------------------------------------------------------------

def _walk_text(obj):
    """文字列リーフを全て集めて連結する (audit._extraction_text と同じ流儀)。"""
    parts = []

    def walk(o):
        if isinstance(o, str):
            parts.append(o)
        elif isinstance(o, dict):
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(obj)
    return " ".join(parts)


def _norm(s):
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", s or "")).strip()


def _clean_doi(d):
    """抽出テキスト中の DOI を整える: URL・接頭辞を外し、末尾の句読点
    (文末のピリオドや JSON 由来のカンマ等) を落とす。"""
    d = (d or "").strip()
    for prefix in ("https://doi.org/", "http://doi.org/", "doi:"):
        if d.lower().startswith(prefix):
            d = d[len(prefix):]
    d = d.strip().rstrip(".,;:")
    while d.endswith(")") and d.count(")") > d.count("("):
        d = d[:-1].rstrip(".,;:")
    return d


def extract_identifiers(inp, cand):
    """評価 (監査入力 + 候補レコード) から照合対象の識別子を集める。
    -> (ncts, dois)。dois は (doi, item ヒント) のリストで、ヒントは
    最初に見つかった場所 (候補自身なら "candidate"、項目の根拠なら
    その項目キー、抽出なら "extraction")。タイトル検索のような曖昧な
    照合はせず、明示された識別子だけを対象にする。"""
    ncts, dois = [], []
    seen_n, seen_d = set(), set()

    def scan(text, hint=None):
        for m in _NCT_RE.finditer(text or ""):
            n = m.group(0).upper()
            if n not in seen_n:
                seen_n.add(n)
                ncts.append(n)
        for m in _DOI_RE.finditer(text or ""):
            d = _clean_doi(m.group(0))
            if d and d.lower() not in seen_d:
                seen_d.add(d.lower())
                dois.append((d, hint))

    cand = cand or {}
    if cand.get("doi"):
        d = _clean_doi(str(cand["doi"]))
        if d and d.lower() not in seen_d:
            seen_d.add(d.lower())
            dois.append((d, "candidate"))
    scan(cand.get("url"), "candidate")
    scan(cand.get("title"))
    scan(cand.get("abstract"))
    for key, it in (inp.get("items") or {}).items():
        if isinstance(it, dict):
            scan(_walk_text(it), key)
    trl = inp.get("trl_level")
    if isinstance(trl, dict):
        scan(_walk_text(trl), "trl_level")
    scan(_walk_text(inp.get("extraction")), "extraction")
    return ncts[:MAX_IDS_PER_KIND], dois[:MAX_IDS_PER_KIND]


def _counter_text(inp):
    """反証側のテキスト (抽出の counter_points + 各項目の
    counter_evidence) を正規化して小文字で返す (CT1 の記録判定用)。
    項目側の母数は A8 と揃え、参考項目 (規程第13条) の counter_evidence
    は数えない (N-1)。"""
    refs = set(inp.get("reference_items") or [])
    parts = []
    ext = inp.get("extraction")
    if isinstance(ext, dict):
        parts.append(_walk_text(ext.get("counter_points")))
    for key, it in (inp.get("items") or {}).items():
        if key in refs:
            continue
        if isinstance(it, dict):
            parts.append(_walk_text(it.get("counter_evidence")))
    return _norm(" ".join(parts)).lower()


def _termination_recorded(counter_text, nct):
    """反証側にその治験の中止系ステータスが記録されているか。
    NCT 番号そのものへの言及か、中止系の語彙があれば記録ありとする
    (どちらも無ければ「記録されていない」)。"""
    if nct.lower() in counter_text:
        return True
    return any(w in counter_text for w in _TERM_WORDS)


# ---------------------------------------------------------------------------
# 企業名の照合 (CT2)。表記ゆれが多いため保守的に判定する: 法人格の
# 接尾辞や汎用語を落としたトークン集合が少しでも重なれば一致とみなし、
# 完全にばらばらな場合だけ「明らかに一致しない」として記録する。
# ---------------------------------------------------------------------------

_NAME_DROP = {"inc", "corp", "corporation", "ltd", "limited", "llc", "llp",
              "plc", "gmbh", "ag", "sa", "sarl", "nv", "bv", "co", "company",
              "holdings", "holding", "group", "the", "and", "of"}


def _name_tokens(name):
    t = unicodedata.normalize("NFKC", name or "").lower()
    t = re.sub(r"[^0-9a-z\u3040-\u30ff\u4e00-\u9fff]+", " ", t)
    return {w for w in t.split() if len(w) >= 2 and w not in _NAME_DROP}


def _names_match(a, b):
    """True=一致とみなす / False=明らかに一致しない / None=判定不能
    (どちらかの名前から有意なトークンが取れない)。"""
    ta, tb = _name_tokens(a), _name_tokens(b)
    if not ta or not tb:
        return None
    if ta & tb:
        return True
    sa = re.sub(r"[^0-9a-z]+", "", unicodedata.normalize("NFKC", a or "").lower())
    sb = re.sub(r"[^0-9a-z]+", "", unicodedata.normalize("NFKC", b or "").lower())
    if sa and sb and (sa in sb or sb in sa):
        return True
    # 連結した表記ゆれ (ModernaTX 対 Moderna 等): 一方のトークンが
    # 他方のトークンの接頭辞になっていれば一致とみなす。短い語幹での
    # 誤一致を避けるため、短い方に5文字以上を要求する。
    if any(x != y and (x.startswith(y) or y.startswith(x))
           and min(len(x), len(y)) >= 5 for x in ta for y in tb):
        return True
    return False


# ---------------------------------------------------------------------------
# 照合チェック。get(kind, key, url) は (status, data, fetched_at) を返し、
# 失敗時は FetchFailed を送出する (runner がキャッシュと間隔を担う)。
# ---------------------------------------------------------------------------

def _study_snapshot(study):
    ps = (study or {}).get("protocolSection") or {}
    status = ps.get("statusModule") or {}
    sponsor = ((ps.get("sponsorCollaboratorsModule") or {})
               .get("leadSponsor") or {}).get("name")
    enrollment = ((ps.get("designModule") or {}).get("enrollmentInfo") or {})
    idm = ps.get("identificationModule") or {}
    return {"nct_id": idm.get("nctId"),
            "status": status.get("overallStatus"),
            "last_update": (status.get("lastUpdatePostDateStruct") or {}).get("date"),
            "sponsor": sponsor,
            "enrollment": enrollment.get("count"),
            "title": idm.get("briefTitle")}


def _check_trials(inp, ncts, get, out, stats):
    """CT1 (⑦反証) / CT2 (③企業帰属)。NCT ごとに ClinicalTrials.gov を
    引き、中止系ステータスと Lead Sponsor を評価の記録と突き合わせる。"""
    counter = _counter_text(inp)
    strong = [c.get("name") for c in inp.get("companies") or []
              if c.get("strength") == "strong" and c.get("name")]
    for nct in ncts:
        status, data, fetched_at = get("ct", nct, f"{_CT_API}/{nct}")
        stats["nct"] += 1
        if status == 404:
            out.append({
                "check": "CT1", "item": "counter_points", "verdict": "flag",
                "rule": "ctgov-not-found",
                "detail": f"根拠が参照する治験 {nct} が ClinicalTrials.gov に"
                          f"実在しない (404)。識別子の誤記か引用ミスの疑い。",
                "source_api": "clinicaltrials.gov", "fetched_at": fetched_at,
                "snapshot": {"nct_id": nct, "status_code": 404}})
            continue
        snap = _study_snapshot(data)
        st = (snap.get("status") or "").upper()
        if st in STOP_STATUSES and not _termination_recorded(counter, nct):
            out.append({
                "check": "CT1", "item": "counter_points", "verdict": "flag",
                "rule": f"status:{st}",
                "detail": f"根拠が参照する治験 {nct} は ClinicalTrials.gov で"
                          f" {st} (中止系、最終更新 "
                          f"{snap.get('last_update') or '不明'}) になっているが、"
                          f"反証 (counter_points / counter_evidence) にその旨の"
                          f"記録が見当たらない。",
                "source_api": "clinicaltrials.gov", "fetched_at": fetched_at,
                "snapshot": snap})
        sponsor = snap.get("sponsor")
        if sponsor and strong:
            if all(_names_match(sponsor, name) is False for name in strong):
                out.append({
                    "check": "CT2", "item": "companies", "verdict": "gray",
                    "rule": "sponsor-mismatch",
                    "detail": f"治験 {nct} の Lead Sponsor は「{sponsor}」だが、"
                              f"strong リンク企業 ({', '.join(strong)}) と名前が"
                              f"一致しない。帰属の妥当性は判定保留。",
                    "source_api": "clinicaltrials.gov",
                    "fetched_at": fetched_at, "snapshot": snap})


def _check_dois(dois, get, out, stats):
    """CR1 (①出典)。明示された DOI の実在を2段で確認する: まず
    Crossref で引き、404 なら DOI ハンドル (doi.org の Handle API)
    でも引く。Crossref は Crossref 登録分の索引でしかなく、DataCite
    等の他レジストリに登録された DOI (例: PANGAEA のデータ DOI) は
    Crossref に無くて当然なため、404 だけでは「実在しない」とは
    言えない (本番試行で実証: 10.1594/PANGAEA.989871 は Crossref
    404 だが DataCite 登録済みで実在)。両方で実在しない場合だけ
    要確認にする。引けた場合は所見なし (監査行そのものが照合実施の
    記録になる)。"""
    for doi, hint in dois:
        url = f"{_CR_API}/{urllib.parse.quote(doi, safe='')}"
        status, data, fetched_at = get("cr", doi.lower(), url)
        stats["doi"] += 1
        if status != 404:
            continue
        hurl = f"{_HANDLE_API}/{urllib.parse.quote(doi, safe='')}"
        hstatus, hdata, hfetched_at = get("doi", doi.lower(), hurl)
        code = hdata.get("responseCode") if isinstance(hdata, dict) else None
        if hstatus != 404 and code == 1:
            continue  # 他レジストリ (DataCite 等) に登録済みで実在する
        if hstatus != 404 and code != 100:
            # ハンドル側の応答が判定不能な形なら、未実施として扱う
            raise FetchFailed(f"handle lookup indeterminate for {doi}")
        out.append({
            "check": "CR1", "item": hint or "candidate", "verdict": "flag",
            "rule": "doi-not-found",
            "detail": f"明示された DOI {doi} が Crossref にも DOI ハンドル"
                      f"にも実在しない (Crossref 404 / handle "
                      f"responseCode={code if code is not None else hstatus})。"
                      f"出典の誤記か、実在しない出典の疑い。",
            "source_api": "crossref", "fetched_at": hfetched_at,
            "snapshot": {"doi": doi, "crossref_status": 404,
                         "handle_response_code": code,
                         "handle_status": hstatus}})


def audit_evaluation_api(inp, cand, get):
    """評価1件に API 照合を適用し、(entries, stats) を返す。
    照合リクエストが失敗したら FetchFailed がそのまま伝播する
    (runner 側でその評価を未実施として扱う)。"""
    ncts, dois = extract_identifiers(inp, cand)
    out, stats = [], {"nct": 0, "doi": 0}
    if ncts:
        _check_trials(inp, ncts, get, out, stats)
    if dois:
        _check_dois(dois, get, out, stats)
    return out, stats


# ---------------------------------------------------------------------------
# runner: 鮮度モデルでの対象選択と記録
# ---------------------------------------------------------------------------

def select_stale_api(store, fresh_days, limit=100, kind="long"):
    """最後の API 監査から fresh_days 日以上経過した (または API 監査が
    無い) 評価を古い順に返す。鮮度は audit_results の created_at
    (auditor_provider='api' の最新行) で判定する。"""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=fresh_days)) \
        .isoformat(timespec="seconds")
    rows = store.conn.execute(
        "SELECT e.* FROM evaluations e WHERE e.kind = ?"
        " AND NOT EXISTS (SELECT 1 FROM audit_results a"
        "   WHERE a.evaluation_id = e.id AND a.auditor_provider = ?"
        "   AND a.created_at >= ?)"
        " ORDER BY e.evaluated_at ASC, e.id ASC LIMIT ?",
        (kind, API_PROVIDER, cutoff, int(limit))).fetchall()
    return [dict(r) for r in rows]


def run_audit_api(store, cfg, *, limit=100, log=print, fetch=None,
                  sleep=time.sleep):
    """API 照合層の実行。規則監査 (run_audit_rules) の後に呼ばれる前提で、
    ここでの失敗が規則監査の結果に影響しないよう、評価単位で例外を
    隔離する。照合リクエストが失敗した評価は記録せず skipped に数える
    (次回に再試行される)。limit は種類 (long/event) ごとの上限。
    fetch はテスト用の差し替え口 (url -> (status, json) / FetchFailed)。"""
    from .audit import build_audit_input  # 循環回避のため遅延 import
    fetch = fetch or _fetch_json
    now = datetime.now(timezone.utc)
    batch = "api-" + now.strftime("%Y%m%dT%H%M%SZ")
    sha = _api_prompt_sha(now.strftime("%Y-%m-%d"))
    rows = select_stale_api(store, FRESH_DAYS, limit, kind="long") \
        + select_stale_api(store, FRESH_DAYS, limit, kind="event")
    cache = {}
    state = {"requests": 0}

    def get(kind, key, url):
        ck = (kind, key)
        if ck in cache:
            return cache[ck]
        if state["requests"]:
            sleep(REQUEST_INTERVAL_SEC)
        state["requests"] += 1
        fetched_at = _now_iso()
        status, data = fetch(url)  # FetchFailed は呼び出し側で隔離
        cache[ck] = (status, data, fetched_at)
        return cache[ck]

    audited = skipped = failed = n_flags = n_grays = 0
    with_ids = n_nct = n_doi = 0
    checks = {}
    for row in rows:
        try:
            inp = build_audit_input(store, row, cfg)
            cand = store.get_candidate(row["candidate_id"]) or {}
            entries, stats = audit_evaluation_api(inp, cand, get)
        except FetchFailed as e:
            skipped += 1
            log(f"api audit skipped (ev {row['id']}): {e}")
            continue
        except Exception as e:  # 1件の想定外で全体を落とさない (規則側と同じ)
            failed += 1
            log(f"api audit failed (ev {row['id']}): {type(e).__name__}: {e}")
            continue
        if stats["nct"] or stats["doi"]:
            with_ids += 1
        n_nct += stats["nct"]
        n_doi += stats["doi"]
        nf = sum(1 for e in entries if e.get("verdict") == "flag")
        ng = sum(1 for e in entries if e.get("verdict") == "gray")
        store.add_audit_result({
            "evaluation_id": row["id"], "candidate_id": row["candidate_id"],
            "run_id": batch, "auditor_provider": API_PROVIDER,
            "auditor_model": API_MODEL, "prompt_version": API_VERSION,
            "prompt_sha": sha, "flags": entries,
            "summary": f"API照合: 要確認 {nf} 件 / グレー {ng} 件" if entries else ""})
        audited += 1
        n_flags += nf
        n_grays += ng
        for e in entries:
            c = checks.setdefault(e["check"], {"flag": 0, "gray": 0})
            c["gray" if e.get("verdict") == "gray" else "flag"] += 1
    res = {"batch": batch, "selected": len(rows), "audited": audited,
           "skipped": skipped, "failed": failed, "flags": n_flags,
           "grays": n_grays, "checks": checks, "with_ids": with_ids,
           "nct_refs": n_nct, "doi_refs": n_doi, "requests": state["requests"]}
    log(f"api-audited {audited}/{len(rows)} evaluations "
        f"(要確認 {n_flags} 件, グレー {n_grays} 件, 未実施 {skipped} 件, "
        f"失敗 {failed} 件, 識別子あり {with_ids} 件, "
        f"NCT {n_nct} 件・DOI {n_doi} 件を照合) as {batch}")
    return res
