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
    ids |= {c["id"] for c in select_for_eval(store, cfg, today, pending_limit)}
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

    -> (訳した件数, 段ごとの翻訳文字数)。失敗したバッチは原文表示に落ちる
    だけで、残りは続ける。"""
    stats = {}
    done = 0
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
    return done, stats


def translate_display_set(store, cfg, today, log=print, http=http_fetch):
    """表示対象のうち未訳のものを訳して保存する。

    -> (訳した件数, 段ごとの翻訳文字数 {"azure": 字数, ...})。
    文字数は実際に送信して訳し終えた原文の字数 (課金・枠消費の単位)。
    """
    chain = _backends(http)
    if not chain:
        return 0, {}
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
    done, stats = _run_batches(store, todo, chain, log)
    if done:
        log(f"[translate] 日本語訳を {done} 件作成")
    return done, stats


# --- ウォッチ・参考の一次情報の翻訳キュー ------------------------------------

# 対象は一次情報のみ (論文と治験)。ニュースは訳さない (ノイズが多く、
# 読む価値は一次情報側にあるという判断。2026-11-06 泰崇)。
# 経路は Azure 主・Google 予備で、DeepL の一回限り枠は温存して使わない。
# 上限と予算は 2026-11-06 の確定値: 1実行 50件、翻訳全体の月次予算 150万字
# (Azure F0 の月 200万字に対し、表示セットの余地を残すための天井)。
WATCH_MAX_PER_RUN = 50
WATCH_MONTHLY_BUDGET = 1_500_000
WATCH_SOURCES = ("pubmed", "openalex", "arxiv", "trials")


def _watch_todo(store, cfg, today):
    """キューの対象を優先順に並べる。

    優先順位は ウォッチ > 参考 > core の未訳分 (表示セットは先行フェーズの
    担当なので除く)。同じ層では企業ラウンドロビン (材料の多い企業が
    枠を独占しない)、企業内は新しい順。"""
    skip = display_ids(store, cfg, today)
    tiers = {e["id"]: e.get("tier") or "core"
             for e in (cfg.get("entities") or {}).get("entities", [])}
    links = {}
    for cid, eid in store.conn.execute("SELECT candidate_id, entity_id FROM candidate_entities"):
        links.setdefault(cid, []).append(eid)
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


def translate_watch_queue(store, cfg, today, log=print, http=http_fetch):
    """ウォッチ・参考 (と残りの core) の一次情報の未訳分を予算内で訳す。

    -> (訳した件数, 段ごとの翻訳文字数)。月次予算は翻訳全体 (azure+google
    の月次累計) に対して効かせ、表示セットの枠を食い潰さない。"""
    chain = _backends(http, exclude={"deepl"})
    if not chain:
        return 0, {}
    used = usage_totals(store, today)
    month_used = used.get("azure", 0) + used.get("google", 0)
    todo, spent = [], 0
    for item in _watch_todo(store, cfg, today):
        if len(todo) >= WATCH_MAX_PER_RUN:
            break
        if month_used + spent + item[4] > WATCH_MONTHLY_BUDGET:
            continue  # 予算に収まる小さい材料が後ろにあれば拾う
        spent += item[4]
        todo.append(item[:4])
    if not todo:
        return 0, {}
    done, stats = _run_batches(store, todo, chain, log)
    if done:
        log(f"[translate] ウォッチ・参考の一次情報の訳を {done} 件作成")
    return done, stats
