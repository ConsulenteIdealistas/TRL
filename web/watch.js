"use strict";
// Look・See ページ: 評価対象外の企業 (基礎 tier = watch / reference) の
// プール材料を、企業単位・一次情報優先で読むための表示専用ページ。
const $ = s => document.querySelector(s);
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
// リンク先は http(s) のみ許可する (主ページ・詳細ページの safeUrl と同じ扱い)
const safeUrl = u => /^https?:\/\//i.test(String(u || "")) ? String(u) : "#";
const ML = {JP: "日本", US: "米国", GB: "英国", CH: "スイス", DE: "ドイツ", DK: "デンマーク", SE: "スウェーデン", IT: "イタリア", CA: "カナダ", AU: "オーストラリア", ES: "スペイン", FR: "フランス", NL: "オランダ", FI: "フィンランド", NO: "ノルウェー", BE: "ベルギー", IN: "インド", SG: "シンガポール", NZ: "ニュージーランド", PL: "ポーランド", AT: "オーストリア", TW: "台湾", HK: "香港", IE: "アイルランド", PT: "ポルトガル", CZ: "チェコ", GR: "ギリシャ"};
const CL = {space: "宇宙", bio: "バイオ", semiconductor: "半導体", ai: "AI", robotics: "ロボティクス", energy: "エネルギー・蓄電池", quantum: "量子技術", defense: "防衛"};
const STG = {rd: "研究開発型", demo: "実証型", production: "量産型", commercial: "商業化型"};
const TL = {photonics: "フォトニクス", medtech: "医療機器", earth_observation: "地球観測", constellation: "衛星コンステレーション", space_transport: "宇宙輸送", lunar: "月面開発", debris_removal: "デブリ除去", mrna: "mRNA", cell_therapy: "細胞治療", gene_therapy: "遺伝子治療・ゲノム編集", antibody: "抗体医薬", radiopharm: "放射性医薬品", vaccine: "ワクチン", regenerative_medicine: "再生医療", diagnostics: "診断", drug_ai: "創薬AI", surgical_robot: "手術ロボット", power_semi: "パワー半導体", advanced_packaging: "先端パッケージング", memory: "メモリ", semi_equipment: "半導体製造装置", ai_infra: "AIインフラ・データセンター", voice_ai: "音声AI", industrial_robotics: "産業用ロボット・自動化", warehouse_automation: "倉庫・物流自動化", autonomous_mobility: "自律移動・ドローン", humanoid: "ヒューマノイド", hydrogen: "水素・燃料電池", ess: "蓄電・ESS", ev_charging: "EV充電", solid_state_battery: "全固体電池", battery_materials: "電池材料", perovskite: "ペロブスカイト太陽電池", fusion: "核融合", smr: "SMR", quantum_computing: "量子コンピュータ", quantum_comm: "量子通信・暗号", quantum_sensing: "量子センシング"};
const SRC_CLASS = {arxiv: "s-arxiv", pubmed: "s-pubmed", gnews: "s-gnews", trials: "s-trials", edinet: "s-edinet", yahoo: "s-yahoo", openalex: "s-openalex", prtimes: "s-prtimes", edgar: "s-edgar", roic: "s-roic", simfin: "s-simfin"};
const SRC_LABEL = {arxiv: "arXiv", pubmed: "PubMed", gnews: "News", trials: "ClinicalTrials", edinet: "EDINET", yahoo: "Yahoo Finance", openalex: "OpenAlex", prtimes: "PR TIMES", edgar: "SEC EDGAR", roic: "ROIC.ai", simfin: "SimFin"};
const NEWS_SOURCES = new Set(["gnews", "prtimes"]);
const tagLabel = t => TL[t] || CL[t] || t;
const srcName = s => SRC_LABEL[s] || s;
// 導出 tier (tiers/v1): 基礎 watch → Look、基礎 reference → See。
// export に derived_tier が無い古い data.json では基礎 tier から読み替える
const DT = {look: "Look", see: "See"};
const dtier = c => c.derived_tier || (c.tier === "reference" ? "see" : "look");

let companies = [];
const state = {q: "", tier: "", markets: new Set(), sectors: new Set(), tags: new Set(), sort: "latest"};

// 表示言語は主ページ・詳細ページと共通 (localStorage の trl-lang)。
// 既定は日本語訳 (訳がある場合) で、スイッチで原文表示に切り替える。
// 訳が無い材料はスイッチの状態に依らず原文のみになる
let LANG = localStorage.getItem("trl-lang") || "ja";
const dispT = m => LANG === "ja" && m.title_ja ? m.title_ja : m.title;
const dispA = m => LANG === "ja" && m.abstract_ja ? m.abstract_ja : m.abstract || "";

function haystack(c) {
  const parts = [c.name, c.ticker || "", ML[c.market] || "", CL[c.sector] || "",
    ...(c.tags || []), ...(c.tags || []).map(tagLabel),
    ...(c.materials || []).flatMap(m => [m.title, m.title_ja || ""])];
  return parts.join(" ").toLowerCase();
}

async function copyText(text, btn) {
  try { await navigator.clipboard.writeText(text); }
  catch (e) {
    const ta = document.createElement("textarea");
    ta.value = text; document.body.appendChild(ta); ta.select();
    document.execCommand("copy"); ta.remove();
  }
  if (btn) { const o = btn.textContent; btn.textContent = "コピー済み"; setTimeout(() => { btn.textContent = o; }, 1200); }
}

function citeText(m) {
  return [m.title, srcName(m.source) + (m.published ? " ・ " + m.published : ""), m.url].filter(Boolean).join("\n");
}
function matText(m) {
  return [m.title, m.title_ja || "", `出典: ${srcName(m.source)} ${m.published || ""}`, m.url,
    "", m.abstract_ja || m.abstract || ""].join("\n");
}
function matMd(c, m) {
  return [`## ${m.title}`, "",
    `- 企業: ${c.name}（${ML[c.market] || c.market}・${CL[c.sector] || c.sector || ""}）`,
    `- 出典: ${srcName(m.source)}（${m.published || "日付不明"}）`,
    `- URL: ${m.url}`, "",
    m.summary ? `### 要点（抄録から抽出）\n\n${m.summary}\n` : "",
    m.abstract_ja ? `### 抄録（和訳）\n\n${m.abstract_ja}` : (m.abstract ? `### 抄録\n\n${m.abstract}` : "")].join("\n");
}

function matHtml(c, m) {
  // 主表示は表示言語 (dispT/dispA) に従い、もう一方の言語は見出し下の
  // 副題と <details> に回す。訳が無い材料はどちらでも原文のみ
  const main = dispA(m);
  const other = LANG === "ja" ? (m.abstract || "") : (m.abstract_ja || "");
  const abs = main
    ? `<p class="mabs">${LANG === "ja" && m.abstract_ja ? "<strong>和訳抄録</strong><br>" : ""}${esc(main)}</p>` +
      (other && other !== main ? `<details><summary>${LANG === "ja" ? "抄録（原文）" : "抄録（和訳）"}</summary><p class="mabs">${esc(other)}</p></details>` : "")
    : "";
  const sub = LANG === "ja" ? m.title : (m.title_ja || "");
  return `<article class="mat">
    <div class="mrow"><span class="${esc(SRC_CLASS[m.source] || "")}">${esc(srcName(m.source))}</span><span>${esc(m.published || "日付不明")}</span>${m.strength === "weak" ? "<span>・弱</span>" : ""}</div>
    <h4 class="mt">${m.url ? `<a href="${esc(safeUrl(m.url))}" target="_blank" rel="noopener">${esc(dispT(m))}</a>` : esc(dispT(m))}</h4>
    ${sub && sub !== dispT(m) ? `<p class="mja">${esc(sub)}</p>` : ""}
    ${m.summary ? `<p class="msum"><strong>要点（抄録から抽出）</strong><br>${esc(m.summary)}</p>` : ""}
    ${abs}
    <div class="mops"><button type="button" class="btn" data-ccite="${m.id}">出典コピー</button>${m.url ? `<button type="button" class="btn" data-clink="${m.id}">リンクコピー</button>` : ""}<button type="button" class="btn" data-copy="${m.id}">コピー</button><button type="button" class="btn" data-md="${m.id}">Markdown</button></div>
  </article>`;
}

function cardHtml(c) {
  const meta = [ML[c.market] || c.market, CL[c.sector] || "", STG[c.stage] || "", c.ticker || ""].filter(Boolean).join("・");
  const tags = (c.tags || []).length ? `<div class="wmeta">${c.tags.map(t => `<span class="chip">${esc(tagLabel(t))}</span>`).join(" ")}</div>` : "";
  const cnt = `一次情報 ${c.counts.primary}件・ニュース ${c.counts.news}件${c.latest ? `・最新 ${esc(c.latest)}` : ""}・未評価`;
  return `<section class="card wcard" data-id="${esc(c.id)}">
    <div class="whead"><span class="wname">${esc(c.name)}</span><span class="tbadge">${esc(DT[dtier(c)] || dtier(c))}</span></div>
    <div class="wmeta">${esc(meta)}</div>${tags}
    <div class="wmeta">${esc(cnt)}</div>
    <div class="mops"><button type="button" class="btn" data-toggle="${esc(c.id)}" aria-expanded="false">材料を見る</button></div>
    <div class="wmat" data-mat="${esc(c.id)}"></div>
  </section>`;
}

function renderMats(el, c) {
  const prim = c.materials.filter(m => !NEWS_SOURCES.has(m.source));
  const news = c.materials.filter(m => NEWS_SOURCES.has(m.source));
  let html = "";
  if (prim.length) html += `<p class="slabel">一次情報</p>` + prim.map(m => matHtml(c, m)).join("");
  if (news.length) html += `<p class="slabel">ニュース</p>` + news.map(m => matHtml(c, m)).join("");
  if (!prim.length && !news.length) html = `<p class="wmeta">材料はまだありません（次回以降の収集で集まります）。</p>`;
  if (c.counts.primary > prim.length || c.counts.news > news.length)
    html += `<p class="wmeta">表示は一次情報 ${prim.length}件・ニュース ${news.length}件まで（全件は収集プール内）。</p>`;
  el.innerHTML = html;
  el.querySelectorAll("[data-ccite]").forEach(b => b.addEventListener("click", () => {
    const m = c.materials.find(x => String(x.id) === b.dataset.ccite); if (m) copyText(citeText(m), b);
  }));
  el.querySelectorAll("[data-clink]").forEach(b => b.addEventListener("click", () => {
    const m = c.materials.find(x => String(x.id) === b.dataset.clink); if (m && m.url) copyText(m.url, b);
  }));
  el.querySelectorAll("[data-copy]").forEach(b => b.addEventListener("click", () => {
    const m = c.materials.find(x => String(x.id) === b.dataset.copy); if (m) copyText(matText(m), b);
  }));
  el.querySelectorAll("[data-md]").forEach(b => b.addEventListener("click", () => {
    const m = c.materials.find(x => String(x.id) === b.dataset.md); if (m) copyText(matMd(c, m), b);
  }));
}

function filtered() {
  const q = state.q.trim().toLowerCase();
  let rows = companies.filter(c =>
    (!state.tier || dtier(c) === state.tier) &&
    (!state.markets.size || state.markets.has(c.market)) &&
    (!state.sectors.size || state.sectors.has(c.sector)) &&
    (!state.tags.size || (c.tags || []).some(t => state.tags.has(t))) &&
    (!q || haystack(c).includes(q)));
  if (state.sort === "latest") rows.sort((a, b) => (b.latest || "") > (a.latest || "") ? 1 : (b.latest || "") < (a.latest || "") ? -1 : a.name.localeCompare(b.name, "ja"));
  else if (state.sort === "oldest") rows.sort((a, b) => (a.latest || "9999") > (b.latest || "9999") ? 1 : (a.latest || "9999") < (b.latest || "9999") ? -1 : a.name.localeCompare(b.name, "ja"));
  else if (state.sort === "count") rows.sort((a, b) => (b.counts.primary - a.counts.primary) || ((b.latest || "") > (a.latest || "") ? 1 : -1));
  else rows.sort((a, b) => a.name.localeCompare(b.name, "ja"));
  return rows;
}

function render() {
  const rows = filtered();
  const w = rows.filter(c => dtier(c) === "look").length, r = rows.length - w;
  $("#cnt").textContent = `${rows.length}社を表示（Look ${w} / See ${r}）`;
  $("#list").innerHTML = rows.length ? rows.map(cardHtml).join("") : `<p class="wmeta">条件に合う企業がありません。</p>`;
  $("#list").querySelectorAll("[data-toggle]").forEach(b => b.addEventListener("click", () => {
    const card = b.closest(".wcard"), c = companies.find(x => x.id === b.dataset.toggle);
    const open = card.classList.toggle("open");
    b.setAttribute("aria-expanded", String(open));
    b.textContent = open ? "材料を閉じる" : "材料を見る";
    const box = card.querySelector("[data-mat]");
    if (open && !box.dataset.done) { renderMats(box, c); box.dataset.done = "1"; }
  }));
}

function chips(el, items, isOn, onToggle) {
  el.innerHTML = items.map(([v, label]) => `<button type="button" class="dchip" data-v="${esc(v)}" aria-pressed="${isOn(v)}">${esc(label)}</button>`).join(" ");
  el.querySelectorAll("[data-v]").forEach(b => b.addEventListener("click", () => onToggle(b.dataset.v)));
}

async function init() {
  let data;
  try { data = await (await fetch("data.json")).json(); }
  catch (e) { $("#cnt").textContent = "データを読み込めませんでした。"; return; }
  companies = data.watch_companies || [];
  companies.forEach(c => { c._hay = haystack(c); });
  chips($("#tiers"), [["", "すべて"], ["look", "Look"], ["see", "See"]],
    v => state.tier === v,
    v => { state.tier = v; initChipsOnly(); render(); });
  const mkts = [...new Set(companies.map(c => c.market).filter(Boolean))]
    .sort((a, b) => companies.filter(c => c.market === b).length - companies.filter(c => c.market === a).length);
  chips($("#markets"), mkts.map(m => [m, ML[m] || m]),
    v => state.markets.has(v),
    v => { state.markets.has(v) ? state.markets.delete(v) : state.markets.add(v); initChipsOnly(); render(); });
  const secs = [...new Set(companies.map(c => c.sector).filter(Boolean))];
  chips($("#sectors"), secs.map(s => [s, CL[s] || s]),
    v => state.sectors.has(v),
    v => { state.sectors.has(v) ? state.sectors.delete(v) : state.sectors.add(v); initChipsOnly(); render(); });
  // タグは数が多いのでセクターごとに折り畳んで出す。同じタグが複数セクターに
  // 現れる場合は同じ選択を共有し、チップの押下状態は全グループで同期する
  const tagOrder = Object.keys(TL);
  const tagGroups = Object.keys(CL).map(s => {
    const ts = [...new Set(companies.filter(c => c.sector === s).flatMap(c => c.tags || []))]
      .sort((a, b) => tagOrder.indexOf(a) - tagOrder.indexOf(b));
    return [s, ts];
  }).filter(([, ts]) => ts.length);
  $("#tags").innerHTML = tagGroups.map(([s, ts]) =>
    `<details class="taggroup" data-sector="${esc(s)}"><summary>${esc(CL[s] || s)}（${ts.length}種）<span class="selcnt"></span></summary><div class="wrow"></div></details>`).join("");
  tagGroups.forEach(([s, ts]) => {
    const box = document.querySelector(`#tags .taggroup[data-sector="${s}"] .wrow`);
    chips(box, ts.map(t => [t, tagLabel(t)]),
      v => state.tags.has(v),
      v => { state.tags.has(v) ? state.tags.delete(v) : state.tags.add(v); initChipsOnly(); render(); });
  });
  function initChipsOnly() {
    document.querySelectorAll("#tiers .dchip").forEach(b => b.setAttribute("aria-pressed", String(state.tier === b.dataset.v)));
    document.querySelectorAll("#markets .dchip").forEach(b => b.setAttribute("aria-pressed", String(state.markets.has(b.dataset.v))));
    document.querySelectorAll("#sectors .dchip").forEach(b => b.setAttribute("aria-pressed", String(state.sectors.has(b.dataset.v))));
    document.querySelectorAll("#tags .dchip").forEach(b => b.setAttribute("aria-pressed", String(state.tags.has(b.dataset.v))));
    document.querySelectorAll("#tags .taggroup").forEach(g => {
      const n = [...g.querySelectorAll(".dchip")].filter(b => state.tags.has(b.dataset.v)).length;
      g.querySelector(".selcnt").textContent = n ? ` ・${n}件選択中` : "";
    });
  }
  initChipsOnly();
  $("#q").addEventListener("input", e => { state.q = e.target.value; render(); });
  $("#sort").addEventListener("change", e => { state.sort = e.target.value; render(); });
  const fl = $("#fl");
  fl.value = LANG;
  fl.addEventListener("change", e => {
    LANG = e.target.value === "orig" ? "orig" : "ja";
    localStorage.setItem("trl-lang", LANG);
    // 開いているカードの材料はその場で描き直す (閉じているカードは
    // 次に開くときに新しい言語で描かれる)
    document.querySelectorAll(".wcard.open [data-mat]").forEach(box => {
      const c = companies.find(x => x.id === box.dataset.mat);
      if (c) renderMats(box, c);
    });
  });
  render();
}
init();
