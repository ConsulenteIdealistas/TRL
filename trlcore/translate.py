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


def _backends(http):
    """使える翻訳経路を優先順に返す: [(名前, バッチ関数), ...]"""
    chain = []
    azure_key = os.environ.get("AZURE_TRANSLATOR_KEY")
    if azure_key:
        region = os.environ.get("AZURE_TRANSLATOR_REGION") or None
        chain.append(("azure", _mt_batch(lambda ts: azure_translate(ts, azure_key, region, http))))
    deepl_key = os.environ.get("DEEPL_API_KEY")
    if deepl_key:
        chain.append(("deepl", _mt_batch(lambda ts: deepl_translate(ts, deepl_key, http))))
    google_key = os.environ.get("GOOGLE_TRANSLATE_API_KEY")
    if google_key:
        chain.append(("google", _mt_batch(lambda ts: google_translate(ts, google_key, http))))
    return chain


def translate_display_set(store, cfg, today, log=print, http=http_fetch):
    """表示対象のうち未訳のものを訳して保存する。

    -> (訳した件数, 段ごとの翻訳文字数 {"azure": 字数, ...})。
    文字数は実際に送信して訳し終えた原文の字数 (課金・枠消費の単位)。
    """
    chain = _backends(http)
    if not chain:
        return 0, {}
    stats = {}
    todo = []
    for cid in sorted(display_ids(store, cfg, today)):
        c = store.get_candidate(cid)
        if not c:
            continue
        want_title = not c.get("title_ja") and needs_translation(c["title"])
        want_abs = not c.get("abstract_ja") and needs_translation(c["abstract"])
        if want_title or want_abs:
            todo.append((cid, c, want_title, want_abs))
        if len(todo) >= MAX_PER_RUN:
            break
    done = 0
    for s in range(0, len(todo), BATCH):
        chunk = todo[s:s + BATCH]
        items = [(c["title"], c["abstract"]) for _, c, _, _ in chunk]
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
    if done:
        log(f"[translate] 日本語訳を {done} 件作成")
    return done, stats
