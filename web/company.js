"use strict";
/* 企業ページ: data.json の候補と監視企業を企業単位に集約して表示する。
   帰属は候補の entities (候補側の企業リンク) と watch_companies
   (Look・See の監視企業) の和集合で、値はすべて data.json の直読み。
   企業単位の新しい判定や点数は作らない。 */
const $ = s => document.querySelector(s);
const esc = v => String(v ?? "").replace(/[&<>"']/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
const safeUrl = u => /^https?:\/\//i.test(String(u || "")) ? String(u) : "#";
const ML = {JP: "日本", US: "米国", GB: "英国", CH: "スイス", DE: "ドイツ", DK: "デンマーク", SE: "スウェーデン", IT: "イタリア", CA: "カナダ", AU: "オーストラリア", ES: "スペイン", FR: "フランス", NL: "オランダ", FI: "フィンランド", NO: "ノルウェー", BE: "ベルギー", IN: "インド", SG: "シンガポール", NZ: "ニュージーランド", PL: "ポーランド", AT: "オーストリア", TW: "台湾", HK: "香港", IE: "アイルランド", PT: "ポルトガル", CZ: "チェコ", GR: "ギリシャ"};
const CL = {space: "宇宙", bio: "バイオ", semiconductor: "半導体", ai: "AI", robotics: "ロボティクス", energy: "エネルギー・蓄電池", quantum: "量子技術", defense: "防衛"};
const TL = {photonics: "フォトニクス", medtech: "医療機器", earth_observation: "地球観測", constellation: "衛星コンステレーション", space_transport: "宇宙輸送", lunar: "月面開発", debris_removal: "デブリ除去", mrna: "mRNA", cell_therapy: "細胞治療", gene_therapy: "遺伝子治療・ゲノム編集", antibody: "抗体医薬", radiopharm: "放射性医薬品", vaccine: "ワクチン", regenerative_medicine: "再生医療", diagnostics: "診断", drug_ai: "創薬AI", surgical_robot: "手術ロボット", power_semi: "パワー半導体", advanced_packaging: "先端パッケージング", memory: "メモリ", semi_equipment: "半導体製造装置", ai_infra: "AIインフラ・データセンター", voice_ai: "音声AI", industrial_robotics: "産業用ロボット・自動化", warehouse_automation: "倉庫・物流自動化", autonomous_mobility: "自律移動・ドローン", humanoid: "ヒューマノイド", hydrogen: "水素・燃料電池", ess: "蓄電・ESS", ev_charging: "EV充電", solid_state_battery: "全固体電池", battery_materials: "電池材料", perovskite: "ペロブスカイト太陽電池", fusion: "核融合", smr: "SMR", quantum_computing: "量子コンピュータ", quantum_comm: "量子通信・暗号", quantum_sensing: "量子センシング"};
const STG = {rd: "研究開発型", demo: "実証型", production: "量産型", commercial: "商業化型"};
const DTL = {core: "Core", conditional: "Conditional", look: "Look", see: "See"};
const SL = {final: "選定可能", review: "要追加調査", hold: "保留", pending: "未評価", error: "エラー", rejected: "除外"};
const DL = {positive: "ポジティブ", negative: "ネガティブ", mixed: "混在"};
const BRL = {devil_river: "Devil River", valley_of_death: "Valley of Death", darwinian_sea: "Darwinian Sea"};
const BRO = ["devil_river", "valley_of_death", "darwinian_sea"];
const tagLabel = t => CL[t] || TL[t] || t;
const dtierOf = e => e.derived_tier || (e.tier === "watch" ? "look" : e.tier === "reference" ? "see" : "core");
const dtText = co => {
  const l = DTL[dtierOf(co)] || "";
  if (!l || dtierOf(co) !== "conditional") return l;
  const rs = (Array.isArray(co.derived_reasons) ? co.derived_reasons : []).filter(Boolean);
  return rs.length ? `Conditional（${rs.join("／")}）` : l;
};
let LANG = "ja";
try { LANG = localStorage.getItem("trl-lang") || "ja"; } catch (e) { /* 保存不可なら既定のまま */ }
const dispT = p => LANG === "ja" && p.title_ja ? p.title_ja : p.title;
const lvl = p => (p.long && p.long.trl_level && p.long.trl_level.level) ?? null;

/* 企業単位への集約 (純粋関数)。属性は候補側の企業情報を優先し、
   監視企業だけにある属性 (ticker 等) は watch_companies から補う */
function buildCompanies(cands, watchCos) {
  const map = new Map();
  const get = id => {
    if (!map.has(id)) map.set(id, {id, name: "", market: null, sector: null, tags: [], stage: null,
      tier: null, derived_tier: null, derived_reasons: [], ticker: null, links: [], watch: null});
    return map.get(id);
  };
  for (const p of cands || []) for (const e of p.entities || []) {
    if (!e.entity_id) continue;
    const c = get(e.entity_id);
    c.name = e.name || c.name;
    c.market = e.market || c.market;
    c.sector = e.sector || c.sector;
    if ((e.tags || []).length) c.tags = e.tags;
    c.stage = e.stage || c.stage;
    c.tier = e.tier || c.tier;
    c.derived_tier = e.derived_tier || c.derived_tier;
    if ((e.derived_reasons || []).length) c.derived_reasons = e.derived_reasons;
    c.links.push({cand: p, strength: e.strength, product_name: e.product_name || null});
  }
  for (const w of watchCos || []) {
    if (!w.id) continue;
    const c = get(w.id);
    c.name = c.name || w.name || "";
    c.market = c.market || w.market;
    c.sector = c.sector || w.sector;
    if (!c.tags.length) c.tags = w.tags || [];
    c.stage = c.stage || w.stage;
    c.tier = c.tier || w.tier;
    c.derived_tier = c.derived_tier || w.derived_tier;
    if (!c.derived_reasons.length) c.derived_reasons = w.derived_reasons || [];
    c.ticker = w.ticker || null;
    c.watch = w;
  }
  return map;
}
/* その企業の最新の long 評価 (error は評価結果ではないので除く) */
function latestLong(co) {
  let best = null;
  for (const l of co.links) {
    const ev = l.cand.long;
    if (!ev || ev.status === "error") continue;
    if (!best || String(ev.evaluated_at || "") > String(best.ev.evaluated_at || "")) best = {cand: l.cand, ev};
  }
  return best;
}
/* 企業の全評価を1本に繋いだ統合タイムライン (純粋関数)。
   各候補の history (最新を含む全点) を評価日時の新しい順に並べる */
function entityTimeline(co) {
  const rows = [];
  for (const l of co.links) {
    const pts = Array.isArray(l.cand.history) && l.cand.history.length
      ? l.cand.history
      : (l.cand.long ? [{evaluated_at: l.cand.long.evaluated_at, status: l.cand.long.status, total: l.cand.long.total, trl_level: lvl(l.cand)}] : []);
    for (const pt of pts) rows.push({at: pt.evaluated_at || "", cand: l.cand, pt});
  }
  return rows.sort((a, b) => String(b.at).localeCompare(String(a.at)));
}
/* 同分野の競合ランキング (純粋関数)。順位のキーは各企業の最新の long 評価の
   総合点で、同点は企業名順。対象は総合点のある企業だけ */
function sectorRanking(cos, sector) {
  const rows = [];
  for (const co of cos.values()) {
    if (co.sector !== sector) continue;
    const lat = latestLong(co);
    if (lat && typeof lat.ev.total === "number") rows.push({co, total: lat.ev.total});
  }
  return rows.sort((a, b) => b.total - a.total || a.co.name.localeCompare(b.co.name, "ja"));
}
function barrierHitsOf(co) {
  const out = [];
  for (const l of co.links) {
    const b = l.cand.long && l.cand.long.barriers;
    if (!b) continue;
    for (const k of BRO) if (b[k] && b[k].status === "hit") out.push({gate: k, cand: l.cand});
  }
  return out;
}
/* 市場×分野のカバレッジ (純粋関数): 企業数の行列と市場別の集計。
   選定可能は、その企業の最新の long 評価を持つ候補が final のこと */
function coverageOf(cos) {
  const markets = new Map(), sectors = new Set();
  for (const co of cos.values()) {
    const mk = co.market || "—";
    if (!markets.has(mk)) markets.set(mk, {total: 0, finals: 0, bySector: new Map()});
    const m = markets.get(mk);
    m.total++;
    const lat = latestLong(co);
    if (lat && lat.cand.display_status === "final") m.finals++;
    const sc = co.sector || "—";
    sectors.add(sc);
    m.bySector.set(sc, (m.bySector.get(sc) || 0) + 1);
  }
  return {markets, sectors: [...sectors].sort()};
}
function coverageHtml(cos) {
  const {markets, sectors} = coverageOf(cos);
  const mks = [...markets.entries()].sort((a, b) => b[1].total - a[1].total);
  if (!mks.length) return "";
  const maxCell = Math.max(1, ...mks.flatMap(([, m]) => sectors.map(s => m.bySector.get(s) || 0)));
  const head = `<tr><th>市場</th>${sectors.map(s => `<th>${esc(CL[s] || s)}</th>`).join("")}<th>企業数</th><th>選定可能</th></tr>`;
  const body = mks.map(([mk, m]) => `<tr><td>${esc(ML[mk] || mk)}</td>${sectors.map(s => {
    const n = m.bySector.get(s) || 0;
    const bg = n ? ` style="background:color-mix(in srgb, var(--accent) ${Math.round(12 + 50 * n / maxCell)}%, transparent)"` : "";
    return `<td${bg}>${n || ""}</td>`;
  }).join("")}<td>${m.total}</td><td>${m.finals}</td></tr>`).join("");
  return `<section class="ds"><h4>市場×分野のカバレッジ（企業数）</h4><div class="tscroll"><table class="rt"><thead>${head}</thead><tbody>${body}</tbody></table></div><p class="hint">選定可能は、その企業の最新の評価が確定している企業の数です。色の濃さは企業数に比例します。</p></section>`;
}
const metaLine = co => [co.market ? ML[co.market] || co.market : "", co.sector ? CL[co.sector] || co.sector : "",
  co.stage ? STG[co.stage] || co.stage : "", dtText(co)].filter(Boolean).join("・");

let DATA = null;
function renderIndex(cos) {
  const all = [...cos.values()].sort((a, b) => a.name.localeCompare(b.name, "ja"));
  const row = co => {
    const lat = latestLong(co);
    const lastMat = co.watch && co.watch.materials && co.watch.materials.length
      ? co.watch.materials.map(m => m.published || "").sort().slice(-1)[0] : "";
    const right = lat
      ? `最新評価: 総合 ${lat.ev.total != null ? lat.ev.total + "点" : "—"} / TRL ${lvl(lat.cand) != null ? lvl(lat.cand) : "未確定"}（${esc(String(lat.ev.evaluated_at || "").slice(0, 10))}）`
      : co.watch ? `監視材料: 一次情報 ${co.watch.counts.primary}件 / ニュース ${co.watch.counts.news}件${lastMat ? ` / 最新 ${esc(lastMat)}` : ""}` : "未評価";
    return `<div class="crow"><span class="cn"><a href="company.html?e=${encodeURIComponent(co.id)}">${esc(co.name)}</a></span>`
      + `<span class="cm">${esc(metaLine(co))}</span>`
      + `<span class="cs">候補 ${co.links.length}件 / ${esc(right)}</span></div>`;
  };
  $("#root").innerHTML = `<h1 class="ptitle">企業</h1>`
    + `<p class="pdesc">候補に紐づく企業と、Look・See の監視企業を企業単位で集約した一覧です。値は各候補の評価の直読みで、企業単位の新しい判定は作りません。</p>`
    + coverageHtml(cos)
    + `<input id="q" class="tin" type="search" placeholder="企業名で検索" aria-label="企業名で検索">`
    + `<p class="pdesc" id="cnt" role="status"></p><div id="list"></div>`;
  const draw = () => {
    const q = $("#q").value.trim().toLowerCase();
    const rows = all.filter(co => !q || co.name.toLowerCase().includes(q));
    $("#cnt").textContent = `${rows.length}社`;
    $("#list").innerHTML = rows.map(row).join("") || `<p class="pdesc">一致する企業がありません。</p>`;
  };
  $("#q").addEventListener("input", draw);
  draw();
}
function renderCompany(co, cos) {
  const lat = latestLong(co);
  const hits = barrierHitsOf(co);
  const links = [...co.links].sort((a, b) =>
    ((b.strength === "strong") - (a.strength === "strong"))
    || String((b.cand.long && b.cand.long.evaluated_at) || "").localeCompare(String((a.cand.long && a.cand.long.evaluated_at) || ""))
    || String(b.cand.published || "").localeCompare(String(a.cand.published || "")));
  const lrow = l => {
    const p = l.cand, ev = p.long;
    const parts = [SL[p.display_status] || p.display_status,
      ev && ev.status !== "error" ? `総合 ${ev.total != null ? ev.total + "点" : "—"} / TRL ${lvl(p) != null ? lvl(p) : "未確定"}` : "",
      ev && ev.direction && ev.direction.direction ? DL[ev.direction.direction] || "" : "",
      ev && ev.evaluated_at ? `評価 ${String(ev.evaluated_at).slice(0, 10)}` : "",
      l.strength === "strong" ? "" : "弱いリンク",
      l.product_name ? `製品: ${l.product_name}` : ""].filter(Boolean).join(" / ");
    return `<div class="lrow"><div class="lt"><a href="detail.html?id=${encodeURIComponent(p.id)}">${esc(dispT(p))}</a></div><div class="lm">${esc(parts)}</div></div>`;
  };
  const mats = co.watch ? [...(co.watch.materials || [])].sort((a, b) => String(b.published || "").localeCompare(String(a.published || ""))) : [];
  const tl = entityTimeline(co);
  const rank = co.sector ? sectorRanking(cos, co.sector) : [];
  const myRank = rank.findIndex(r => r.co.id === co.id);
  const rankHtml = rank.length >= 2 && myRank >= 0
    ? `<section class="ds"><h4>同分野（${esc(CL[co.sector] || co.sector)}）の順位</h4><p class="hint">${rank.length}社中 ${myRank + 1}位（各企業の最新の評価の総合点順）。</p>${rank.slice(0, 5).map((r, i) => `<div class="lrow"><div class="lt">${i + 1}. ${r.co.id === co.id ? `<b>${esc(r.co.name)}</b>` : `<a href="company.html?e=${encodeURIComponent(r.co.id)}">${esc(r.co.name)}</a>`}</div><div class="lm">総合 ${r.total}点</div></div>`).join("")}${myRank >= 5 ? `<div class="lrow"><div class="lt">${myRank + 1}. <b>${esc(co.name)}</b></div><div class="lm">総合 ${rank[myRank].total}点</div></div>` : ""}</section>`
    : "";
  const tlHtml = tl.length
    ? `<section class="ds"><h4>評価の統合タイムライン（${tl.length}件）</h4>${tl.slice(0, 20).map(r => `<div class="lrow"><div class="lt"><a href="detail.html?id=${encodeURIComponent(r.cand.id)}">${esc(dispT(r.cand))}</a></div><div class="lm">${esc(String(r.at).slice(0, 10))} / ${esc(SL[r.pt.status] || r.pt.status || "")} / 総合 ${r.pt.total != null ? r.pt.total + "点" : "—"} / TRL ${r.pt.trl_level != null ? r.pt.trl_level : "未確定"}</div></div>`).join("")}${tl.length > 20 ? `<p class="hint">新しい順に20件まで表示しています。</p>` : ""}</section>`
    : "";
  $("#root").innerHTML = `<h1 class="ptitle">${esc(co.name)}</h1>`
    + `<p class="kv">${esc(metaLine(co))}${co.ticker ? ` / ティッカー ${esc(co.ticker)}` : ""}${(co.tags || []).length ? `<br>タグ: ${co.tags.map(t => esc(tagLabel(t))).join("・")}` : ""}</p>`
    + (lat ? `<section class="ds"><h4>最新の評価</h4><p>${esc(SL[lat.cand.display_status] || lat.cand.display_status)} / 総合 ${lat.ev.total != null ? lat.ev.total + "点" : "—"} / TRL ${lvl(lat.cand) != null ? lvl(lat.cand) : "未確定"}（評価 ${esc(String(lat.ev.evaluated_at || "").slice(0, 10))}）</p><p><a class="tg" href="detail.html?id=${encodeURIComponent(lat.cand.id)}">この候補の詳細を見る →</a></p></section>` : `<section class="ds"><h4>最新の評価</h4><p class="hint">この企業の候補に long 評価はまだありません。</p></section>`)
    + rankHtml + tlHtml
    + (hits.length ? `<section class="ds"><h4>関門の該当</h4><ul class="pts">${hits.map(h => `<li><b>${esc(BRL[h.gate])}</b><span><a href="detail.html?id=${encodeURIComponent(h.cand.id)}">${esc(dispT(h.cand))}</a></span></li>`).join("")}</ul></section>` : "")
    + `<section class="ds"><h4>関連する候補（${links.length}件）</h4>${links.map(lrow).join("") || `<p class="hint">候補はありません。</p>`}</section>`
    + (co.watch ? `<section class="ds"><h4>監視材料</h4><p class="hint">一次情報 ${co.watch.counts.primary}件 / ニュース ${co.watch.counts.news}件（Look・See の材料置き場から）。</p>${mats.map(m => `<div class="lrow"><div class="lt"><a href="${esc(safeUrl(m.url))}" target="_blank" rel="noopener noreferrer">${esc(LANG === "ja" && m.title_ja ? m.title_ja : m.title)}</a></div><div class="lm">${esc(m.source || "")}${m.published ? ` / ${esc(m.published)}` : ""}</div></div>`).join("")}</section>` : "")
    + `<section class="ds"><h4>この企業の書き出し</h4><div class="acts"><button class="btn sec" type="button" id="cojson">JSONで保存</button><button class="btn sec" type="button" id="cocsv">候補一覧をCSVで保存</button></div><p class="hint">この企業の属性と関連候補をそのまま書き出します（新しい集計はしません）。</p></section>`;
  const dl = (blob, name) => {
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob); a.download = name; a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 4000);
  };
  const mine = (DATA.candidates || []).filter(p => co.links.some(l => l.cand.id === p.id));
  const cj = $("#cojson"), cc = $("#cocsv");
  if (cj) cj.addEventListener("click", () => dl(new Blob([JSON.stringify({company: {id: co.id, name: co.name, market: co.market, sector: co.sector, stage: co.stage, ticker: co.ticker, tags: co.tags}, candidates: mine}, null, 1)], {type: "application/json"}), `trl-company-${co.id}.json`));
  if (cc) cc.addEventListener("click", () => {
    const head = ["id", "title", "status", "total", "trl", "evaluated_at", "url"];
    const q = v => { const s = String(v ?? ""); return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s; };
    const rows = mine.map(p => [p.id, dispT(p), p.display_status, p.long && typeof p.long.total === "number" ? p.long.total : "", lvl(p) ?? "", (p.long && p.long.evaluated_at) || "", p.url || ""]);
    dl(new Blob(["\ufeff" + [head.join(","), ...rows.map(r => r.map(q).join(","))].join("\r\n")], {type: "text/csv"}), `trl-company-${co.id}.csv`);
  });
}
async function load() {
  let d;
  try {
    const res = await fetch("data.json", {cache: "no-cache"});
    if (!res.ok) throw new Error("HTTP " + res.status);
    d = await res.json();
  } catch (e) {
    $("#root").innerHTML = `<p class="pdesc">data.json を読み込めません。</p>`;
    return;
  }
  if (d.schema_version !== 3) { $("#root").innerHTML = `<p class="pdesc">data.json の形式がこの画面に対応していません。</p>`; return; }
  DATA = d;
  const cos = buildCompanies(d.candidates, d.watch_companies);
  const id = new URLSearchParams(location.search).get("e");
  if (id && cos.has(id)) renderCompany(cos.get(id), cos);
  else renderIndex(cos);
}
load();

/* ページ移動のキーシーケンス: g の後にホームポジション周辺のキー
   (i=メイン b=ブリーフィング a=監査 c=採点基準 w=Look・See o=観測
    q=次回評価予定 r=実行履歴 d=項目別の不足 l=用語集 k=キー操作の説明書) */
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
