"use strict";
/* 監査レビューページ (段階A): data.json の audit (最新の long 評価に紐づく
   監査結果) を読み、要確認/グレーへの処分・再評価の予約・TRL の読み替え
   (調整) を端末内 (localStorage: trl-review) に記録する。
   DB への反映は段階B で review_inbox.json 経由 (パイプライン開始時に
   取り込み) になるため、このページは記録と「反映用 JSON」の書き出しまでを
   担う。元の評価・監査の記録はここでは一切変更しない (非破壊)。
   取り込まれた調整は詳細ページの通常表示に「調整後の値 (元: ○)」の
   併記として出る。処分・予約・調整の DB 側の状態は data.json の export
   経由で戻ってくる (監査ブロックの decisions、候補の reeval_booked、
   long.adjustment) ため、起動時にそれを「反映済み」の種として端末内の
   未反映分と重ね合わせ、どの端末で開いても同じ状態が見えるようにする
   (DB が正で、端末内の記録は未反映の変更の層)。 */
const $ = s => document.querySelector(s);
const esc = v => String(v ?? "").replace(/[&<>"']/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
const jst = iso => { try { return new Date(iso).toLocaleString("ja-JP", {timeZone: "Asia/Tokyo", hour12: false}); } catch (e) { return iso; } };
const ok = t => Number.isInteger(t) && t >= 1 && t <= 9;
const SL = {final: "選定可能", review: "要追加調査", hold: "保留", pending: "未評価", error: "エラー", rejected: "除外"};
const PL = {rules: "規則監査", api: "API照合"};  // 監査 provider の表示名 (未知の provider は名前をそのまま出す)
const CL = {space: "宇宙", bio: "バイオ", semiconductor: "半導体", ai: "AI", robotics: "ロボティクス", energy: "エネルギー・蓄電池", quantum: "量子技術", defense: "防衛"};
const IL = {technology_readiness: "技術成熟度", technological_advantage: "技術的優位性", monetization: "収益化能力",
  market_growth: "市場成長性", financial_sustainability: "財務的持続可能性", moat_durability: "競争優位性の持続性",
  management_execution: "経営陣・実行力",
  trl_level: "TRL判定", counter_points: "反証", candidate: "候補の時点", total: "総合点", companies: "企業帰属"};
/* チェック名の正本は export の監査カタログ (data.json の audit_catalog、
   監査側 trlcore/audit.py の CHECK_CATALOG)。起動時にカタログがあれば
   そちらで上書きする。ここの静的定義はカタログが無い古い data.json 用の
   予備で、以前 A1 が実装 (原則2) と食い違う転記ミスがあったため、
   カタログ側を正とする */
const CHK = {A1: "原則2: 件数・期間の推測", A2: "原則2: TRL 9 の継続証拠", A3: "原則3: TRLからの推論",
  A4: "原則1: TRL 9 の根拠が商用展開のみ", A5: "点と理由の整合", A6: "欠落判定（材料プール外）", A7: "減点理由の具体性",
  A8: "反証の欠落", A9: "根拠のない高得点", A10: "時点の不整合（公開日が基準日より後）", A11: "数値の原文照合",
  A12: "出典リンクの形式", A13: "総合点の再導出照合", A14: "企業帰属（弱いリンクのみ）",
  A15: "上限の観測", A16: "TRLレベルと点数帯域の乖離", A17: "単一出所の高得点",
  E1: "価格反応と未織り込みの矛盾", E2: "反応データ未使用の保留",
  P1: "TRL 9 到達の可能性", P2: "保留のままの豊富な事実", P3: "根拠が厚いのに低得点",
  P4: "未採点なのに根拠あり", P5: "企業項目の未採点", P6: "反応がないのに織り込み済み",
  CR1: "DOI の実在確認", CT1: "治験ステータス（中止系）の反証", CT2: "治験スポンサーの帰属"};
const DISP = [["confirmed", "問題あり"], ["false_positive", "誤検知"], ["watch", "様子見"]];
// 導出 tier (tiers/v1) の表示名 (W-9: 監査カードの企業名に併記するだけ)
const DTL = {core: "Core", conditional: "Conditional", look: "Look", see: "See"};
const dtLabelA = e => DTL[e.derived_tier || (e.tier === "watch" ? "look" : e.tier === "reference" ? "see" : "core")] || "";

const RKEY = "trl-review";
let review = loadReview();
/* DB 側の状態のうち、この端末が最後に見た「反映済み」の姿 (data.json の
   export から組み立てる)。起動時の重ね合わせと、反映用 JSON で
   「未反映分だけを書き出す」ための比較に使う */
let SERVER = {decisions: {}, reeval: {}, adjustments: {}};
function loadReview() {
  try {
    const r = JSON.parse(localStorage.getItem(RKEY) || "null");
    if (r && typeof r === "object") {
      return {decisions: r.decisions || {}, reeval: r.reeval || {}, adjustments: r.adjustments || {}};
    }
  } catch (e) { /* 保存が読めなければ空から始める */ }
  return {decisions: {}, reeval: {}, adjustments: {}};
}
function saveReview() { try { localStorage.setItem(RKEY, JSON.stringify(review)); } catch (e) { /* 保存不可でも表示は続ける */ } }
function newId() {
  try { if (window.crypto && crypto.randomUUID) return crypto.randomUUID(); } catch (e) { /* 下の代替生成へ */ }
  return "r-" + Date.now().toString(36) + "-" + Math.random().toString(36).slice(2, 12) + Math.random().toString(36).slice(2, 12);
}
/* 監査エントリの識別子は evaluation_id + check + item (段階B の取り込みも同じキーで照合する) */
const entryKey = (a, e) => `${a.evaluation_id}:${e.check || ""}:${e.item || ""}`;
const decisionOf = (a, e) => review.decisions[entryKey(a, e)] || null;
const adjDraft = {};  // 調整の下書き (未設定の選択・理由)。永続化は設定時に行う
/* 調整の有効判定: 取り消しは記録を消さず「to_value = 元値 (from_value)」の
   形で残す (設計書3節の統一形)。inbox の取り込み (段階B) は追記専用で、
   行の不在は「未送信」と区別できないため、消してしまうと DB 側へ取り消しが
   伝わらない。有効な調整として数えるのは to_value が元値と異なるものだけ */
const activeAdj = eid => { const a = review.adjustments[eid]; return a && a.to_value !== a.from_value ? a : null; };

/* 反映用 JSON は「未反映分だけ」を書き出す。すでに DB にある記録
   (SERVER と同じ内容) を再送しても取り込み側で重複として弾かれるが、
   件数表示が膨らんで「まだ反映されていない」と誤解されるため、ここで
   落とす。処分の取り消しは、反映済みの記録を消す操作なので墓標
   (disposition: "cleared") として書き出す (取り込み側の最新行として
   効き、export と tier 導出の両方で未処分に戻る) */
function buildInbox() {
  const skey = r => `${r.evaluation_id}:${r.check || ""}:${r.item || ""}`;
  const decisions = Object.values(review.decisions).filter(r => {
    if (!r) return false;
    const s = SERVER.decisions[skey(r)];
    if (!s) return !!r.disposition;               // 端末だけの記録: 処分がある分だけ
    if (r.id !== s.id) return !!r.disposition;    // 別端末の記録と違う判断 (新しい方)
    // 同じ記録: 内容が変わっていれば未反映の変更 (取り消しは cleared で送る)
    return r.disposition !== s.disposition || (r.note || "") !== (s.note || "");
  }).map(r => ({
    id: r.id, evaluation_id: r.evaluation_id, check: r.check, item: r.item,
    auditor: r.auditor || "", disposition: r.disposition || "cleared", note: r.note || "",
    decided_at: r.updated_at || ""}));
  // 予約は DB のキューに未完了の行があれば書き出さない (二重投入は
  // 取り込み側でも弾かれるが、未反映分の件数に混ぜないため)
  const reeval_queue = Object.values(review.reeval)
    .filter(r => r && r.on && !SERVER.reeval[r.candidate_id]).map(r => ({
      id: r.id, candidate_id: r.candidate_id}));
  const adjustments = Object.values(review.adjustments).filter(r => {
    if (!r) return false;
    const s = SERVER.adjustments[r.evaluation_id];
    if (!s) return true;
    if (r.id !== s.id) return true;
    return r.to_value !== s.to_value || (r.reason || "") !== (s.reason || "");
  }).map(r => ({
    id: r.id, evaluation_id: r.evaluation_id, field: "trl_level",
    from_value: r.from_value, to_value: r.to_value, reason: r.reason || ""}));
  return {version: 1, exported_at: new Date().toISOString(), decisions, reeval_queue, adjustments};
}

/* data.json の export から DB 側 (反映済み) のレビュー状態を組み立てる。
   処分は監査ブロックの decisions、予約は候補の reeval_booked、調整は
   long.adjustment (段階B の adjusted_view、同梱は有効な調整だけ) が運ぶ */
function serverReviewState() {
  const s = {decisions: {}, reeval: {}, adjustments: {}};
  for (const p of (DATA && DATA.candidates) || []) {
    for (const a of [...(p.audit || []), ...(p.audit_event || [])]) {
      for (const d of a.decisions || []) {
        s.decisions[`${a.evaluation_id}:${d.check || ""}:${d.item || ""}`] = {
          id: d.source_id || "", evaluation_id: a.evaluation_id, check: d.check || "",
          item: d.item || "", auditor: d.auditor || "", disposition: d.disposition,
          note: d.note || "", updated_at: d.decided_at || "", origin: "server"};
      }
    }
    if (p.reeval_booked) s.reeval[p.id] = {id: "", candidate_id: p.id, on: true, origin: "server"};
    const adj = p.long && p.long.adjustment;
    const eid = (p.audit && p.audit[0] && p.audit[0].evaluation_id) || null;
    if (adj && eid) s.adjustments[eid] = {
      id: adj.source_id || "", evaluation_id: eid, field: "trl_level",
      from_value: adj.from_value, to_value: adj.to_value, reason: adj.reason || "",
      updated_at: adj.adjusted_at || "", origin: "server"};
  }
  return s;
}

/* 反映済み (サーバ) と端末内の記録の重ね合わせ。処分・調整は記録単位で:
   同じ id なら内容が違う方 (= 端末側の新しい編集) を、違う id なら決めた
   時刻が新しい方を採る (同点はサーバ側 = 共有された記録を優先する) */
function mergeDecisions(server, local) {
  const out = {...server};
  for (const [key, l] of Object.entries(local || {})) {
    if (!l) continue;
    const s = server[key];
    if (!s) {
      // 端末だけの記録はそのまま残す。ただし「サーバ由来で取り消し済み」
      // の残骸 (墓標が取り込まれてサーバ側から消えた記録) は役目を終えた
      if (!(l.origin === "server" && !l.disposition)) out[key] = l;
      continue;
    }
    if (l.id && l.id === s.id) {
      out[key] = (l.disposition === s.disposition && (l.note || "") === (s.note || "")) ? s : l;
    } else {
      out[key] = String(l.updated_at || "") > String(s.updated_at || "") ? l : s;
    }
  }
  return out;
}
function mergeAdjustments(server, local) {
  const out = {...server};
  for (const [key, l] of Object.entries(local || {})) {
    if (!l) continue;
    const s = server[key];
    if (!s) { out[key] = l; continue; }
    if (l.id && l.id === s.id) {
      out[key] = (l.to_value === s.to_value && (l.reason || "") === (s.reason || "")) ? s : l;
    } else {
      out[key] = String(l.updated_at || "") > String(s.updated_at || "") ? l : s;
    }
  }
  return out;
}
function mergeReeval(server, local) {
  const out = {...server};
  for (const [cid, l] of Object.entries(local || {})) {
    if (!l) continue;
    if (!server[cid]) { out[cid] = l; continue; }
    // サーバに予約がある場合、端末側の新しい記録があればそちらを採る
    // (予約済み同士なら表示は同じ)。端末で解除した記録 (on: false) は
    // この端末の表示だけに効く: キューの行は追記専用で、解除は DB へは
    // 伝わらない (処分の取り消しと違い、予約には墓標が無い)
    if (l.origin !== "server") out[cid] = l;
  }
  return out;
}
function mergeServerState() {
  SERVER = serverReviewState();
  review = {decisions: mergeDecisions(SERVER.decisions, review.decisions),
            reeval: mergeReeval(SERVER.reeval, review.reeval),
            adjustments: mergeAdjustments(SERVER.adjustments, review.adjustments)};
  saveReview();
}

async function copyText(t) {
  if (navigator.clipboard && window.isSecureContext) {
    try { await navigator.clipboard.writeText(t); return true; } catch (e) { /* 代替手段へ */ }
  }
  const ta = document.createElement("textarea");
  ta.value = t; ta.setAttribute("readonly", ""); ta.style.position = "fixed"; ta.style.opacity = "0";
  document.body.appendChild(ta); ta.select();
  let done = false;
  try { done = document.execCommand("copy"); } catch (e) { done = false; }
  ta.remove(); return done;
}

let DATA = null;
const blocksOf = (p, k) => ((k === "event" ? p.audit_event : p.audit) || []);
const evOf = (p, k) => (k === "event" ? p.event : p.long);

/* 表示の畳み方 (ユーザー決定): 既定で前面に出すのは「系統ごとに最新の
   監査バッチ」×「未処分の項目」だけにする。過去バッチ (旧版の監査結果) と
   処分済みの項目は、是正済みの誤りも含めて見やすい場所に置くとノイズに
   なるため、スイッチを入れたときだけ展開する。data.json・DB の記録は
   一切変えず、表示だけの切り替え。スイッチは localStorage で保持する */
const PASTKEY = "trl-audit-past", DONEKEY = "trl-audit-done";
let showPast = false, showDone = false;
try {
  showPast = localStorage.getItem(PASTKEY) === "1";
  showDone = localStorage.getItem(DONEKEY) === "1";
} catch (e) { /* 保存不可なら既定 (両方 OFF) のまま */ }
function saveVis() {
  try {
    localStorage.setItem(PASTKEY, showPast ? "1" : "0");
    localStorage.setItem(DONEKEY, showDone ? "1" : "0");
  } catch (e) { /* 保存できなくても切替は効く */ }
}
/* 系統 (評価 × provider) ごとに最新の監査バッチだけを返す。新旧は
   created_at で決め、同点は配列の後方 (= 後から積まれた方) を最新とする */
function latestBlocks(blocks) {
  const best = new Map();
  for (const a of blocks || []) {
    const key = `${a.evaluation_id}:${a.provider || ""}`;
    const cur = best.get(key);
    if (!cur || String(a.created_at || "") >= String(cur.created_at || "")) best.set(key, a);
  }
  return [...best.values()];
}
const entryShown = (a, e) => {
  if (showDone) return true;
  const d = decisionOf(a, e);
  return !(d && d.disposition);
};
/* この候補 × 種別で「今の畳み方で見える」監査ブロックを決める。
   バッチ軸: 過去スイッチ OFF なら系統ごとの最新バッチだけ。
   項目軸: 項目が全部畳まれるブロックは出さない (何も挙がらなかった
   ブロックは従来どおり出す)。latestSet は過去ブロックの判別用 */
function shownBlocks(p, k) {
  const all = blocksOf(p, k);
  const lat = new Set(latestBlocks(all));
  const batch = showPast ? all : all.filter(a => lat.has(a));
  const blocks = batch.filter(a => !(a.entries || []).length || a.entries.some(e => entryShown(a, e)));
  return {blocks, latestSet: lat};
}
/* 畳んでいる分の件数 (スイッチのラベルに別数で示す。表示中集合の統計・
   チップには混ぜない)。過去はブロック数、処分済みは項目数で数える。
   処分済みは「バッチ軸で見えている範囲」の中での件数 (過去バッチの中の
   処分済みは過去側の件数にだけ現れ、二重に数えない) */
function hiddenCounts() {
  const ks = viewState.kind === "all" ? ["long", "event"] : [viewState.kind];
  let past = 0, doneN = 0;
  for (const p of DATA.candidates || []) for (const k of ks) {
    const all = blocksOf(p, k);
    if (!all.length) continue;
    const lat = new Set(latestBlocks(all));
    past += all.filter(a => !lat.has(a)).length;
    const batch = showPast ? all : all.filter(a => lat.has(a));
    for (const a of batch) for (const e of a.entries || []) {
      const d = decisionOf(a, e);
      if (d && d.disposition) doneN++;
    }
  }
  return {past, doneN};
}
/* 表示単位は「候補 × 種別」のカード。種別が all (両方) のときは長期・
   イベントの両方のカードが並び、両方の監査がある候補は2枚になる。
   件数の単位は常にこのカードで、統計枠・チップ・一覧で揃える */
function cards() {
  const ks = viewState.kind === "all" ? ["long", "event"] : [viewState.kind];
  const out = [];
  for (const p of DATA.candidates || []) for (const k of ks) {
    if (shownBlocks(p, k).blocks.length) out.push({p, kind: k});
  }
  return out;
}
/* 畳み方を無視したカード数 (空表示の文言の切り分け用) */
function rawCardCount() {
  const ks = viewState.kind === "all" ? ["long", "event"] : [viewState.kind];
  let n = 0;
  for (const p of DATA.candidates || []) for (const k of ks) {
    if (blocksOf(p, k).length) n++;
  }
  return n;
}

/* カードの状態 (絞り込みとバッジ用)。要確認/グレーは「未処分が残っている」
   もの、処分済みは「全エントリ処分済み」のものとして数える */
const viewState = {filter: "all", sort: "default", kind: "long"};
const SFILTERS = [["all", "すべて"], ["flag", "要確認"], ["gray", "グレー"],
  ["done", "処分済み"], ["dconfirmed", "問題あり"], ["dfalse", "誤検知"],
  ["dwatch", "様子見"], ["reserved", "再評価予約"], ["adjusted", "調整済み"]];
function cardState(c) {
  const blocks = shownBlocks(c.p, c.kind).blocks;
  let flagOpen = 0, grayOpen = 0, total = 0, decided = 0;
  const disp = {confirmed: 0, false_positive: 0, watch: 0};
  for (const a of blocks) for (const e of a.entries) {
    if (!entryShown(a, e)) continue;
    total++;
    const d = decisionOf(a, e);
    if (d && d.disposition) { decided++; if (d.disposition in disp) disp[d.disposition]++; }
    else if (e.verdict === "gray") grayOpen++; else flagOpen++;
  }
  const evalId = blocks.length ? blocks[0].evaluation_id : blocksOf(c.p, c.kind)[0].evaluation_id;
  return {flagOpen, grayOpen, done: total > 0 && decided === total, disp,
          // 予約は候補単位の記録だが、両方表示では長期カードにだけ付ける
          // (イベントカードへ重ねると同じ予約を二重に数えるため)。
          // 単独表示での数え方は従来どおり。調整は長期評価だけの概念で、
          // イベント側の評価 ID では引けないため自然に付かない
          reserved: !!(review.reeval[c.p.id] && review.reeval[c.p.id].on)
                    && (c.kind === "long" || viewState.kind !== "all"),
          adjusted: !!activeAdj(evalId)};
}
function applyView(list) {
  const f = viewState.filter;
  const out = list.filter(c => {
    if (f === "all") return true;
    const st = cardState(c);
    return f === "flag" ? st.flagOpen > 0 : f === "gray" ? st.grayOpen > 0 :
           f === "done" ? st.done :
           f === "dconfirmed" ? st.disp.confirmed > 0 :
           f === "dfalse" ? st.disp.false_positive > 0 :
           f === "dwatch" ? st.disp.watch > 0 :
           f === "reserved" ? st.reserved : st.adjusted;
  });
  const trlOf = c => { const lv = c.kind === "long" && c.p.long && c.p.long.trl_level ? c.p.long.trl_level.level : null; return ok(lv) ? lv : null; };
  const dateOf = c => { const L = evOf(c.p, c.kind); return (L && L.evaluated_at) || ""; };
  if (viewState.sort === "trl-desc") out.sort((a, b) => (trlOf(b) ?? -1) - (trlOf(a) ?? -1));
  else if (viewState.sort === "trl-asc") out.sort((a, b) => (trlOf(a) ?? 99) - (trlOf(b) ?? 99));
  else if (viewState.sort === "new") out.sort((a, b) => dateOf(b).localeCompare(dateOf(a)));
  else if (viewState.sort === "old") out.sort((a, b) => dateOf(a).localeCompare(dateOf(b)));
  else if (viewState.sort === "kind") out.sort((a, b) =>
    ((a.kind === "event") - (b.kind === "event")) || dateOf(b).localeCompare(dateOf(a)));
  return out;
}
function renderControls() {
  const counts = {all: 0, flag: 0, gray: 0, done: 0, dconfirmed: 0, dfalse: 0,
                  dwatch: 0, reserved: 0, adjusted: 0};
  for (const c of cards()) {
    counts.all++;
    const st = cardState(c);
    if (st.flagOpen) counts.flag++;
    if (st.grayOpen) counts.gray++;
    if (st.done) counts.done++;
    if (st.disp.confirmed) counts.dconfirmed++;
    if (st.disp.false_positive) counts.dfalse++;
    if (st.disp.watch) counts.dwatch++;
    if (st.reserved) counts.reserved++;
    if (st.adjusted) counts.adjusted++;
  }
  // 種別チップの件数もカード単位 (今の畳み方で見えるカードの数):
  // 「両方」は長期カード+イベントカードの総数
  const kindCounts = {long: 0, event: 0};
  for (const p of DATA.candidates || []) for (const k of ["long", "event"]) {
    if (shownBlocks(p, k).blocks.length) kindCounts[k]++;
  }
  kindCounts.all = kindCounts.long + kindCounts.event;
  // 畳み方スイッチの状態と、畳んでいる分の件数 (別数表示)
  const hc = hiddenCounts();
  const swp = $("#swPast"), swd = $("#swDone");
  if (swp) swp.checked = showPast;
  if (swd) swd.checked = showDone;
  const pc = $("#pastCnt"), dc = $("#doneCnt");
  if (pc) pc.textContent = `（${hc.past}件）`;
  if (dc) dc.textContent = `（${hc.doneN}件）`;
  const kf = $("#kindFilters");
  if (kf) kf.innerHTML = [["long", "長期評価"], ["event", "イベント評価"], ["all", "両方"]].map(([k, label]) =>
    `<button type="button" class="fchip" data-kind="${k}" aria-pressed="${viewState.kind === k ? "true" : "false"}">${label} ${kindCounts[k]}</button>`).join("");
  $("#stateFilters").innerHTML = SFILTERS.map(([v, label]) =>
    `<button type="button" class="fchip" data-sfilter="${v}" aria-pressed="${viewState.filter === v ? "true" : "false"}">${label} ${counts[v]}</button>`).join("");
  const sel = $("#sortSel");
  if (sel) sel.value = viewState.sort;
}

function renderCov() {
  // 枠の数字は絞り込みチップと必ず一致させる: すべて「評価 (カード) の数」
  // で、チップと同じ cardState の数え方で数える。要確認/グレーは未処分が
  // 残っているカードだけ (処分済みのエントリは数えない。ISSUES-2 の O-3:
  // 以前は処分済みも含めて数え、チップと食い違っていた)。処分済み =
  // 全エントリを処分した評価、問題あり等 = その処分を含む評価。再評価の
  // 予約/調整済みも localStorage 全体でなく表示カード単位で数え、チップと
  // 母数を揃える。両方表示でも単位はカード (候補×種別) のまま数える
  let flagN = 0, grayN = 0, done = 0, reev = 0, adj = 0;
  const byDisp = {confirmed: 0, false_positive: 0, watch: 0};
  for (const c of cards()) {
    const st = cardState(c);
    if (st.flagOpen) flagN++;
    if (st.grayOpen) grayN++;
    if (st.done) done++;
    if (st.reserved) reev++;
    if (st.adjusted) adj++;
    for (const k of Object.keys(byDisp)) if (st.disp[k]) byDisp[k]++;
  }
  $("#cov").innerHTML =
    `<div class="stat y"><small>要確認のある評価</small><strong>${flagN}</strong><em>件</em></div>` +
    `<div class="stat x"><small>グレーのある評価</small><strong>${grayN}</strong><em>件</em></div>` +
    `<div class="stat v"><small>処分済み</small><strong>${done}</strong><em>件の評価</em></div>` +
    `<div class="stat r"><small>問題あり</small><strong>${byDisp.confirmed}</strong><em>件の評価</em></div>` +
    `<div class="stat g"><small>誤検知</small><strong>${byDisp.false_positive}</strong><em>件の評価</em></div>` +
    `<div class="stat o"><small>様子見</small><strong>${byDisp.watch}</strong><em>件の評価</em></div>` +
    `<div class="stat b"><small>再評価の予約</small><strong>${reev}</strong><em>件</em></div>` +
    `<div class="stat pk"><small>調整済み</small><strong>${adj}</strong><em>件</em></div>`;
}

function entryHtml(a, e) {
  const key = entryKey(a, e);
  const d = decisionOf(a, e);
  const chips = DISP.map(([v, label]) =>
    `<button type="button" class="dchip" data-disp="${v}" data-key="${esc(key)}" data-eid="${a.evaluation_id}" data-check="${esc(e.check || "")}" data-item="${esc(e.item || "")}" data-auditor="${esc(a.auditor || "")}" aria-pressed="${d && d.disposition === v ? "true" : "false"}">${label}</button>`).join("");
  return `<div class="eentry">
    <div class="erow"><span class="chk">${esc(e.check || "")}</span><span class="vb ${e.verdict === "gray" ? "gray" : "flag"}">${e.verdict === "gray" ? "グレー" : "要確認"}</span><span class="eitem">${esc(IL[e.item] || e.item || "")}</span><span class="hint">${esc(CHK[e.check] || "")}</span></div>
    <p class="edetail">${esc(e.detail || "")}</p>
    ${e.rule ? `<div class="erule">規則: ${esc(e.rule)}</div>` : ""}
    <div class="edisp">${chips}<input class="tin" data-dnote="${esc(key)}" placeholder="メモ（任意）" value="${esc(d ? d.note || "" : "")}"></div>
  </div>`;
}

function auditBlockHtml(a, past) {
  const who = PL[a.provider] || a.provider || "";
  const head = `${who} ${esc(a.auditor || "")}${a.version ? `（版 ${esc(a.version)}）` : ""}`;
  const shown = (a.entries || []).filter(e => entryShown(a, e));
  const entries = (a.entries || []).length ? shown.map(e => entryHtml(a, e)).join("")
    : `<p class="hint" style="margin-top:8px">この監査では何も挙がりませんでした。</p>`;
  return `<div class="ablk${past ? " past" : ""}">
    <div class="ablkhead"><span>${head}</span>${past ? `<span class="sbadge pastb">過去の監査</span>` : ""}<span class="hint">${a.created_at ? jst(a.created_at) : ""} ・ 評価 #${a.evaluation_id}</span></div>
    ${a.summary ? `<p class="asum">${esc(a.summary)}</p>` : ""}
    ${entries}
  </div>`;
}

function adjustHtml(p, evalId) {
  const lv = p.long && p.long.trl_level ? p.long.trl_level.level : null;
  if (!ok(lv)) return `<div><strong style="font-size:13px">TRL 調整（読み替え）</strong><p class="hint" style="margin-top:4px">TRL が未確定の評価には読み替えを設定できません。</p></div>`;
  const rec = review.adjustments[evalId] || null;
  const adj = rec && rec.to_value !== rec.from_value ? rec : null;
  const draft = adjDraft[evalId] || {};
  const selVal = adj ? adj.to_value : (draft.to ?? "");
  const reason = adj ? (adj.reason || "") : (draft.reason ?? (rec ? rec.reason || "" : ""));
  const opts = `<option value="">読み替え先…</option>` + Array.from({length: 9}, (_, i) => i + 1)
    .map(n => `<option value="${n}" ${String(selVal) === String(n) ? "selected" : ""}>TRL ${n}</option>`).join("");
  return `<div>
    <div class="adjrow"><strong style="font-size:13px">TRL 調整（読み替え）</strong>${adj ? `<span class="adjnow">調整: TRL ${adj.from_value} → ${adj.to_value}</span>` : ""}${rec && !adj ? `<span class="hint">取り消し済み（反映用 JSON で DB 側の調整も取り消されます）</span>` : ""}</div>
    <div class="adjrow" style="margin-top:6px">
      <select class="tsel" data-adjsel="${evalId}">${opts}</select>
      <input class="tin" data-adjreason="${evalId}" placeholder="調整理由（必須）" value="${esc(reason)}">
      <button type="button" class="btn quiet" data-adjset="${evalId}" data-from="${lv}">設定</button>
      ${adj ? `<button type="button" class="btn quiet" data-adjclear="${evalId}">取り消す</button>` : ""}
    </div>
    <p class="hint" style="margin-top:4px">読み替えを入れると、総合点・帯域は採点の計算で機械的に導き直されます（段階B）。項目点の手直しはしません。</p>
  </div>`;
}

function cardHtml(c) {
  const p = c.p, k = c.kind;
  const L = evOf(p, k);
  const title = p.title_ja || p.title;
  const entNames = (p.entities || []).map(e => e.name).filter(Boolean).join("、");
  const sectors = [...new Set((p.entities || []).map(e => CL[e.sector] || e.sector).filter(Boolean))].join("・");
  const bits = [];
  if (entNames) bits.push(esc(entNames));
  const tierLbls = [...new Set((p.entities || []).map(dtLabelA).filter(Boolean))];
  if (tierLbls.length) bits.push(esc(tierLbls.join(" / ")));
  if (sectors) bits.push(esc(sectors));
  if (p.title_ja && p.title && p.title !== p.title_ja) bits.push(esc(p.title));
  const ev = [];
  if (L) {
    if (L.total !== null && L.total !== undefined) ev.push(`総合点 ${esc(L.total)}${L.band ? `（${esc(L.band)}）` : ""}`);
    if (k === "long") {
      const lv = L.trl_level ? L.trl_level.level : null;
      ev.push(`TRL ${ok(lv) ? lv : "未確定"}`);
    }
    if (L.llm_model) ev.push(`評価: ${esc(L.llm_model)}`);
    if (L.evaluated_at) ev.push(jst(L.evaluated_at));
  }
  const shown = shownBlocks(p, k);
  const evalId = shown.blocks.length ? shown.blocks[0].evaluation_id : blocksOf(p, k)[0].evaluation_id;
  const stKey = k === "event" && L ? L.status : p.display_status;
  const resv = review.reeval[p.id];
  const resvOn = !!(resv && resv.on);
  // 予約ボタンも両方表示では長期カードにだけ出す (状態の付け方と揃える)
  const showResv = k === "long" || viewState.kind !== "all";
  // 両方表示で同じ候補のカードが2枚あるときはイベント側の id を分ける
  return `<section class="panel acard" id="card-${esc(p.id)}${viewState.kind === "all" && k === "event" ? "--event" : ""}">
    <div class="ahead">
      <div style="min-width:0">
        <a class="atitle" href="detail.html?id=${encodeURIComponent(p.id)}">${esc(title)}</a>
        ${bits.length ? `<div class="ameta">${bits.join(" ・ ")}</div>` : ""}
      </div>
      <span class="st st-${esc(stKey || "")}">${esc(SL[stKey] || stKey || "")}</span>
    </div>
    ${ev.length ? `<div class="ameta">元評価: ${ev.join(" ・ ")}</div>` : ""}
    ${(() => { const st = cardState(c); const b = [];
      if (k === "event" && viewState.kind !== "long") b.push(`<span class="sbadge evk">イベント評価</span>`);
      if (k === "long" && viewState.kind === "all") b.push(`<span class="sbadge lngk">長期評価</span>`);
      if (st.flagOpen) b.push(`<span class="sbadge flag">要確認 ${st.flagOpen}</span>`);
      if (st.grayOpen) b.push(`<span class="sbadge gray">グレー ${st.grayOpen}</span>`);
      if (st.done) b.push(`<span class="sbadge done">処分済み</span>`);
      if (st.reserved) b.push(`<span class="sbadge resv">再評価予約</span>`);
      if (st.adjusted) b.push(`<span class="sbadge adj">調整済み</span>`);
      return b.length ? `<div class="srow">${b.join("")}</div>` : ""; })()}
    <div class="ameta"><a class="tg" href="detail.html?id=${encodeURIComponent(p.id)}">記事（詳細ページ）を見る →</a></div>
    ${shown.blocks.map(a => auditBlockHtml(a, !shown.latestSet.has(a))).join("")}
    ${showResv || k === "long" ? `<div class="aops">
      ${showResv ? `<div class="adjrow">
        <button type="button" class="btn quiet resv" data-reeval="${esc(p.id)}" aria-pressed="${resvOn ? "true" : "false"}">${resvOn ? "再評価: 予約済み（タップで解除）" : "再評価を予約する"}</button>
        <span class="hint">処分で「問題あり」にした評価の作り直しは、再評価（経路A）で行います。</span>
      </div>` : ""}
      ${k === "long" ? adjustHtml(p, evalId) : ""}
    </div>` : ""}
  </section>`;
}

function render() {
  renderCov();
  renderControls();
  const all = cards();
  const list = applyView(all);
  $("#list").innerHTML = list.length ? list.map(cardHtml).join("")
    : `<div class="empty">${all.length ? "この状態のカードはありません。"
      : rawCardCount() ? "現在の表示では出るカードがありません（過去の監査・処分済みの項目は上のスイッチで表示できます）。"
      : "監査が付いた評価はまだありません。"}</div>`;
}

function ensureDecision(key, meta) {
  let r = review.decisions[key];
  if (!r) {
    r = {id: newId(), evaluation_id: meta.eid, check: meta.check, item: meta.item,
         auditor: meta.auditor || "", disposition: null, note: "", updated_at: null};
    review.decisions[key] = r;
  }
  return r;
}

document.addEventListener("click", ev => {
  const kindBtn = ev.target.closest("[data-kind]");
  if (kindBtn) { viewState.kind = kindBtn.dataset.kind; viewState.filter = "all"; render(); return; }
  const sfBtn = ev.target.closest("[data-sfilter]");
  if (sfBtn) {
    viewState.filter = sfBtn.dataset.sfilter;
    // 処分系の絞り込みは、処分済みの項目が畳まれたままでは常に空になる。
    // チップを選んだ時点で「処分済みの項目を表示」も一緒に ON にする
    if (["done", "dconfirmed", "dfalse", "dwatch"].includes(viewState.filter) && !showDone) {
      showDone = true; saveVis();
    }
    render(); return;
  }
  const dispBtn = ev.target.closest("[data-disp]");
  if (dispBtn) {
    const key = dispBtn.dataset.key;
    const r = ensureDecision(key, {eid: Number(dispBtn.dataset.eid), check: dispBtn.dataset.check,
                                   item: dispBtn.dataset.item, auditor: dispBtn.dataset.auditor});
    if (!r.auditor) r.auditor = dispBtn.dataset.auditor || "";  // メモ入力で先に作られた記録にも監査者を補う
    r.disposition = (r.disposition === dispBtn.dataset.disp) ? null : dispBtn.dataset.disp;
    r.updated_at = new Date().toISOString();
    saveReview(); render();
    return;
  }
  const resvBtn = ev.target.closest("[data-reeval]");
  if (resvBtn) {
    const cid = resvBtn.dataset.reeval;
    let r = review.reeval[cid];
    if (!r) { r = {id: newId(), candidate_id: cid, on: false}; review.reeval[cid] = r; }
    r.on = !r.on;
    saveReview(); render();
    return;
  }
  const setBtn = ev.target.closest("[data-adjset]");
  if (setBtn) {
    const eid = setBtn.dataset.adjset;
    const draft = adjDraft[eid] || {};
    const to = Number(draft.to);
    const reason = (draft.reason || "").trim();
    const msg = $("#copyMsg");
    if (!ok(to) || !reason) {
      msg.textContent = "調整の設定には、読み替え先の TRL（1〜9）と理由の両方が必要です。";
      return;
    }
    const prev = review.adjustments[eid];
    review.adjustments[eid] = {id: prev ? prev.id : newId(), evaluation_id: Number(eid),
      field: "trl_level", from_value: Number(setBtn.dataset.from), to_value: to,
      reason, updated_at: new Date().toISOString()};
    delete adjDraft[eid];
    saveReview(); render();
    msg.textContent = `調整を記録しました（TRL ${setBtn.dataset.from} → ${to}）。`;
    return;
  }
  const clrBtn = ev.target.closest("[data-adjclear]");
  if (clrBtn) {
    const rec = review.adjustments[clrBtn.dataset.adjclear];
    if (rec) {
      // 取り消しは記録を消さず「to_value = 元値」にして残す (activeAdj 参照)。
      // 次回の反映用 JSON にこの行が入ることで、DB 側の調整も取り消される
      rec.to_value = rec.from_value;
      rec.updated_at = new Date().toISOString();
    }
    delete adjDraft[clrBtn.dataset.adjclear];
    saveReview(); render();
  }
});

document.addEventListener("input", ev => {
  const note = ev.target.closest("[data-dnote]");
  if (note) {
    const key = note.dataset.dnote;
    const parts = key.split(":");
    const r = ensureDecision(key, {eid: Number(parts[0]), check: parts[1] || "", item: parts.slice(2).join(":") || "", auditor: ""});
    r.note = note.value;
    saveReview();
    return;
  }
  const rsn = ev.target.closest("[data-adjreason]");
  if (rsn) {
    const d = adjDraft[rsn.dataset.adjreason] || (adjDraft[rsn.dataset.adjreason] = {});
    d.reason = rsn.value;
  }
});
document.addEventListener("change", ev => {
  if (ev.target.id === "sortSel") { viewState.sort = ev.target.value; render(); return; }
  if (ev.target.id === "swPast") { showPast = ev.target.checked; saveVis(); render(); return; }
  if (ev.target.id === "swDone") { showDone = ev.target.checked; saveVis(); render(); return; }
  const sel = ev.target.closest("[data-adjsel]");
  if (sel) {
    const d = adjDraft[sel.dataset.adjsel] || (adjDraft[sel.dataset.adjsel] = {});
    d.to = sel.value;
  }
});

$("#copyBtn").addEventListener("click", async () => {
  const inbox = buildInbox();
  const n = inbox.decisions.length + inbox.reeval_queue.length + inbox.adjustments.length;
  const msg = $("#copyMsg");
  if (!n) {
    const anyServer = Object.keys(SERVER.decisions).length
      || Object.keys(SERVER.reeval).length || Object.keys(SERVER.adjustments).length;
    msg.textContent = anyServer
      ? "書き出す新しい記録はありません（表示中の処分・予約・調整はすべて DB に反映済みです）。"
      : "まだ記録がありません（処分・予約・調整を入れると書き出せます）。";
    return;
  }
  const done = await copyText(JSON.stringify(inbox, null, 2));
  msg.textContent = done
    ? `コピーしました（未反映分: 処分 ${inbox.decisions.length} 件・予約 ${inbox.reeval_queue.length} 件・調整 ${inbox.adjustments.length} 件）。review_inbox.json に貼ってください。`
    : "コピーできませんでした";
});

(async () => {
  let d;
  try {
    const r = await fetch("data.json");
    if (!r.ok) throw new Error("HTTP " + r.status);
    d = await r.json();
  } catch (e) {
    $("#list").innerHTML = `<div class="empty">data.json を読み込めませんでした（${esc(e.message)}）</div>`;
    return;
  }
  if (d.schema_version !== 3) {
    $("#list").innerHTML = `<div class="empty">このページが想定するデータ形式ではありません（schema_version: ${esc(d.schema_version)}）</div>`;
    return;
  }
  DATA = d;
  // チェック名はカタログ (正本) で上書きする。カタログが無い古い
  // data.json では上の CHK の静的定義のまま表示する
  if (d.audit_catalog && Array.isArray(d.audit_catalog.checks)) {
    for (const c of d.audit_catalog.checks) {
      if (c && c.id && c.name) CHK[c.id] = c.name;
    }
  }
  // DB 側 (反映済み) の状態を種として端末内の記録と重ね合わせる。
  // 処分・予約・調整が端末をまたいで同じに見えるのはこの重ね合わせによる
  mergeServerState();
  // 詳細ページからの遷移: ?k=event でイベント側、?k=all で両方を開く。
  // ?c= の候補が現在の種別に無ければ、監査がある側へ切り替える
  const q = new URLSearchParams(location.search);
  if (q.get("k") === "event" || q.get("k") === "long" || q.get("k") === "all") viewState.kind = q.get("k");
  render();
  const target = q.get("c");
  const hasCard = id => cards().some(c => c.p.id === id);
  if (target && !hasCard(target) && viewState.kind !== "all") {
    const other = viewState.kind === "event" ? "long" : "event";
    const has = (DATA.candidates || []).some(p => p.id === target && blocksOf(p, other).length);
    if (has) { viewState.kind = other; render(); }
  }
  // 対象のカードが畳み方で隠れている場合は、遷移の意図 (この記事の指摘を
  // 見る) を優先して両方のスイッチを入れてから探し直す
  if (target && !hasCard(target)) {
    const p = (DATA.candidates || []).find(x => x.id === target);
    if (p && ((p.audit || []).length || (p.audit_event || []).length) && (!showPast || !showDone)) {
      showPast = true; showDone = true; saveVis(); render();
    }
  }
  if (target && hasCard(target)) {
    viewState.filter = "all";
    render();
    const el = document.getElementById("card-" + target)
      || document.getElementById("card-" + target + "--event");
    if (el) {
      el.scrollIntoView({block: "start"});
      el.classList.add("hl");
      setTimeout(() => el.classList.remove("hl"), 5000);
    }
  }
})();
