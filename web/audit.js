"use strict";
/* 監査レビューページ (段階A): data.json の audit (最新の long 評価に紐づく
   監査結果) を読み、要確認/グレーへの処分・再評価の予約・TRL の読み替え
   (調整) を端末内 (localStorage: trl-review) に記録する。
   DB への反映は段階B で review_inbox.json 経由になるため、このページは
   記録と「反映用 JSON」の書き出しまでを担う。元の評価・監査の記録は
   ここでは一切変更しない (非破壊)。調整はこのページ内だけの表示で、
   一覧・詳細の通常表示には影響しない。 */
const $ = s => document.querySelector(s);
const esc = v => String(v ?? "").replace(/[&<>"']/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
const jst = iso => { try { return new Date(iso).toLocaleString("ja-JP", {timeZone: "Asia/Tokyo", hour12: false}); } catch (e) { return iso; } };
const ok = t => Number.isInteger(t) && t >= 1 && t <= 9;
const SL = {final: "選定可能", review: "要追加調査", hold: "保留", pending: "未評価", error: "エラー", rejected: "除外"};
const CL = {space: "宇宙", bio: "バイオ", semiconductor: "半導体", ai: "AI", robotics: "ロボティクス", energy: "エネルギー・蓄電池", quantum: "量子技術", defense: "防衛"};
const IL = {technology_readiness: "技術成熟度", technological_advantage: "技術的優位性", monetization: "収益化能力",
  market_growth: "市場成長性", financial_sustainability: "財務的持続可能性", moat_durability: "競争優位性の持続性",
  trl_level: "TRL判定", counter_points: "反証", candidate: "候補の時点", total: "総合点", companies: "企業帰属"};
const CHK = {A1: "原則1: 商用展開とTRLの混同", A2: "原則2: TRL 9 の継続証拠", A3: "原則3: TRLからの推論",
  A4: "TRL 9 の根拠が商用展開のみ", A5: "点と理由の整合", A6: "欠落判定（材料プール外）", A7: "減点理由の具体性",
  A8: "反証の欠落", A9: "根拠のない高得点", A10: "時点の不整合（公開日が基準日より後）", A11: "数値の原文照合",
  A12: "出典リンクの形式", A13: "総合点の再導出照合", A14: "企業帰属（弱いリンクのみ）"};
const DISP = [["confirmed", "問題あり"], ["false_positive", "誤検知"], ["watch", "様子見"]];

const RKEY = "trl-review";
let review = loadReview();
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

function buildInbox() {
  const decisions = Object.values(review.decisions).filter(r => r && r.disposition).map(r => ({
    id: r.id, evaluation_id: r.evaluation_id, check: r.check, item: r.item,
    auditor: r.auditor || "", disposition: r.disposition, note: r.note || "",
    decided_at: r.updated_at || ""}));
  const reeval_queue = Object.values(review.reeval).filter(r => r && r.on).map(r => ({
    id: r.id, candidate_id: r.candidate_id}));
  const adjustments = Object.values(review.adjustments).filter(Boolean).map(r => ({
    id: r.id, evaluation_id: r.evaluation_id, field: "trl_level",
    from_value: r.from_value, to_value: r.to_value, reason: r.reason || ""}));
  return {version: 1, exported_at: new Date().toISOString(), decisions, reeval_queue, adjustments};
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
function audited() { return (DATA.candidates || []).filter(p => p.audit && p.audit.length); }

/* カードの状態 (絞り込みとバッジ用)。要確認/グレーは「未処分が残っている」
   もの、処分済みは「全エントリ処分済み」のものとして数える */
const viewState = {filter: "all", sort: "default"};
const SFILTERS = [["all", "すべて"], ["flag", "要確認"], ["gray", "グレー"],
  ["done", "処分済み"], ["reserved", "再評価予約"], ["adjusted", "調整済み"]];
function cardState(p) {
  let flagOpen = 0, grayOpen = 0, total = 0, decided = 0;
  for (const a of p.audit) for (const e of a.entries) {
    total++;
    const d = decisionOf(a, e);
    if (d && d.disposition) decided++;
    else if (e.verdict === "gray") grayOpen++; else flagOpen++;
  }
  const evalId = p.audit[0].evaluation_id;
  return {flagOpen, grayOpen, done: total > 0 && decided === total,
          reserved: !!(review.reeval[p.id] && review.reeval[p.id].on),
          adjusted: !!review.adjustments[evalId]};
}
function applyView(list) {
  const f = viewState.filter;
  const out = list.filter(p => {
    if (f === "all") return true;
    const st = cardState(p);
    return f === "flag" ? st.flagOpen > 0 : f === "gray" ? st.grayOpen > 0 :
           f === "done" ? st.done : f === "reserved" ? st.reserved : st.adjusted;
  });
  const trlOf = p => { const lv = p.long && p.long.trl_level ? p.long.trl_level.level : null; return ok(lv) ? lv : null; };
  const dateOf = p => (p.long && p.long.evaluated_at) || "";
  if (viewState.sort === "trl-desc") out.sort((a, b) => (trlOf(b) ?? -1) - (trlOf(a) ?? -1));
  else if (viewState.sort === "trl-asc") out.sort((a, b) => (trlOf(a) ?? 99) - (trlOf(b) ?? 99));
  else if (viewState.sort === "new") out.sort((a, b) => dateOf(b).localeCompare(dateOf(a)));
  else if (viewState.sort === "old") out.sort((a, b) => dateOf(a).localeCompare(dateOf(b)));
  return out;
}
function renderControls() {
  const counts = {all: 0, flag: 0, gray: 0, done: 0, reserved: 0, adjusted: 0};
  for (const p of audited()) {
    counts.all++;
    const st = cardState(p);
    if (st.flagOpen) counts.flag++;
    if (st.grayOpen) counts.gray++;
    if (st.done) counts.done++;
    if (st.reserved) counts.reserved++;
    if (st.adjusted) counts.adjusted++;
  }
  $("#stateFilters").innerHTML = SFILTERS.map(([v, label]) =>
    `<button type="button" class="fchip" data-sfilter="${v}" aria-pressed="${viewState.filter === v ? "true" : "false"}">${label} ${counts[v]}</button>`).join("");
  const sel = $("#sortSel");
  if (sel) sel.value = viewState.sort;
}

function renderCov() {
  const flagE = new Set(), grayE = new Set();
  let decided = 0;
  const byDisp = {confirmed: 0, false_positive: 0, watch: 0};
  for (const p of audited()) for (const a of p.audit) for (const e of a.entries) {
    if (e.verdict === "gray") grayE.add(a.evaluation_id); else flagE.add(a.evaluation_id);
    const d = decisionOf(a, e);
    if (d && d.disposition) { decided++; if (d.disposition in byDisp) byDisp[d.disposition]++; }
  }
  const reev = Object.values(review.reeval).filter(r => r && r.on).length;
  const adj = Object.keys(review.adjustments).length;
  $("#cov").innerHTML =
    `<div class="stat y"><small>要確認のある評価</small><strong>${flagE.size}</strong><em>件</em></div>` +
    `<div class="stat x"><small>グレーのある評価</small><strong>${grayE.size}</strong><em>件</em></div>` +
    `<div class="stat g"><small>処分済み</small><strong>${decided}</strong><em>件のエントリ</em></div>` +
    `<div class="stat r"><small>問題あり</small><strong>${byDisp.confirmed}</strong><em>件のエントリ</em></div>` +
    `<div class="stat g"><small>誤検知</small><strong>${byDisp.false_positive}</strong><em>件のエントリ</em></div>` +
    `<div class="stat o"><small>様子見</small><strong>${byDisp.watch}</strong><em>件のエントリ</em></div>` +
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

function auditBlockHtml(a) {
  const who = a.provider === "rules" ? "規則監査" : "LLM監査";
  const head = `${who} ${esc(a.auditor || "")}${a.version ? `（版 ${esc(a.version)}）` : ""}`;
  const entries = a.entries.length ? a.entries.map(e => entryHtml(a, e)).join("")
    : `<p class="hint" style="margin-top:8px">この監査では何も挙がりませんでした。</p>`;
  return `<div class="ablk">
    <div class="ablkhead"><span>${head}</span><span class="hint">${a.created_at ? jst(a.created_at) : ""} ・ 評価 #${a.evaluation_id}</span></div>
    ${a.summary ? `<p class="asum">${esc(a.summary)}</p>` : ""}
    ${entries}
  </div>`;
}

function adjustHtml(p, evalId) {
  const lv = p.long && p.long.trl_level ? p.long.trl_level.level : null;
  if (!ok(lv)) return `<div><strong style="font-size:13px">TRL 調整（読み替え）</strong><p class="hint" style="margin-top:4px">TRL が未確定の評価には読み替えを設定できません。</p></div>`;
  const adj = review.adjustments[evalId] || null;
  const draft = adjDraft[evalId] || {};
  const selVal = adj ? adj.to_value : (draft.to ?? "");
  const reason = adj ? (adj.reason || "") : (draft.reason ?? "");
  const opts = `<option value="">読み替え先…</option>` + Array.from({length: 9}, (_, i) => i + 1)
    .map(n => `<option value="${n}" ${String(selVal) === String(n) ? "selected" : ""}>TRL ${n}</option>`).join("");
  return `<div>
    <div class="adjrow"><strong style="font-size:13px">TRL 調整（読み替え）</strong>${adj ? `<span class="adjnow">調整: TRL ${adj.from_value} → ${adj.to_value}</span>` : ""}</div>
    <div class="adjrow" style="margin-top:6px">
      <select class="tsel" data-adjsel="${evalId}">${opts}</select>
      <input class="tin" data-adjreason="${evalId}" placeholder="調整理由（必須）" value="${esc(reason)}">
      <button type="button" class="btn quiet" data-adjset="${evalId}" data-from="${lv}">設定</button>
      ${adj ? `<button type="button" class="btn quiet" data-adjclear="${evalId}">取り消す</button>` : ""}
    </div>
    <p class="hint" style="margin-top:4px">読み替えを入れると、総合点・帯域は採点の計算で機械的に導き直されます（段階B）。項目点の手直しはしません。</p>
  </div>`;
}

function cardHtml(p) {
  const L = p.long || null;
  const title = p.title_ja || p.title;
  const entNames = (p.entities || []).map(e => e.name).filter(Boolean).join("、");
  const sectors = [...new Set((p.entities || []).map(e => CL[e.sector] || e.sector).filter(Boolean))].join("・");
  const bits = [];
  if (entNames) bits.push(esc(entNames));
  if (sectors) bits.push(esc(sectors));
  if (p.title_ja && p.title && p.title !== p.title_ja) bits.push(esc(p.title));
  const ev = [];
  if (L) {
    if (L.total !== null && L.total !== undefined) ev.push(`総合点 ${esc(L.total)}${L.band ? `（${esc(L.band)}）` : ""}`);
    const lv = L.trl_level ? L.trl_level.level : null;
    ev.push(`TRL ${ok(lv) ? lv : "未確定"}`);
    if (L.llm_model) ev.push(`評価: ${esc(L.llm_model)}`);
    if (L.evaluated_at) ev.push(jst(L.evaluated_at));
  }
  const evalId = p.audit[0].evaluation_id;
  const resv = review.reeval[p.id];
  const resvOn = !!(resv && resv.on);
  return `<section class="panel acard">
    <div class="ahead">
      <div style="min-width:0">
        <a class="atitle" href="detail.html?id=${encodeURIComponent(p.id)}">${esc(title)}</a>
        ${bits.length ? `<div class="ameta">${bits.join(" ・ ")}</div>` : ""}
      </div>
      <span class="st st-${esc(p.display_status || "")}">${esc(SL[p.display_status] || p.display_status || "")}</span>
    </div>
    ${ev.length ? `<div class="ameta">元評価: ${ev.join(" ・ ")}</div>` : ""}
    ${(() => { const st = cardState(p); const b = [];
      if (st.flagOpen) b.push(`<span class="sbadge flag">要確認 ${st.flagOpen}</span>`);
      if (st.grayOpen) b.push(`<span class="sbadge gray">グレー ${st.grayOpen}</span>`);
      if (st.done) b.push(`<span class="sbadge done">処分済み</span>`);
      if (st.reserved) b.push(`<span class="sbadge resv">再評価予約</span>`);
      if (st.adjusted) b.push(`<span class="sbadge adj">調整済み</span>`);
      return b.length ? `<div class="srow">${b.join("")}</div>` : ""; })()}
    ${p.audit.map(auditBlockHtml).join("")}
    <div class="aops">
      <div class="adjrow">
        <button type="button" class="btn quiet resv" data-reeval="${esc(p.id)}" aria-pressed="${resvOn ? "true" : "false"}">${resvOn ? "再評価: 予約済み（タップで解除）" : "再評価を予約する"}</button>
        <span class="hint">処分で「問題あり」にした評価の作り直しは、再評価（経路A）で行います。</span>
      </div>
      ${adjustHtml(p, evalId)}
    </div>
  </section>`;
}

function render() {
  renderCov();
  renderControls();
  const all = audited();
  const list = applyView(all);
  $("#list").innerHTML = list.length ? list.map(cardHtml).join("")
    : `<div class="empty">${all.length ? "この状態のカードはありません。" : "監査が付いた評価はまだありません。"}</div>`;
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
  const sfBtn = ev.target.closest("[data-sfilter]");
  if (sfBtn) { viewState.filter = sfBtn.dataset.sfilter; render(); return; }
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
    delete review.adjustments[clrBtn.dataset.adjclear];
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
  if (!n) { msg.textContent = "まだ記録がありません（処分・予約・調整を入れると書き出せます）。"; return; }
  const done = await copyText(JSON.stringify(inbox, null, 2));
  msg.textContent = done
    ? `コピーしました（処分 ${inbox.decisions.length} 件・予約 ${inbox.reeval_queue.length} 件・調整 ${inbox.adjustments.length} 件）。review_inbox.json に貼ってください。`
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
  render();
})();
