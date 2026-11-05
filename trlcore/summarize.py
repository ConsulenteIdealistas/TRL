"""論文抄録からの決定的な要点抽出 (抽出型要約、表示専用)。

LLM を使わず、節見出し・文内頻度・タイトルとの重なり・位置・数値の有無で
文を採点し、上位3文を原文順に返す。同じ入力からは常に同じ出力になるため
export 時に計算してよく、DB へ保存しない。

重みの論拠 (恣意性を避けるため明記する。いずれも表示にのみ効き、調整は
再計算で即反映される):
- TF (3): その抄録内で頻出する語を多く含む文。Luhn 以来の抽出型要約の基礎。
- 節ボーナス (3): RESULTS / CONCLUSIONS 節の文。抄録の構造上、要点はここにある。
- タイトル重なり (2): 題名の語を含む文は主題の中心に近い。
- 位置 (2): 冒頭文と末尾2文。抄録の型として冒頭=目的、末尾=結論に寄る。
- 数値 (2): 数字を含む文。結果文の特徴。
"""

import re
from collections import Counter

PAPER_SOURCES = frozenset({"pubmed", "openalex", "arxiv"})
MAX_SENTENCES = 3   # 抄録の文数中央値 9 (2026-11-06 実測) の約 1/3
MIN_SENTENCES = 5   # 4文以下は抄録自体が要点サイズなので付けない

_STOP = frozenset("""a an the and or of to in for on with by from as at is are was were be been being
this that these those it its we our they their he she his her not no than then so such into
using used use based can could may might will would should has have had do does did also
more most other others which who whom when where while during between among per via""".split())

_LABELS = ("BACKGROUND", "OBJECTIVE", "OBJECTIVES", "PURPOSE", "METHODS", "METHOD",
           "RESULTS", "CONCLUSIONS", "CONCLUSION", "DISCUSSION", "INTRODUCTION")
_GOOD_SECTIONS = frozenset({"RESULTS", "CONCLUSIONS", "CONCLUSION"})
_LABEL_RE = re.compile(r"\b(" + "|".join(_LABELS) + r")\s*:\s*", re.I)
_JA_RE = re.compile(r"[ぁ-んァ-ヶ一-龥]")


def _split_sections(text):
    """見出し付き抄録を (見出し, 本文) の列に。無ければ [(None, 全文)]。"""
    hits = list(_LABEL_RE.finditer(text))
    if len(hits) < 2:
        return [(None, text)]
    out = []
    for i, h in enumerate(hits):
        end = hits[i + 1].start() if i + 1 < len(hits) else len(text)
        out.append((h.group(1).upper(), text[h.end():end].strip()))
    return out


def _split_sentences(text, ja):
    if ja:
        t = re.sub(r"\s+", "", text or "").strip()
        return [p.strip() for p in re.split(r"(?<=[。！？])", t) if len(p.strip()) >= 10]
    t = re.sub(r"\s+", " ", text or "").strip()
    t = re.sub(r"\b(e\.g|i\.e|et al|Fig|Dr|Mr|vs)\.", r"\1@@", t)
    parts = re.split(r"(?<=[.!?])\s+(?=[A-Z0-9\"“])", t)
    return [p.replace("@@", ".").strip() for p in parts if len(p.strip()) >= 25]


def _tokens(text, ja):
    if ja:
        s = re.sub(r"[^0-9A-Za-zぁ-んァ-ヶ一-龥]", "", text)
        return [s[i:i + 2] for i in range(len(s) - 1)]
    return [w for w in re.findall(r"[a-z0-9][a-z0-9\-]{1,}", text.lower()) if w not in _STOP]


def extract_summary(title, abstract, abstract_ja=None):
    """抄録の要点 (最大3文を原文順に連結した文字列) か、対象外なら None。

    訳文があれば訳文から抽出する (日本語で読める分だけ、要点も日本語がよい)。
    """
    text = abstract_ja or abstract
    if not text:
        return None
    ja = bool(_JA_RE.search(text[:200])) and "。" in text
    sents = []
    for lab, body in _split_sections(text):
        for s in _split_sentences(body, ja):
            sents.append((s, lab))
    if len(sents) < MIN_SENTENCES:
        return None
    tf = Counter()
    sent_tokens = []
    for s, _lab in sents:
        tk = _tokens(s, ja)
        sent_tokens.append(tk)
        tf.update(set(tk))
    title_tk = set(_tokens(title or "", ja))
    n = len(sents)
    scored = []
    for i, ((s, lab), tk) in enumerate(zip(sents, sent_tokens)):
        if not tk:
            continue
        uniq = set(tk)
        tf_sc = sum(tf[w] for w in uniq) / len(uniq)
        ti_sc = (len(uniq & title_tk) / len(title_tk)) if title_tk else 0.0
        pos = 1.0 if i == 0 else (0.7 if i >= n - 2 else 0.0)
        num = 1.0 if re.search(r"\d", s) else 0.0
        sec = 1.0 if lab in _GOOD_SECTIONS else 0.0
        scored.append((3 * tf_sc + 2 * ti_sc + 2 * pos + 2 * num + 3 * sec, i, s))
    top = sorted(scored, reverse=True)[:MAX_SENTENCES]
    picked = [s for _sc, _i, s in sorted(top, key=lambda x: x[1])]
    return ("" if ja else " ").join(picked)
