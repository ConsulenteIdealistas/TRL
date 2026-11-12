"""表示用タイトル・抄録の日本語訳。

サイトに載る候補(評価済み+次に評価される上位)だけを訳して DB に保存する。
訳は1回作れば再利用されるため、実行ごとの追加コストは新規分だけ。
経路は Azure Translator → DeepL API → Google Cloud Translation の順に
試し(キーが無い経路は飛ばす)、どの経路で訳したかを候補ごとに記録して
画面に明記する。翻訳は専用 API だけで行い、評価用 LLM には任せない。
Azure を本命にするのは無料枠(F0: 月200万字)が毎月リセットされ恒常的に
使えるため。DeepL の無料枠(合計100万字・一回限り)は温存し、Google の
無料枠(月50万字)が最終段を受け持つ。
翻訳の失敗はその分を原文表示に落とすだけで、実行全体は止めない。
"""
import json
import os
import re
from collections import Counter
from datetime import timedelta

from .http import http_fetch

MAX_PER_RUN = 30  # 1回の実行で訳す候補数の上限(初回のバックフィル用。定常は新規分のみ)
BATCH = 5         # 1回でまとめて訳す候補数

_CJK = re.compile(r"[぀-ヿ㐀-鿿ｱ-ﾝ]")


def needs_translation(text):
    """日本語らしい文は訳さない。英字を含む文だけを対象にする。"""
    t = (text or "").strip()
    if len(t) < 4:
        return False
    if not re.search(r"[A-Za-z]", t):
        return False
    return len(_CJK.findall(t)) / len(t) < 0.1


# --- DeepL API -------------------------------------------------------------

def deepl_translate(texts, key, http=http_fetch):
    """texts -> 同じ並びの訳文リスト。失敗時は例外。"""
    host = "api-free.deepl.com" if key.endswith(":fx") else "api.deepl.com"
    body = json.dumps({"text": list(texts), "target_lang": "JA"}).encode()
    raw = http(f"https://{host}/v2/translate",
               headers={"Authorization": f"DeepL-Auth-Key {key}",
                        "Content-Type": "application/json"},
               data=body)
    d = json.loads(raw)
    out = [str(t.get("text") or "") for t in d.get("translations", [])]
    if len(out) != len(texts):
        raise ValueError("DeepL: response length mismatch")
    return out


# --- Azure Translator ------------------------------------------------------

def azure_translate(texts, key, region=None, http=http_fetch):
    """texts -> 同じ並びの訳文リスト。失敗時は例外。"""
    headers = {"Ocp-Apim-Subscription-Key": key,
               "Content-Type": "application/json"}
    if region:
        headers["Ocp-Apim-Subscription-Region"] = region
    body = json.dumps([{"Text": t} for t in texts]).encode()
    raw = http("https://api.cognitive.microsofttranslator.com/translate",
               params={"api-version": "3.0", "to": "ja"}, headers=headers, data=body)
    d = json.loads(raw)
    out = [str((t.get("translations") or [{}])[0].get("text") or "") for t in d]
    if len(out) != len(texts):
        raise ValueError("Azure: response length mismatch")
    return out


def _mt_batch(fn):
    """テキスト単位の翻訳関数を、候補単位 (title, abstract) のバッチに変換する。"""
    def run(items):
        flat, slots = [], []
        for i, (title, abstract) in enumerate(items):
            for field, text in (("title", title), ("abstract", abstract)):
                if text:
                    slots.append((i, field))
                    flat.append(text)
        got = fn(flat)
        out = {}
        for (i, field), text in zip(slots, got):
            t_ja, a_ja = out.get(i, (None, None))
            out[i] = (text, a_ja) if field == "title" else (t_ja, text)
        return out
    return run


# --- Google Cloud Translation(最終段) ---------------------------------------

def google_translate(texts, key, http=http_fetch):
    """texts -> 同じ並びの訳文リスト。失敗時は例外。

    APIキーは URL に付けず X-Goog-Api-Key ヘッダーで送る(URL に付けると
    エラー時の URL が実行ログに残り、キーが漏れるおそれがあるため)。
    """
    import html as _html
    body = json.dumps({"q": list(texts), "target": "ja", "format": "text"}).encode()
    raw = http("https://translation.googleapis.com/language/translate/v2",
               headers={"X-Goog-Api-Key": key, "Content-Type": "application/json"},
               data=body)
    d = json.loads(raw)
    out = [_html.unescape(str(t.get("translatedText") or ""))
           for t in d.get("data", {}).get("translations", [])]
    if len(out) != len(texts):
        raise ValueError("Google: response length mismatch")
    return out


# --- 使用量の計測としきい値 --------------------------------------------------

# 無料枠に対する使用量の通知しきい値 (字)。Azure は月 200万字で月初リセット、
# Google は月 50万字、DeepL は合計 100万字の一回限り枠。跨いだら run 記録と
# ログに残し、気づかず枠を使い切ることを防ぐ。
USAGE_THRESHOLDS = [500_000, 1_000_000, 1_250_000, 1_500_000, 1_750_000, 2_000_000]


def crossed_thresholds(before, after):
    """before < t <= after となる しきい値のリスト。"""
    return [t for t in USAGE_THRESHOLDS if before < t <= after]


def usage_totals(store, today):
    """過去の run 記録から段ごとの使用量ベースラインを集計する。

    azure/google は today の属する月の累計、deepl は全期間の通算 (一回限り枠のため)。
    戻り値: {"azure": 字数, "deepl": 字数, "google": 字数}
    """
    totals = {"azure": 0, "deepl": 0, "google": 0}
    month = today.strftime("%Y-%m")
    for started, counts_json in store.conn.execute("SELECT started_at, counts_json FROM runs"):
        try:
            chars = (json.loads(counts_json) or {}).get("translation_chars") or {}
        except (TypeError, ValueError):
            continue
        for name in totals:
            v = chars.get(name)
            if isinstance(v, (int, float)) and (name == "deepl" or str(started or "")[:7] == month):
                totals[name] += int(v)
    return totals


# --- 表示対象の翻訳 ----------------------------------------------------------

def display_ids(store, cfg, today, pending_limit=10):
    """サイトに載る候補の id 集合: 評価済み全件 + 次に評価される上位。"""
    ids = {r[0] for r in store.conn.execute("SELECT DISTINCT candidate_id FROM evaluations")}
    from .pipeline import select_for_eval  # 循環 import 回避のため遅延 import
    ids |= {c["id"] for c in select_for_eval(store, cfg, today, pending_limit, ids_only=True)}
    return ids


def _backends(http, exclude=()):
    """使える翻訳経路を優先順に返す: [(名前, バッチ関数), ...]"""
    chain = []
    azure_key = os.environ.get("AZURE_TRANSLATOR_KEY")
    if azure_key and "azure" not in exclude:
        region = os.environ.get("AZURE_TRANSLATOR_REGION") or None
        chain.append(("azure", _mt_batch(lambda ts: azure_translate(ts, azure_key, region, http))))
    deepl_key = os.environ.get("DEEPL_API_KEY")
    if deepl_key and "deepl" not in exclude:
        chain.append(("deepl", _mt_batch(lambda ts: deepl_translate(ts, deepl_key, http))))
    google_key = os.environ.get("GOOGLE_TRANSLATE_API_KEY")
    if google_key and "google" not in exclude:
        chain.append(("google", _mt_batch(lambda ts: google_translate(ts, google_key, http))))
    return chain


def _wants(c):
    """候補が訳を必要とする側 (title, abstract) を返す。

    出典 (translation_src) の記録が無い訳は、記録を付け始める前の古い訳。
    訳文があっても出典が無い項目は未訳扱いにして訳し直し、出典を付け直す。"""
    try:
        src = json.loads(c.get("translation_src") or "{}")
    except ValueError:
        src = {}
    if not isinstance(src, dict):
        src = {}
    want_title = needs_translation(c["title"]) and (not c.get("title_ja") or "title" not in src)
    want_abs = needs_translation(c["abstract"]) and (not c.get("abstract_ja") or "abstract" not in src)
    return want_title, want_abs


def _run_batches(store, todo, chain, log):
    """(cid, 候補, want_title, want_abs) の並びをバッチ翻訳して保存する。

    -> (訳した件数, 段ごとの翻訳文字数, 失敗した件数)。失敗したバッチは
    原文表示に落ちるだけで残りは続けるが、全経路で訳せなかった候補の
    件数は呼び出し側に返し、実行レポート (counts) に残す (Actions の
    ログ止まりにして観測から消さないため)。"""
    stats = {}
    done = 0
    failed = 0
    for s in range(0, len(todo), BATCH):
        chunk = todo[s:s + BATCH]
        # 必要な側だけ送る (既訳・和文の側まで送ると API 枠と使用量カウントを無駄に消費する)
        items = [(c["title"] if wt else "", c["abstract"] if wa else "")
                 for _, c, wt, wa in chunk]
        got, provider = {}, None
        for name, fn in chain:
            try:
                got = fn(items)
            except Exception as e:  # この経路は諦めて次を試す
                # ステータスコードまで出す (401=キー違い / 403=プラン・接続先違い / 456=上限)。
                # キーはヘッダーで送るため、エラー文字列に秘密情報は含まれない
                log(f"[translate] {name} での翻訳に失敗: {type(e).__name__}: {e}")
                got = {}
            if len(got) >= len(chunk):
                provider = name
                break
            got = {}
        if provider is None:
            failed += len(chunk)
            continue
        sent = sum(len(x) for pair in items for x in pair if x)
        stats[provider] = stats.get(provider, 0) + sent
        for i, (cid, c, want_title, want_abs) in enumerate(chunk):
            t_ja, a_ja = got[i]
            store.set_translation(cid,
                                  t_ja if want_title else c.get("title_ja"),
                                  a_ja if want_abs else c.get("abstract_ja"),
                                  title_by=provider if want_title else None,
                                  abstract_by=provider if want_abs else None)
            done += 1
    return done, stats, failed


def translate_display_set(store, cfg, today, log=print, http=http_fetch):
    """表示対象のうち未訳のものを訳して保存する。

    -> (訳した件数, 段ごとの翻訳文字数 {"azure": 字数, ...}, 失敗した件数)。
    文字数は実際に送信して訳し終えた原文の字数 (課金・枠消費の単位)。
    """
    chain = _backends(http)
    if not chain:
        return 0, {}, 0
    todo = []
    for cid in sorted(display_ids(store, cfg, today)):
        c = store.get_candidate(cid)
        if not c:
            continue
        want_title, want_abs = _wants(c)
        if want_title or want_abs:
            todo.append((cid, c, want_title, want_abs))
        if len(todo) >= MAX_PER_RUN:
            break
    done, stats, failed = _run_batches(store, todo, chain, log)
    if done:
        log(f"[translate] 日本語訳を {done} 件作成")
    if failed:
        log(f"[translate] 翻訳に失敗した候補が {failed} 件あります (原文表示のまま、次回実行で再試行)")
    return done, stats, failed


# --- ウォッチ・参考の一次情報の翻訳キュー ------------------------------------

# 対象は一次情報のみ (論文・治験・開示)。ニュースは訳さない (ノイズが多く、
# 読む価値は一次情報側にあるという判断。2026-11-06 泰崇、2026-11-11 に
# 再確認)。開示の yahoo / edgar は 2026-11-11 に追加した (どちらも一次
# 情報で、未訳プールも合計約14.5万字と小さい)。
# prtimes と edinet は原文が日本語なので対象外。
# 経路は Azure 主・Google 予備で、DeepL の一回限り枠は温存して使わない。
#
# 1実行の予算 (2026-11-11 泰崇の承認で、件数上限 50件/回から文字数予算へ
# 変更): Azure F0 の月枠 200万字から当月の使用済み (表示セット込み) を
# 引き、当月の残り実行回数で割った文字数をこの実行の上限とする
# (watch_run_budget)。月枠そのものが天井になるため月途中で枯渇せず、
# 月内の過不足は残り回数への割り直しで自動的に均される。件数の上限は
# 置かない (律速は文字数だけ)。
AZURE_MONTHLY_QUOTA = 2_000_000
# 予算の安全率 (2026-11-11 の決定で 0.8): Core・Conditional の評価材料の
# 翻訳はこの予算の外で同じ Azure 枠を使う (そちらが本命)。月内のその
# 消費は事前に読めないため、watch 側の予算に2割の余白を確保する。
WATCH_BUDGET_SAFETY_FACTOR = 0.8
WATCH_SOURCES = ("pubmed", "openalex", "arxiv", "trials", "yahoo", "edgar")
# ウォッチページの表示窓 (site._watch_companies と同じ定義・同じ並び)。
# site → pipeline → translate の import 方向の都合で import できないため
# 値を写し、site.py の WATCH_PRIMARY_CAP / WATCH_NEWS_SOURCES と揃える。
# 窓の外 (企業ごとの一次情報で 16件目以降) は訳しても表示されない。
WATCH_DISPLAY_CAP = 15
WATCH_NEWS_SOURCES = ("gnews", "prtimes")
# prune の paper 保持期限と同じ窓 (読みの実効窓 90日 ×2 = 180日。
# trl-db-cleanup/DESIGN.md §4、store.prune の N-2 と同じ導出)。この窓を
# 出た screened の paper は N-2 の有効化後に削除されるため、表示窓の
# 内側でも訳さない。
WATCH_PAPER_WINDOW_DAYS = 180


def _watch_todo(store, cfg, today):
    """キューの対象を優先順に並べる。

    優先順位は ウォッチ > 参考 > core の未訳分 (表示セットは先行フェーズの
    担当なので除く)。同じ層では企業ラウンドロビン (材料の多い企業が
    枠を独占しない)、企業内は新しい順。
    適格条件として、予算を使っても意味の無い材料を先に落とす:
    - ウォッチ・参考は、ウォッチページの表示窓 (企業ごとの一次情報の
      新しい順 15件) の内側だけ。窓の外は訳しても表示されない。
    - 全層共通で、prune の paper 保持窓 (180日) を出た screened の
      paper は落とす。N-2 の有効化後に削除される材料のため。"""
    skip = display_ids(store, cfg, today)
    tiers = {e["id"]: e.get("tier") or "core"
             for e in (cfg.get("entities") or {}).get("entities", [])}
    links = {}
    for cid, eid in store.conn.execute("SELECT candidate_id, entity_id FROM candidate_entities"):
        links.setdefault(cid, []).append(eid)
    # 表示窓の順位: watch/reference 企業ごとに、一次情報 (ニュース以外) を
    # site._watch_companies と同じ並び (published 新しい順・NULL は最後、
    # 同点は id 順) で数えた順位。yahoo / edgar は site 側でも一次情報に
    # 数えられるため、この順位にそのまま含まれる
    rank = {}
    seen = Counter()
    for eid, cid, source in store.conn.execute(
            "SELECT ce.entity_id, c.id, c.source FROM candidate_entities ce"
            " JOIN candidates c ON c.id = ce.candidate_id"
            " WHERE c.status != 'rejected'"
            " ORDER BY ce.entity_id, (c.published IS NULL), c.published DESC, c.id"):
        if tiers.get(eid) not in ("watch", "reference") or source in WATCH_NEWS_SOURCES:
            continue
        rank[(eid, cid)] = seen[eid]
        seen[eid] += 1
    paper_cut = (today - timedelta(days=WATCH_PAPER_WINDOW_DAYS)).isoformat()
    srcs = ",".join("?" * len(WATCH_SOURCES))
    rows = store.conn.execute(
        f"SELECT id FROM candidates WHERE source IN ({srcs}) AND status != 'rejected'"
        " AND (title_ja IS NULL OR abstract_ja IS NULL"
        " OR translation_src IS NULL OR translation_src = '{}')", WATCH_SOURCES).fetchall()
    groups = {}
    for (cid,) in rows:
        if cid in skip:
            continue
        linked = links.get(cid) or []
        if not linked:
            continue
        lt = [tiers.get(e, "core") for e in linked]
        if "core" in lt:
            bucket = 2
        elif "watch" in lt:
            bucket = 0
        elif "reference" in lt:
            bucket = 1
        else:
            continue
        c = store.get_candidate(cid)
        if not c:
            continue
        want_title, want_abs = _wants(c)
        if not (want_title or want_abs):
            continue
        # prune の paper 窓を出た材料は訳さない (削除予定の材料に予算を
        # 使わない)。保護対象 (評価付き・未解決レビュー) は prune でも
        # 消えず、評価付きは表示セット側の担当でここには来ない
        if (c.get("info_type") == "paper" and c.get("status") == "screened"
                and c.get("published") and c["published"] < paper_cut):
            continue
        # ウォッチ・参考は表示窓の内側だけ (リンク先企業のどれか1社でも
        # 窓の内側なら対象)
        if bucket in (0, 1):
            tier_name = "watch" if bucket == 0 else "reference"
            if not any(rank.get((e, cid), WATCH_DISPLAY_CAP) < WATCH_DISPLAY_CAP
                       for e in linked if tiers.get(e, "core") == tier_name):
                continue
        company = min(e for e in linked if tiers.get(e, "core") == ("watch" if bucket == 0 else "reference" if bucket == 1 else "core"))
        chars = (len(c["title"]) if want_title else 0) + (len(c["abstract"]) if want_abs else 0)
        groups.setdefault((bucket, company), []).append(
            (cid, c, want_title, want_abs, chars))
    for items in groups.values():
        items.sort(key=lambda x: (x[1].get("published") or "", x[0]), reverse=True)
    # 企業の並びは、その企業の最新材料が新しい順 (bucket 内)
    ordered = []
    for bucket in (0, 1, 2):
        companies = sorted((k for k in groups if k[0] == bucket),
                           key=lambda k: groups[k][0][1].get("published") or "", reverse=True)
        queues = [list(groups[k]) for k in companies]
        while any(queues):
            for q in queues:
                if q:
                    ordered.append(q.pop(0))
    return ordered


def _remaining_runs(today):
    """当月の残り実行回数。定期実行 (main.yaml の cron) は火〜金 06:30 JST
    で、今日の分を含む。手動実行の日は定期が無くても今日を1回と数える
    (この実行自体が枠を使うため)。"""
    n, d = 0, today
    while d.month == today.month:
        if d == today or d.weekday() in (1, 2, 3, 4):
            n += 1
        d += timedelta(days=1)
    return max(n, 1)


def watch_run_budget(store, today, spent_before=0):
    """この実行のウォッチキュー予算 (文字数)。

    Azure F0 の月枠 (AZURE_MONTHLY_QUOTA) から、当月の使用済み
    (azure+google の月次累計に、この実行で表示セットが既に使った分
    spent_before を加えたもの) を引き、当月の残り実行回数で割り、
    安全率 (WATCH_BUDGET_SAFETY_FACTOR) を掛ける。
    割り当ては残り枠の内側でしか行わないため月枠を超えることは無く、
    月内の過不足は残り回数への割り直しで自動的に均される。"""
    used = usage_totals(store, today)
    month_used = used.get("azure", 0) + used.get("google", 0) + spent_before
    rest = AZURE_MONTHLY_QUOTA - month_used
    if rest <= 0:
        return 0
    return int(rest // _remaining_runs(today) * WATCH_BUDGET_SAFETY_FACTOR)


def translate_watch_queue(store, cfg, today, log=print, http=http_fetch, spent_before=0):
    """ウォッチ・参考 (と残りの core) の一次情報の未訳分を予算内で訳す。

    -> (訳した件数, 段ごとの翻訳文字数, 失敗した件数)。予算は watch_run_budget
    の導出 (Azure の月枠 − 当月の使用済み) ÷ 当月の残り実行回数 × 安全率。
    表示セットの枠を食い潰さないよう、spent_before にこの実行の表示セット分を
    受け取る。"""
    chain = _backends(http, exclude={"deepl"})
    if not chain:
        return 0, {}, 0
    budget = watch_run_budget(store, today, spent_before)
    todo, spent = [], 0
    for item in _watch_todo(store, cfg, today):
        if spent + item[4] > budget:
            continue  # 予算に収まる小さい材料が後ろにあれば拾う
        spent += item[4]
        todo.append(item[:4])
    if not todo:
        return 0, {}, 0
    done, stats, failed = _run_batches(store, todo, chain, log)
    if done:
        log(f"[translate] ウォッチ・参考の一次情報の訳を {done} 件作成")
    if failed:
        log(f"[translate] ウォッチ・参考の翻訳に失敗した候補が {failed} 件あります (原文表示のまま、次回実行で再試行)")
    return done, stats, failed
