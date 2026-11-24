/* 観測ページ: 一覧から移した観測系の表示 (分布・統計・動き・翻訳・実行) の置き場。
   集計の関数群は一覧 (app.js) と同じ実装の写しで、定義と母集団は一覧側と同一に保つ。
   凝った可視化はこのページで育てる (一覧は候補を見る場所に専念させる) */
"use strict";
const $ = s => document.querySelector(s);
const LANG = "ja";
const SL = {final: "選定可能", review: "要追加調査", hold: "保留", pending: "未評価", error: "エラー", rejected: "除外"};
const IL = {technology_readiness: "技術成熟度", technological_advantage: "技術的優位性", monetization: "収益化能力",
  market_growth: "市場成長性", financial_sustainability: "財務的持続可能性", moat_durability: "競争優位性の持続性",
  management_execution: "経営陣・実行力"};
const DL = {positive: "ポジティブ", negative: "ネガティブ", mixed: "混在"};
let P = [], META = null, evalMaxTs = null;
let COHORT = null, ITEM_KEYS = [], ASOF = null, CALM = null, CALOPEN = false;
const esc = v => String(v ?? "").replace(/[&<>"']/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
const ok = t => Number.isInteger(t) && t >= 1 && t <= 9;
const lvl = p => (p.long && p.long.trl_level && p.long.trl_level.level) ?? null;
const dirOf = p => (p.long && p.long.status !== "error" && p.long.direction && p.long.direction.direction) || null;
const label = k => IL[k] || k;
const candName = p => { const e = (p.entities || []).find(x => x.strength === "strong"); return e ? e.name : (p.title || "").slice(0, 24); };
const dispT = p => LANG === "ja" && p.title_ja ? p.title_ja : p.title;
const meanOf = xs => xs.reduce((a, b) => a + b, 0) / xs.length;
const fmt1 = v => v == null ? "—" : (Math.round(v * 10) / 10).toFixed(1);
function boxGeom(st, W = 170) { const X = v => Math.round((v / 100 * (W - 4) + 2) * 10) / 10; return {min: X(st.min), q1: X(st.q1), med: X(st.median), q3: X(st.q3), max: X(st.max)}; }
const SN_FINITE = {2: 0.743, 3: 1.851, 4: 0.954, 5: 1.351, 6: 0.993, 7: 1.198, 8: 1.005, 9: 1.131};
const snCorr = n => SN_FINITE[n] !== undefined ? SN_FINITE[n] : (n % 2 === 1 ? n / (n - 0.9) : 1.0);
const QN_FINITE = {2: 0.399356, 3: 0.99365, 4: 0.51321, 5: 0.84401, 6: 0.6122, 7: 0.85877, 8: 0.66993, 9: 0.87344, 10: 0.72014, 11: 0.88906, 12: 0.75743};
function qnFiniteC(n) { const t = 1 / n; return n % 2 === 1 ? 1 + (1.60188 + (-2.1284 - 5.172 * t) * t) * t : 1 + (3.67561 + (1.9654 + (6.987 - 77 * t) * t) * t) * t; }
const qnCorr = n => QN_FINITE[n] !== undefined ? QN_FINITE[n] : 1 / qnFiniteC(n);
const SRC_LABEL = {arxiv: "arXiv", pubmed: "PubMed", gnews: "News", trials: "ClinicalTrials", edinet: "EDINET", yahoo: "Yahoo Finance", openalex: "OpenAlex", prtimes: "PR TIMES", edgar: "SEC EDGAR", roic: "ROIC.ai"};
const srcName = s => SRC_LABEL[s] || esc(s);
const fmtRunAt = t => new Date(t).toLocaleString("ja-JP", {timeZone: "Asia/Tokyo", month: "numeric", day: "numeric", weekday: "short", hour: "2-digit", minute: "2-digit"});
function pointOf(p) {
  if (!p.long) return null;
  const items = {};
  for (const k of Object.keys(p.long.items || {})) items[k] = p.long.items[k] && p.long.items[k].score;
  return {evaluated_at: p.long.evaluated_at, status: p.long.status, total: p.long.total, items};
}

function evalPoints(p) {
  const h = Array.isArray(p.history) ? p.history : [];
  if (h.length) return h.map((pt, i) => ({at: pt.evaluated_at, first: i === 0}));
  if (p.long && p.long.evaluated_at) return [{at: p.long.evaluated_at, first: (p.eval_count || 1) <= 1}];
  return [];
}
function histFacts(p) {
  const h = Array.isArray(p.history) ? p.history : [];
  const cur = h.length ? h[h.length - 1] : null;
  const prv = h.length > 1 ? h[h.length - 2] : null;
  const dTrl = cur && prv && cur.trl_level != null && prv.trl_level != null ? cur.trl_level - prv.trl_level : null;
  const dTot = cur && prv && cur.total != null && prv.total != null ? Math.round((cur.total - prv.total) * 10) / 10 : null;
  return {
    n: h.length, first: h.length === 1, dTrl, dTot,
    bandCross: !!(cur && prv && cur.band && prv.band && cur.band !== prv.band),
    dirFlip: !!(cur && prv && cur.direction && prv.direction && cur.direction !== prv.direction),
    moved: dTrl !== null ? dTrl !== 0 : (dTot !== null ? dTot !== 0 : false),
    latestAt: cur ? cur.evaluated_at : null,
  };
}
function distBins(cands) {
  const score = new Array(10).fill(0), trl = new Array(10).fill(0);  // trl[0..8]=TRL1..9、trl[9]=未確定
  for (const p of cands || []) {
    if (p.display_status === "rejected") continue;
    const t = p.long && typeof p.long.total === "number" ? p.long.total : null;
    if (t != null) score[Math.min(9, Math.max(0, Math.floor(t / 10)))]++;
    if (p.long && p.long.status !== "error") {
      const l = lvl(p);
      if (l != null) trl[l - 1]++; else trl[9]++;
    }
  }
  return {score, trl};
}
function barsSvg(counts, labels, w, h) {
  const max = Math.max(1, ...counts);
  const bw = w / counts.length;
  const rects = counts.map((n, i) => {
    const bh = Math.round((n / max) * (h - 12));
    return `<rect x="${(i * bw + 1).toFixed(1)}" y="${h - 10 - bh}" width="${(bw - 2).toFixed(1)}" height="${bh}" fill="var(--accent)" opacity="${n ? 0.85 : 0.25}"><title>${esc(labels[i])}: ${n}件</title></rect>`
      + `<text x="${(i * bw + bw / 2).toFixed(1)}" y="${h - 1}" font-size="7" text-anchor="middle" fill="var(--muted)">${esc(labels[i])}</text>`;
  }).join("");
  return `<svg viewBox="0 0 ${w} ${h}" width="${w}" height="${h}" role="img">${rects}</svg>`;
}
function scatterGeom(pts) {
  const W = 170, H = 64;
  return pts.map(({dTot, dTrl}) => ({
    x: Math.round((W / 2 + Math.max(-50, Math.min(50, dTot)) / 50 * (W / 2 - 4)) * 10) / 10,
    y: Math.round((H / 2 - Math.max(-8, Math.min(8, dTrl)) / 8 * (H / 2 - 4)) * 10) / 10,
  }));
}
function scatterHtml() {
  const pts = [];
  for (const p of P) { const f = histFacts(p); if (f.dTot !== null && f.dTrl !== null) pts.push(f); }
  if (!pts.length) return "";
  const cs = scatterGeom(pts);
  return `<span>変動の散布（再評価 ${pts.length}件、横=総合点の差 / 縦=TRLの差）</span>`
    + `<svg viewBox="0 0 170 64" width="170" height="64" role="img"><line x1="85" y1="2" x2="85" y2="62" stroke="var(--line)"/><line x1="2" y1="32" x2="168" y2="32" stroke="var(--line)"/>`
    + cs.map(c => `<circle cx="${c.x}" cy="${c.y}" r="2.5" fill="var(--accent)" opacity="0.7"/>`).join("") + `</svg>`;
}
function nullDistHtml() {
  const keys = ["technology_readiness", "technological_advantage", "monetization", "market_growth", "financial_sustainability", "moat_durability"];
  const cnt = {}; let base = 0;
  for (const p of P) {
    if (!p.long || p.long.status === "error") continue;
    base++;
    for (const k of keys) { const it = p.long.items ? p.long.items[k] : null; if (!it || it.score == null) cnt[k] = (cnt[k] || 0) + 1; }
  }
  if (!base) return "";
  const parts = keys.filter(k => cnt[k]).map(k => `${label(k)} ${cnt[k]}件`);
  return `<span>項目別の未確定（評価 ${base}件中）: ${parts.length ? esc(parts.join(" / ")) : "なし"}</span>`;
}
function pct7(xs, p) {
  const s = [...xs].sort((a, b) => a - b);
  if (s.length === 1) return s[0];
  const k = (s.length - 1) * p, f = Math.floor(k), c = Math.min(f + 1, s.length - 1);
  return s[f] + (s[c] - s[f]) * (k - f);
}
function sdOf(xs) { if (xs.length < 2) return null; const m = meanOf(xs); return Math.sqrt(xs.reduce((a, b) => a + (b - m) ** 2, 0) / (xs.length - 1)); }
function snOf(xs) {
  const s = [...xs].sort((a, b) => a - b), n = s.length;
  if (n === 1) return 0;
  const inner = s.map(x => { const d = s.map(y => Math.abs(x - y)).sort((a, b) => a - b); return d[Math.floor(n / 2)]; });
  inner.sort((a, b) => a - b);
  return 1.1926 * snCorr(n) * inner[Math.floor((n + 1) / 2) - 1];
}
function qnOf(xs) {
  const s = [...xs].sort((a, b) => a - b), n = s.length;
  if (n < 2) return null;
  const diffs = [];
  for (let i = 0; i < n; i++) for (let j = i + 1; j < n; j++) diffs.push(Math.abs(s[i] - s[j]));
  diffs.sort((a, b) => a - b);
  const h = Math.floor(n / 2) + 1, k = h * (h - 1) / 2;
  return 2.21914446598508 * qnCorr(n) * diffs[k - 1];
}
function statsOf(xs) {
  const v = (xs || []).filter(x => typeof x === "number" && isFinite(x));
  if (!v.length) return null;
  return {n: v.length, mean: meanOf(v), median: pct7(v, 0.5), sd: sdOf(v), sn: snOf(v), qn: qnOf(v),
    q1: pct7(v, 0.25), q3: pct7(v, 0.75), min: Math.min(...v), max: Math.max(...v)};
}
function boxSvg(st) {
  const b = boxGeom(st);
  return `<svg viewBox="0 0 170 18" width="170" height="18" role="img"><line x1="${b.min}" y1="9" x2="${b.max}" y2="9" stroke="var(--muted)"/><rect x="${b.q1}" y="4" width="${Math.max(1, b.q3 - b.q1)}" height="10" fill="var(--accent)" opacity="0.35" stroke="var(--accent)"/><line x1="${b.med}" y1="3" x2="${b.med}" y2="15" stroke="var(--accent)" stroke-width="2"/></svg>`;
}
function buildCohort() {
  const g = (META.config && META.config.item_groups) || {};
  ITEM_KEYS = [...(g.technology || ["technology_readiness", "technological_advantage"]),
    ...(g.business || ["monetization", "market_growth", "financial_sustainability", "moat_durability"])];
  if (!ASOF) {
    const finals = P.filter(p => p.display_status === "final" && p.long && p.long.items);
    const items = {};
    for (const k of ITEM_KEYS) items[k] = statsOf(finals.map(p => p.long.items[k] && p.long.items[k].score));
    COHORT = {items, total: statsOf(finals.map(p => p.long.total)), asOf: null};
    return;
  }
  const pts = P.map(p => asOfPoint(p, ASOF)).filter(pt => pt && pt.status === "final");
  const items = {};
  for (const k of ITEM_KEYS) items[k] = statsOf(pts.map(pt => pt.items && pt.items[k]));
  COHORT = {items, total: statsOf(pts.map(pt => pt.total)), asOf: ASOF};
}
function asOfPoint(p, asOf) {
  const pts = [...(p.history || []), ...(p.history && p.history.length ? [] : [pointOf(p)])]
    .filter(pt => pt && pt.evaluated_at && pt.evaluated_at.slice(0, 10) <= asOf);
  if (!pts.length) return null;
  pts.sort((a, b) => a.evaluated_at < b.evaluated_at ? -1 : 1);
  return pts[pts.length - 1];
}
function evalDayCounts() {
  const m = new Map();
  const add = ts => { if (!ts) return; const d = ts.slice(0, 10); m.set(d, (m.get(d) || 0) + 1); };
  for (const p of P) {
    if (p.history && p.history.length) p.history.forEach(pt => add(pt.evaluated_at));
    else if (p.long) add(p.long.evaluated_at);
  }
  return m;
}
function monthCells(y, m) {
  const first = new Date(Date.UTC(y, m, 1));
  const lead = (first.getUTCDay() + 6) % 7;
  const days = new Date(Date.UTC(y, m + 1, 0)).getUTCDate();
  const cells = [];
  for (let i = 0; i < lead; i++) cells.push(null);
  for (let d = 1; d <= days; d++) cells.push(`${y}-${String(m + 1).padStart(2, "0")}-${String(d).padStart(2, "0")}`);
  while (cells.length % 7) cells.push(null);
  return cells;
}
function calHtml() {
  if (!CALOPEN || !CALM) return "";
  const counts = evalDayCounts();
  const maxDay = [...counts.keys()].sort().pop() || "";
  const wd = ["月", "火", "水", "木", "金", "土", "日"].map(w => `<span class="calw">${w}</span>`).join("");
  const cells = monthCells(CALM.y, CALM.m).map(d => {
    if (!d) return `<span class="cald empty"></span>`;
    const n = counts.get(d) || 0, dis = d > maxDay ? " disabled" : "", sel = d === ASOF ? " sel" : "";
    return `<button class="cald${sel}" data-d="${d}"${dis}>${Number(d.slice(8))}${n ? `<span class="cdot">${n}件</span>` : ""}</button>`;
  }).join("");
  return `<div class="calpop"><div class="calhead"><button class="chip" data-mv="-1">‹ 前月</button>`
    + `<b>${CALM.y}年${CALM.m + 1}月</b><button class="chip" data-mv="1">翌月 ›</button></div>`
    + `<div class="calgrid">${wd}${cells}</div>`
    + `<p class="hint">件数はその日に行われた long 評価の数です。日を選ぶと、その日時点の母集団で統計とレーダーの中央値が再計算されます。</p></div>`;
}
function rateGeom(hist, W = 260, H = 34) {
  const rates = (hist || []).map(r => {
    const c = r.counts || {}, ok = c.sources_ok, ng = c.sources_failed;
    return (typeof ok === "number" && typeof ng === "number" && ok + ng > 0) ? ok / (ok + ng) : null;
  }).filter(v => v !== null);
  return rates.map((v, i) => ({
    x: rates.length <= 1 ? Math.round(W / 2) : Math.round(i / (rates.length - 1) * (W - 4) + 2),
    y: Math.round((H - 4) - v * (H - 10)), rate: v}));
}
function nextRuns(fromMs, n) {
  const out = [];
  const JST = 9 * 3600000;
  const d = new Date(fromMs + JST);
  const base = Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate());
  for (let i = 0; out.length < n && i < 14; i++) {
    const day = new Date(base + i * 86400000);
    const wd = day.getUTCDay();
    if (wd >= 2 && wd <= 5) {
      const t = base + i * 86400000 + 6.5 * 3600000 - JST;
      if (t > fromMs) out.push(t);
    }
  }
  return out;
}
function moveBand() {
  // 直近7日の評価件数 (履歴と最新の評価から数える) と、大きく動いた候補
  const el = $("#moveband");
  if (!el) return;
  let week = 0, firsts = 0;
  if (evalMaxTs) for (const p of P) for (const pt of evalPoints(p)) {
    const at = Date.parse(pt.at || "");
    if (at && evalMaxTs - at <= 7 * 864e5 && at <= evalMaxTs) { week++; if (pt.first) firsts++; }
  }
  const moved = [];
  for (const p of P) {
    const f = histFacts(p);
    if (f.n >= 2 && (f.bandCross || (f.dTrl !== null && f.dTrl !== 0) || (f.dTot !== null && Math.abs(f.dTot) >= 10))) moved.push({p, f});
  }
  moved.sort((a, b) => String(b.f.latestAt || "").localeCompare(String(a.f.latestAt || "")));
  if (!week && !moved.length) { el.hidden = true; return; }
  el.innerHTML = `<span>直近7日の評価 <strong>${week}件</strong>（初回 ${firsts}件 / 再評価 ${week - firsts}件）</span>`
    + moved.slice(0, 12).map(({p, f}) => `<a class="tz" href="detail.html?id=${encodeURIComponent(p.id)}">${esc(candName(p))}: ${f.dTrl !== null && f.dTrl !== 0 ? `TRL ${f.dTrl > 0 ? "+" : ""}${f.dTrl} ` : ""}${f.dTot !== null ? `総合 ${f.dTot > 0 ? "+" : ""}${f.dTot}` : ""}${f.bandCross ? " ・帯域を跨いだ" : ""}</a>`).join("");
  el.hidden = false;
}
function usageBand() {
  // 翻訳使用量のゲージ (translation_usage の直読み)
  const el = $("#usageband");
  if (!el) return;
  const u = META && META.translation_usage;
  if (!u) { el.hidden = true; return; }
  const caps = [["Azure (月)", 2000000, (u.monthly || {}).azure], ["Google (月)", 500000, (u.monthly || {}).google], ["DeepL (通算)", 1000000, (u.cumulative || {}).deepl]];
  const seg = ([lab, cap, v]) => {
    if (typeof v !== "number") return "";
    const pct = Math.min(100, Math.round(v / cap * 1000) / 10);
    return `<span>${lab} <strong>${v.toLocaleString("ja-JP")}字</strong> / ${cap.toLocaleString("ja-JP")}字 (${pct}%)</span><span class="ubar"><span style="width:${pct}%"></span></span>`;
  };
  el.innerHTML = `<span>翻訳使用量（${esc(u.month || "")}）</span>` + caps.map(seg).join("");
  // 訳の出所別の候補数 (translation_src の直読み。言語の内訳として添える)
  const bySrc = {};
  for (const p of P) {
    const s = p.translation_src && p.translation_src.title;
    if (s) bySrc[s] = (bySrc[s] || 0) + 1;
  }
  const parts = Object.entries(bySrc).sort((a, b) => b[1] - a[1]).map(([s, n]) => `${esc(s)} ${n}件`).join(" / ");
  if (parts) el.innerHTML += `<span>タイトルの訳の出所（候補数）</span><span class="tz">${parts}</span>`;
  el.hidden = false;
}
function distBand() {
  // 分布の可視化: 総合点 (10点刻み) と TRL (1〜9と未確定) の度数を
  // 棒で出す。数える対象と単位は統計帯と同じ母数にそろえる
  const el = $("#distband");
  if (!el) return;
  const b = distBins(P);
  const ns = b.score.reduce((a, x) => a + x, 0), nt = b.trl.reduce((a, x) => a + x, 0);
  if (!ns && !nt) { el.hidden = true; return; }
  el.innerHTML = `<span>総合点の分布（${ns}件）</span>${barsSvg(b.score, ["0", "10", "20", "30", "40", "50", "60", "70", "80", "90"], 170, 40)}`
    + `<span>TRLの分布（${nt}件）</span>${barsSvg(b.trl, ["1", "2", "3", "4", "5", "6", "7", "8", "9", "未"], 170, 40)}`
    + scatterHtml() + nullDistHtml()
    + `<span><a href="deductions.html">減点理由の集計ページ</a></span>`;
  el.hidden = false;
}
function statsBand() {
  const el = $("#statband");
  if (!el) return;
  if (!COHORT) { el.hidden = true; return; }
  const row = (lab, st) => st ? `<span class="srow"><b>${esc(lab)}</b> ${boxSvg(st)}<span class="snum">n=${st.n} 平均 ${fmt1(st.mean)} / 中央値 ${fmt1(st.median)} / SD ${fmt1(st.sd)} / Sn ${fmt1(st.sn)} / Qn ${fmt1(st.qn)} / Q1 ${fmt1(st.q1)} / Q3 ${fmt1(st.q3)}</span></span>` : "";
  const when = COHORT.asOf ? `${COHORT.asOf} 時点` : "最新";
  // 節目表示: 観測の累計 (評価回数の合計・企業数・観測日数) は候補の直読み
  const totEvals = P.reduce((a, p) => a + (p.eval_count || 0), 0);
  const nEnt = new Set(P.flatMap(p => (p.entities || []).map(e => e.entity_id))).size;
  const firstDay = P.flatMap(p => (p.history || []).map(h => h.evaluated_at)).sort()[0];
  const genMs = Date.parse(META.generated_at || "");
  const obsDays = firstDay && !Number.isNaN(genMs) ? Math.max(1, Math.floor((genMs - Date.parse(firstDay)) / 86400000) + 1) : null;
  const miles = `<span class="tz">節目: 累計評価 ${totEvals}件 / 企業 ${nEnt}社${obsDays ? ` / 観測 ${obsDays}日目` : ""}</span>`;
  // 世代別の件数と総合点の中央値 (最新の long 評価の gen_config で分ける)
  const byGen = {};
  for (const p of P) {
    if (p.display_status !== "final" || !p.long || typeof p.long.total !== "number") continue;
    const g = (p.long.gen && p.long.gen.config) || "不明";
    (byGen[g] = byGen[g] || []).push(p.long.total);
  }
  const genTxt = Object.entries(byGen).sort((a, b) => b[1].length - a[1].length)
    .map(([g, vs]) => `${esc(g)}${g === (META.config && META.config.version) ? " (現行)" : ""}: ${vs.length}件・中央値 ${fmt1(statsOf(vs).median)}`).join(" / ");
  const genHtml = genTxt ? `<span class="tz">世代別: ${genTxt}</span>` : "";
  el.innerHTML = `<span>確定評価の統計（${when}の母集団 ${COHORT.total ? COHORT.total.n : 0}件、箱は最小–Q1–中央値–Q3–最大）</span>` + miles + genHtml
    + `<span class="srow"><button class="chip" id="asofbtn">母集団の基準日: ${COHORT.asOf || "最新（as_of）"}</button>`
    + (COHORT.asOf ? `<button class="chip" id="asofclear">最新に戻す</button>` : "") + `</span>`
    + calHtml()
    + (COHORT.total ? row("総合点", COHORT.total) + ITEM_KEYS.map(k => row(label(k), COHORT.items[k])).join("")
      : `<span class="snum">その日時点で確定した評価はまだありません。</span>`);
  el.hidden = false;
}
function runBand() {
  const el = $("#runband");
  if (!el) return;
  const hist = META.run_history || [], ss = META.source_stats || [];
  if (!hist.length && !ss.length) { el.hidden = true; return; }
  const pts = rateGeom(hist);
  const line = pts.length > 1 ? `<svg viewBox="0 0 260 34" width="260" height="34" role="img"><polyline points="${pts.map(p => p.x + "," + p.y).join(" ")}" fill="none" stroke="var(--accent)"/>${pts.map(p => `<circle cx="${p.x}" cy="${p.y}" r="2" fill="var(--accent)"/>`).join("")}</svg>` : "";
  const lastRate = pts.length ? Math.round(pts[pts.length - 1].rate * 100) : null;
  const recent = hist.slice(-5).reverse().map(r => `<span class="tz">${esc((r.as_of || "").slice(0, 10))} ${esc(r.status || "")} 確定 ${r.counts && r.counts.final != null ? r.counts.final : "—"}/保留 ${r.counts && r.counts.hold != null ? r.counts.hold : "—"}${r.duration_sec ? ` ・${Math.round(r.duration_sec / 60)}分` : ""}</span>`).join("");
  const srcs = ss.slice(0, 8).map(s => `<span class="tz">${esc(s.source)}: 取得 ${s.fetched}・新規 ${s.new}・失敗率 ${s.calls ? Math.round((s.calls - s.ok_calls) / s.calls * 100) : 0}%</span>`).join("");
  const orphans = META.run && META.run.counts && META.run.counts.db_orphans;
  // 財務根拠の出所別に、その出所の根拠を持つ候補の数を数える (補間の適用状況)
  const finBySrc = {};
  for (const p of P) {
    const seen = new Set((p.related_financials || []).map(f => f.source).filter(Boolean));
    for (const s of seen) finBySrc[s] = (finBySrc[s] || 0) + 1;
  }
  const finTxt = Object.entries(finBySrc).sort((a, b) => b[1] - a[1])
    .map(([s, n]) => `${esc(srcName(s))} ${n}件`).join(" / ");
  el.innerHTML = `<span>収集の成功率${lastRate !== null ? `（最新 ${lastRate}%）` : ""} <a href="runs.html">実行履歴ページ</a></span>${line}`
    + (recent ? `<span>直近の実行</span>${recent}` : "")
    + (srcs ? `<span>ソース別（基準ラン）</span>${srcs}` : "")
    + (orphans ? `<span class="tz">辞書に無い候補（孤児） ${orphans}件</span>` : "")
    + (finTxt ? `<span>財務根拠の出所（候補数）</span><span class="tz">${finTxt}</span>` : "")
    + (() => { const nr = nextRuns(Date.now(), 1)[0]; return nr ? `<span>次回実行</span><span class="tz">${esc(fmtRunAt(nr))} JST（あと約 ${Math.max(1, Math.round((nr - Date.now()) / 3600000))}時間）</span>` : ""; })();
  el.hidden = false;
}
function nearBand() {
  // あと1項目で確定: 保留のうち必須項目の不足が1項目だけの候補
  const el = $("#nearband");
  if (!el) return;
  const rows = P.filter(p => p.display_status === "hold" && p.long && Array.isArray(p.long.critical_missing) && p.long.critical_missing.length === 1)
    .sort((a, b) => String(b.long.evaluated_at || "").localeCompare(String(a.long.evaluated_at || "")));
  if (!rows.length) { el.hidden = true; return; }
  el.innerHTML = `<span>あと1項目で確定 <strong>${rows.length}件</strong></span>`
    + rows.slice(0, 12).map(p => `<a class="tz" href="detail.html?id=${encodeURIComponent(p.id)}">${esc(candName(p))}（不足: ${esc(label(p.long.critical_missing[0]))}）</a>`).join("");
  el.hidden = false;
}

/* 母集団の基準日カレンダー: 日を選ぶと母集団をその日時点で再構成する */
$("#statband").addEventListener("click", e => {
  const mv = e.target.closest("[data-mv]"), day = e.target.closest("[data-d]");
  if (e.target.id === "asofbtn") {
    CALOPEN = !CALOPEN;
    if (CALM == null) {
      const days = [...evalDayCounts().keys()].sort();
      const base = (ASOF || days[days.length - 1] || (META.generated_at || "")).slice(0, 7).split("-").map(Number);
      CALM = {y: base[0] || 2026, m: (base[1] || 1) - 1};
    }
    statsBand();
  } else if (e.target.id === "asofclear") {
    ASOF = null; CALOPEN = false; buildCohort(); statsBand();
  } else if (mv && CALM) {
    const d = new Date(Date.UTC(CALM.y, CALM.m + Number(mv.dataset.mv), 1));
    CALM = {y: d.getUTCFullYear(), m: d.getUTCMonth()}; statsBand();
  } else if (day && !day.disabled) {
    ASOF = day.dataset.d; CALOPEN = false;
    const b = ASOF.split("-").map(Number); CALM = {y: b[0], m: b[1] - 1};
    buildCohort(); statsBand();
  }
});

async function load() {
  try {
    const res = await fetch("data.json", {cache: "no-cache"});
    const d = await res.json();
    META = d; P = d.candidates || [];
    evalMaxTs = Math.max(0, ...P.flatMap(p => evalPoints(p).map(pt => Date.parse(pt.at || "") || 0))) || null;
    buildCohort();
    distBand(); moveBand(); nearBand(); usageBand(); statsBand(); runBand();
  } catch (e) {
    const el = $("#oberr");
    if (el) el.textContent = "data.json を読み込めませんでした。";
  }
}
load();

/* ページ移動のキーシーケンス: g の後にホームポジション周辺のキー
   (a=監査 c=採点基準 w=Look・See o=観測 q=次回評価予定 r=実行履歴 d=項目別の不足 l=用語集) */
{
  const GKEYS = {i: "index.html", b: "brief.html", a: "audit.html", c: "criteria.html", w: "watch.html", o: "observe.html", q: "queue.html", r: "runs.html", d: "deductions.html", l: "glossary.html", k: "keys.html"};
  let gAt = 0;
  document.addEventListener("keydown", e => {
    if (e.metaKey || e.ctrlKey || e.altKey || e.isComposing) return;
    const t = e.target;
    if (t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.tagName === "SELECT")) return;
    if (gAt && Date.now() - gAt < 900 && GKEYS[e.key]) { e.preventDefault(); location.href = GKEYS[e.key]; gAt = 0; return; }
    gAt = e.key === "g" ? Date.now() : 0;
  });
}

/* 基準日 (as-of) を評価日単位で前後に送る: [ で前へ、] で次へ。
   最後より先へ送ると「最新」に戻る。カレンダーを開かずに当時の母集団をめくる用 */
document.addEventListener("keydown", e => {
  if (e.metaKey || e.ctrlKey || e.altKey || e.isComposing) return;
  if (e.key !== "[" && e.key !== "]") return;
  const days = [...evalDayCounts().keys()].sort();
  if (!days.length) return;
  const cur = ASOF ? days.indexOf(ASOF) : days.length;
  const nxt = e.key === "[" ? cur - 1 : cur + 1;
  if (nxt < 0) return;
  ASOF = nxt >= days.length ? null : days[nxt];
  if (ASOF) { const b = ASOF.split("-").map(Number); CALM = {y: b[0], m: b[1] - 1}; }
  CALOPEN = false;
  buildCohort(); statsBand();
});
/* ←/→ で統計 (箱ひげ) の項目ハイライトを巡回する */
let boxHl = -1;
document.addEventListener("keydown", e => {
  if (e.key !== "ArrowRight" && e.key !== "ArrowLeft") return;
  const t = e.target;
  if (t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.tagName === "SELECT")) return;
  const rows = [...document.querySelectorAll("#statband .srow")];
  if (!rows.length) return;
  e.preventDefault();
  boxHl = (((boxHl + (e.key === "ArrowRight" ? 1 : -1)) % rows.length) + rows.length) % rows.length;
  rows.forEach((r, i) => r.classList.toggle("hl", i === boxHl));
  rows[boxHl].scrollIntoView({block: "nearest"});
});
