"""表示用タイトル・抄録の日本語訳。

サイトに載る候補(評価済み+次に評価される上位)だけを、評価と同じ LLM で邦訳して
DB に保存する。訳は1回作れば再利用されるため、実行ごとの追加コストは新規分だけ。
作成のオン/オフは scoring.json の llm.translate_enabled(1/0)で切り替える。
停止中も既存の訳はそのまま表示に使われる。
複数候補を1コールにまとめて訳し、翻訳の失敗はその分を原文表示に落とすだけで
実行全体は止めない。LLM が劣化状態(連続失敗等)の実行では翻訳自体を行わない。
"""
import json
import re

MAX_PER_RUN = 30  # 1回の実行で訳す候補数の上限(初回のバックフィル用。定常は新規分のみ)
BATCH = 5         # 1コールでまとめて訳す候補数

_CJK = re.compile(r"[぀-ヿ㐀-鿿ｱ-ﾝ]")


def needs_translation(text):
    """日本語らしい文は訳さない。英字を含む文だけを対象にする。"""
    t = (text or "").strip()
    if len(t) < 4:
        return False
    if not re.search(r"[A-Za-z]", t):
        return False
    return len(_CJK.findall(t)) / len(t) < 0.1


def _parse_batch(text):
    m = re.search(r"\[.*\]", text or "", re.S)
    if not m:
        return {}
    try:
        arr = json.loads(m.group(0))
    except ValueError:
        return {}
    out = {}
    for d in arr if isinstance(arr, list) else []:
        if not isinstance(d, dict) or not isinstance(d.get("i"), int):
            continue
        title = str(d.get("title_ja") or "").strip()
        abstract = str(d.get("abstract_ja") or "").strip()
        if title or abstract:
            out[d["i"]] = (title or None, abstract or None)
    return out


def translate_batch(llm, items):
    """items: [(title, abstract), ...] -> {index: (title_ja, abstract_ja)}"""
    lines = [
        "あなたは科学技術・産業分野の翻訳担当です。次の各資料のタイトルと抄録を、"
        "正確で自然な日本語に翻訳してください。固有名詞・数値・単位はそのまま保持し、"
        "訳せない専門用語は原語のまま残してください。説明や前置きは書かず、"
        '次の形式の JSON 配列だけを出力してください。\n'
        '[{"i": 0, "title_ja": "タイトルの翻訳", "abstract_ja": "抄録の翻訳"}, ...]\n'
    ]
    for i, (title, abstract) in enumerate(items):
        lines.append(f"\n--- 資料 {i} ---\nタイトル: {title or ''}\n抄録: {abstract or ''}")
    return _parse_batch(llm.complete("\n".join(lines)))


def display_ids(store, cfg, today, pending_limit=10):
    """サイトに載る候補の id 集合: 評価済み全件 + 次に評価される上位。"""
    ids = {r[0] for r in store.conn.execute("SELECT DISTINCT candidate_id FROM evaluations")}
    from .pipeline import select_for_eval  # 循環 import 回避のため遅延 import
    ids |= {c["id"] for c in select_for_eval(store, cfg, today, pending_limit)}
    return ids


def translate_display_set(store, cfg, llm, today, log=print):
    """表示対象のうち未訳のものを訳して保存する。-> 訳した件数。"""
    if llm is None:
        return 0
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
        try:
            got = translate_batch(llm, [(c["title"], c["abstract"]) for _, c, _, _ in chunk])
        except Exception as e:  # 翻訳の失敗で実行を止めない
            log(f"[translate] バッチ翻訳に失敗: {type(e).__name__}")
            continue
        for i, (cid, c, want_title, want_abs) in enumerate(chunk):
            if i not in got:
                continue
            t_ja, a_ja = got[i]
            store.set_translation(cid,
                                  t_ja if want_title else c.get("title_ja"),
                                  a_ja if want_abs else c.get("abstract_ja"))
            done += 1
    if done:
        log(f"[translate] 日本語訳を {done} 件作成")
    return done
