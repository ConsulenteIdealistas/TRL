"use strict";
/* TRL Explorer: trl.py export が出力した data.json を読んで表示する(契約: schema.json)。
   評価値はすべて data.json の値をそのまま表示し、画面側で再計算・換算・平均はしない。 */
let P = [], META = null, sel = null;
let LANG = "ja";
try { LANG = localStorage.getItem("trl-lang") || "ja"; } catch (e) { /* 保存不可なら既定のまま */ }
const dispT = p => LANG === "ja" && p.title_ja ? p.title_ja : p.title;
const dispA = p => LANG === "ja" && p.abstract_ja ? p.abstract_ja : p.abstract || "";
const TR_SRC = {deepl: "DeepL", azure: "Azure", google: "Google", llm: "LLM"};
const srcOf = p => { const s = p.translation_src || {}; const used = [...new Set([p.title_ja ? s.title : null, p.abstract_ja ? s.abstract : null].filter(Boolean))]; return used.map(v => TR_SRC[v] || v).join(" / "); };
const SL = {final: "選定可能", review: "要追加調査", hold: "保留", pending: "未評価", error: "エラー", rejected: "除外"};
const SO = {final: 0, review: 1, hold: 2, pending: 3, error: 4, rejected: 5};  // おすすめ順での状態の優先度
const IL = {technology_readiness: "技術成熟度", technological_advantage: "技術的優位性", monetization: "収益化能力",
  market_growth: "市場成長性", financial_sustainability: "財務的持続可能性", moat_durability: "競争優位性の持続性"};
const $ = s => document.querySelector(s);
const esc = v => String(v ?? "").replace(/[&<>"']/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
const safeUrl = u => /^https?:\/\//i.test(String(u || "")) ? String(u) : "#";
const ok = t => Number.isInteger(t) && t >= 1 && t <= 9;
const lvl = p => (p.long && p.long.trl_level && p.long.trl_level.level) ?? null;
const SRC_CLASS = {arxiv: "s-arxiv", pubmed: "s-pubmed", gnews: "s-gnews", trials: "s-trials", edinet: "s-edinet", yahoo: "s-yahoo", openalex: "s-openalex", prtimes: "s-prtimes", edgar: "s-edgar"};
const SRC_LABEL = {arxiv: "arXiv", pubmed: "PubMed", gnews: "News", trials: "ClinicalTrials", edinet: "EDINET", yahoo: "Yahoo Finance", openalex: "OpenAlex", prtimes: "PR TIMES", edgar: "SEC EDGAR"};
const src = s => "badge " + (SRC_CLASS[s] || "");
const srcName = s => SRC_LABEL[s] || esc(s);
const stB = s => `<span class="st ${s === "final" ? "" : "st-" + s}">${SL[s] || esc(s)}</span>`;
const DL = {positive: "ポジティブ", negative: "ネガティブ", mixed: "混在"};
const ML = {JP: "日本", US: "米国", GB: "英国", CH: "スイス", DE: "ドイツ", DK: "デンマーク", SE: "スウェーデン", IT: "イタリア", CA: "カナダ", AU: "オーストラリア", ES: "スペイン", FR: "フランス", NL: "オランダ", FI: "フィンランド", NO: "ノルウェー", BE: "ベルギー"};
const MO = ["JP", "US", "GB", "CH", "DE", "DK", "SE", "IT", "CA", "AU", "ES", "FR", "NL", "FI", "NO", "BE"];  // 国・市場別の並び
const CL = {space: "宇宙", bio: "バイオ", semiconductor: "半導体", ai: "AI", robotics: "ロボティクス", energy: "エネルギー・蓄電池", quantum: "量子技術"};
const CO = ["space", "bio", "semiconductor", "ai", "robotics", "energy", "quantum"];  // 分野別の並び
const TL = {photonics: "フォトニクス", medtech: "医療機器"};  // タグ (分野と違い複数付けられる第二の分類)
const tagLabel = t => CL[t] || TL[t] || t;
const primEnt = p => p.entities.find(e => e.strength === "strong") || p.entities[0] || null;
const marketOf = p => { const e = primEnt(p); return e ? e.market || "JP" : null; };
const sectorOf = p => { const e = primEnt(p); return e ? e.sector || null : null; };
const ord = (v, list) => { const i = list.indexOf(v); return i < 0 ? list.length : i; };
const dirOf = p => (p.long && p.long.direction && p.long.direction.direction) || null;
const dirB = p => { const d = dirOf(p); return d ? `<span class="dir dir-${d}">${DL[d]}</span>` : ""; };
const jst = iso => { try { return new Date(iso).toLocaleString("ja-JP", {timeZone: "Asia/Tokyo", hour12: false}); } catch (e) { return iso; } };
const label = k => IL[k] || k;

function trl(t, d) {
  const k = ok(t);
  return `<div class="${d ? "det" : "trl"}"><div class="num ${k ? "" : "u"}"><span>TRL</span><strong>${k ? t : "未確定"}</strong></div><div class="track">${
    Array.from({length: 9}, (_, i) => `<span class="seg ${k && i < t ? "f" : ""}"></span>`).join("")}</div></div>`;
}
function inR(t, f) {
  return f === "known" ? ok(t) : f === "unknown" ? !ok(t) : f === "early" ? ok(t) && t <= 3
    : f === "middle" ? ok(t) && t >= 4 && t <= 6 : f === "late" ? ok(t) && t >= 7 : true;
}
function haystack(p) {
  const items = p.long ? Object.values(p.long.items).flatMap(i => i.missing_info) : [];
  return [p.title, p.title_ja || "", p.source, p.abstract, p.abstract_ja || "", ...(p.fields || []), ...p.entities.map(e => e.name), ...items].join(" ").toLowerCase();
}
function filtered() {
  const q = $("#q").value.trim().toLowerCase(), s = $("#fs").value, t = $("#ft").value, o = $("#fo").value, fd = $("#fd").value;
  const fm = $("#fm").value, fc = $("#fc").value, fg = $("#fg").value;
  const r = P.filter(p => (!q || haystack(p).includes(q)) && (s === "all" || p.display_status === s) && inR(lvl(p), t)
    && (fd === "all" || (fd === "none" ? !dirOf(p) : dirOf(p) === fd))
    && (fm === "all" || p.entities.some(e => (e.market || "JP") === fm))
    && (fc === "all" || p.entities.some(e => e.sector === fc))
    && (fg === "all" || p.entities.some(e => (e.tags || []).includes(fg))));
  const byStatus = (a, b) => (SO[a.display_status] - SO[b.display_status])
    || ((ok(lvl(b)) ? lvl(b) : -1) - (ok(lvl(a)) ? lvl(a) : -1))
    || String(b.published || "").localeCompare(String(a.published || ""));
  r.sort((a, b) => o === "status" ? byStatus(a, b)
    : o === "market" ? (ord(marketOf(a), MO) - ord(marketOf(b), MO)) || byStatus(a, b)
    : o === "sector" ? (ord(sectorOf(a), CO) - ord(sectorOf(b), CO)) || byStatus(a, b)
    : o === "trl" ? (ok(lvl(b)) ? lvl(b) : -1) - (ok(lvl(a)) ? lvl(a) : -1)
    : o === "trl_asc" ? (ok(lvl(a)) ? lvl(a) : 99) - (ok(lvl(b)) ? lvl(b) : 99)
    : o === "title" ? a.title.localeCompare(b.title, "ja") : String(b.published || "").localeCompare(String(a.published || "")));
  return r;
}
function facetOptions() {
  // 絞り込みの選択肢は、掲載中の候補に実際にある国・分野だけを順序つきで出す
  const fill = (sel, values, order, labels) => {
    const el = $(sel), cur = el.value;
    const present = order.filter(v => values.has(v)).concat([...values].filter(v => !order.includes(v)));
    el.innerHTML = '<option value="all">すべて</option>' + present.map(v => `<option value="${esc(v)}">${esc(labels[v] || v)}</option>`).join("");
    el.value = present.includes(cur) ? cur : "all";
  };
  fill("#fm", new Set(P.flatMap(p => p.entities.map(e => e.market || "JP"))), MO, ML);
  fill("#fc", new Set(P.flatMap(p => p.entities.map(e => e.sector).filter(Boolean))), CO, CL);
  fill("#fg", new Set(P.flatMap(p => p.entities.flatMap(e => e.tags || []))), [...CO, "photonics", "medtech"], {...CL, ...TL});
}
function sc(p, k) { const it = p.long && p.long.items[k]; return it ? it.score : null; }
function fmtScore(v) { return typeof v === "number" && isFinite(v) ? v : "—"; }

function facetText(p) {
  const m = marketOf(p), c = sectorOf(p);
  return [m ? ML[m] || m : "", c ? CL[c] || c : ""].filter(Boolean).join("・");
}
function card(p) {
  const s = p.id === sel;
  const ents = p.entities.filter(e => e.strength === "strong").length;
  const facet = facetText(p);
  return `<article class="card ${s ? "sel" : ""}" data-id="${esc(p.id)}"><div class="main"><div style="min-width:0"><div class="top"><span class="${src(p.source)}">${srcName(p.source)}</span><span class="date">${esc(p.published || "")}</span>${LANG === "ja" && srcOf(p) ? `<span class="date">訳: ${esc(srcOf(p))}</span>` : ""}</div><h3 class="title">${esc(dispT(p))}</h3><p class="abs">${esc(dispA(p))}</p><div class="tags">${(p.fields || []).slice(0, 4).map(t => `<span class="tag">${esc(t)}</span>`).join("")}</div><div class="mi"><span>技術成熟度 <strong>${fmtScore(sc(p, "technology_readiness"))}</strong></span><span>技術的優位性 <strong>${fmtScore(sc(p, "technological_advantage"))}</strong></span><span>関連企業 <strong>${ents ? ents + "社" : "未特定"}</strong></span>${facet ? `<span>国・分野 <strong>${esc(facet)}</strong></span>` : ""}</div></div><div class="side">${stB(p.display_status)}${dirB(p)}${trl(lvl(p))}</div></div><div class="foot"><span>${p.long && p.long.critical_missing.length ? `必須の不足 ${p.long.critical_missing.length}件` : p.display_status === "pending" ? (p.next_up ? "次回評価予定" : "評価待ち") : ""}</span><a class="tg" href="detail.html?id=${encodeURIComponent(p.id)}">詳細を見る →</a></div></article>`;
}
function scoreRow(k, it) {
  const v = it && typeof it.score === "number" && isFinite(it.score) ? it.score : null;
  const why = it && it.reason ? `<p>${esc(it.reason)}</p>` : "";
  const miss = it && v == null && it.missing_info.length ? `<p class="m">不足: ${it.missing_info.map(esc).join(" / ")}</p>` : "";
  const wait = it && it.awaiting_info && it.awaiting_info.length ? `<p class="w2">結果待ち: ${it.awaiting_info.map(esc).join(" / ")}</p>` : "";
  const ev = it && it.evidence.length ? `<p class="hint">根拠 ${it.evidence.length}件: ${it.evidence.slice(0, 3).map(e => esc(e.summary)).join(" / ")}</p>` : "";
  return `<div class="sc"><div class="h"><span>${esc(label(k))}</span><span class="${v == null ? "u" : ""}">${v == null ? "未算出" : v + " / 100"}</span></div><div class="bar"><i style="width:${v == null ? 0 : Math.max(0, Math.min(100, v))}%"></i></div>${why}${miss}${wait}${ev}</div>`;
}
const pts = (a, w) => a.length ? `<ul class="pts">${a.map(x => `<li><b class="${w ? "w" : ""}">${w ? "!" : "✓"}</b><span>${esc(x)}</span></li>`).join("")}</ul>` : `<p class="hint">登録された情報はありません。</p>`;

function getNote(id) { try { return localStorage.getItem("trl-note:" + id) || ""; } catch (e) { return ""; } }
function setNote(id, v) { try { localStorage.setItem("trl-note:" + id, v); return true; } catch (e) { return false; } }

function detail() {
  const p = P.find(x => x.id === sel);
  if (!p) { $("#det").innerHTML = `<div class="ph"><i>⌕</i><h3>論文が選択されていません</h3><p class="hint">検索条件を変更するか、一覧から論文を選択してください。</p></div>`; return; }
  const LL = p.long, G = META.config.item_groups;
  const L = LL && LL.status !== "error" ? LL : null;  // error は評価結果ではないので点数欄には出さない
  const tr = L && L.trl_level;
  const strong = p.entities.filter(e => e.strength === "strong");
  const missAll = L ? [...new Set(Object.values(L.items).flatMap(i => i.missing_info))] : [];
  const waitAll = L ? [...new Set(Object.values(L.items).flatMap(i => i.awaiting_info || []))] : [];
  const q = p.queue && p.queue.long;
  const total = L && L.total != null ? `${L.total} / 100(${esc(L.band || "")})` : "未確定";
  $("#det").innerHTML = `<div class="dc"><header class="dh"><div class="top"><span class="${src(p.source)}">${srcName(p.source)}</span>${stB(p.display_status)}</div><h3>${esc(dispT(p))}</h3><div class="mi"><span>公開日 ${esc(p.published || "—")}</span>${L ? `<span>評価日 ${esc(jst(L.evaluated_at))}</span>` : ""}</div></header>
<section class="ds"><h4>研究概要</h4><p>${esc(dispA(p)) || "（要旨なし）"}</p>${LANG === "ja" && srcOf(p) ? `<p class="hint">日本語訳: ${esc(srcOf(p))}</p>` : ""}</section>
<section class="ds"><h4>技術成熟度（TRL）</h4>${trl(lvl(p), 1)}${tr && tr.reason ? `<p>${esc(tr.reason)}</p>` : ""}${tr && tr.level == null && tr.missing_info.length ? `<p class="hint">不足: ${tr.missing_info.map(esc).join(" / ")}</p>` : ""}${tr && tr.awaiting_info && tr.awaiting_info.length ? `<p class="hint">結果待ち: ${tr.awaiting_info.map(esc).join(" / ")}</p>` : ""}<p class="hint">TRLは資料から根拠付きで判断できた場合のみ表示します。技術成熟度スコアからの換算はしません。</p></section>
<section class="ds"><h4>技術評価</h4>${L ? G.technology.map(k => scoreRow(k, L.items[k])).join("") : `<p class="hint">未評価です。</p>`}</section>
<section class="ds"><h4>事業評価</h4>${L && L.business_stage === "pre_stage" ? `<p class="note">この技術は前商業段階(TRL 3以下)のため、事業項目は「段階未達」として不足とは区別しています。点が付いている項目はそのまま表示します。</p>` : ""}${L ? G.business.map(k => scoreRow(k, L.items[k])).join("") : `<p class="hint">未評価です。</p>`}${L && !strong.length ? `<p class="note">関連企業が特定できていないため、企業固有の項目は算出されません。総合点は6項目すべてが揃うまで確定せず、重みの再配分もしません。</p>` : ""}</section>
<section class="ds"><h4>総合点</h4><p>${total}</p>${L && L.total_scope === "technology" ? `<p class="note">これは技術評価の総合点です。事業項目は段階未達のため対象外で、技術軸の項目だけで算出しています。</p>` : ""}</section>
<section class="ds"><h4>材料の方向性</h4>${L && L.direction ? `<p>${dirB(p)} ${esc(L.direction.reason || "")}</p><p class="hint">方向性は点数やTRLとは独立に、資料が示す出来事の追い風・逆風を判定したものです。</p>` : `<p class="hint">未判定です（方向を決める出来事が資料に無いか、旧基準の評価です）。</p>`}</section>
<section class="ds"><h4>不足している観点</h4>${L && L.critical_missing.length ? `<p class="m" style="color:#a96500;font-weight:700">必須項目の不足: ${L.critical_missing.map(k => esc(label(k))).join("、")}</p>` : ""}${pts(missAll, 1)}${waitAll.length ? `<p class="hint">結果待ち（不足ではなく、治験の結果公表など時間の経過でしか解けないもの）:</p><ul class="pts">${waitAll.map(x => `<li><b>◷</b><span>${esc(x)}</span></li>`).join("")}</ul>` : ""}${q && q.next_sources.length ? `<p class="hint">次に確認する情報源: ${q.next_sources.map(esc).join(" / ")}</p>` : ""}${q && q.reeval_conditions.length ? `<p class="hint">再評価の条件: ${q.reeval_conditions.map(esc).join(" / ")}</p>` : ""}</section>
${p.display_status === "error" && LL && LL.errors.length ? `<section class="ds"><h4>処理上の注意</h4>${pts(LL.errors, 1)}</section>` : ""}
<section class="ds"><h4>関連企業</h4>${p.entities.map(e => `<span class="chip">${esc(e.name)}${e.product_name ? "（" + esc(e.product_name) + "）" : ""}${e.market ? "・" + esc(ML[e.market] || e.market) : ""}${e.sector ? "・" + esc(CL[e.sector] || e.sector) : ""}${(e.tags || []).length ? "・" + e.tags.map(t => esc(tagLabel(t))).join("・") : ""} ${e.strength === "strong" ? "" : "・弱"}${e.tier === "watch" ? "・ウォッチ" : ""}</span>`).join("") || '<span class="hint">関連企業は未特定です。</span>'}</section>
<section class="ds"><h4>調査メモ</h4><textarea id="note" aria-label="調査メモ" placeholder="仮説、確認したい点、次の調査などを記録...">${esc(getNote(p.id))}</textarea><div class="acts"><button class="btn sec" type="button" id="save">メモを保存</button><a class="btn src" href="${esc(safeUrl(p.url))}" target="_blank" rel="noopener noreferrer">原文サイトを開く ↗</a></div><p id="msg" class="hint" aria-live="polite">メモはこの端末のブラウザにだけ保存されます。</p></section></div>`;
}
function render() {
  const r = filtered();
  $("#rc").textContent = r.length;
  $("#empty").hidden = r.length !== 0;
  if (!r.some(p => p.id === sel)) sel = r[0] ? r[0].id : null;
  $("#list").innerHTML = r.map(card).join("");
  detail();
}
function stats() {
  const n = f => P.filter(p => f.includes(p.display_status)).length;
  $("#c0").textContent = P.length;
  $("#c1").textContent = n(["final"]);
  $("#c2").textContent = n(["review", "hold"]);
  $("#c3").textContent = n(["pending", "error"]);
}
function showError(msg) {
  $("#list").innerHTML = "";
  $("#det").innerHTML = "";
  $("#gen").textContent = "データなし";
  $("#empty").textContent = msg;
  $("#empty").hidden = false;
}
function warn(d) {
  const w = [];
  const run = d.run;
  if (run && run.llm_degraded) w.push("直近の実行ではLLM APIの失敗が続いたため、評価を途中で打ち切りました。未評価の論文は次回の実行で再評価されます。");
  if (run && run.status === "failed") w.push("直近の実行は、すべての情報源の取得に失敗しました。");
  const el = $("#warn");
  el.hidden = !w.length;
  el.textContent = w.join(" ");
}
async function load() {
  let d;
  try {
    const res = await fetch("data.json", {cache: "no-cache"});
    if (!res.ok) throw new Error("HTTP " + res.status);
    d = await res.json();
  } catch (e) {
    showError("data.json を読み込めません。実行(Run workflow)が完了し、ページが配備されているか確認してください。");
    return;
  }
  if (d.schema_version !== 3) { showError("data.json の形式(schema_version)がこの画面に対応していません。"); return; }
  META = d; P = d.candidates;
  $("#gen").textContent = "更新 " + jst(d.generated_at);
  $("#fl").value = LANG;
  facetOptions(); warn(d); stats(); render();
}
$("#sf").addEventListener("submit", e => { e.preventDefault(); render(); });
$("#q").addEventListener("input", render);

// 検索サジェスト: 企業名・キーワード・タイトルから補完候補を出す (2文字から)
const sugBox = $("#sug");
let sugItems = [], sugIdx = -1;
function sugIndex() {
  const comp = new Set(), kw = new Set(), ttl = [];
  for (const p of P) {
    for (const e of p.entities || []) if (e.name) comp.add(e.name);
    for (const f of p.fields || []) kw.add(f);
    if (p.title) ttl.push(p.title);
    if (p.title_ja) ttl.push(p.title_ja);
  }
  return { comp: [...comp], kw: [...kw], ttl: [...new Set(ttl)] };
}
function sugMatch(list, q) {
  const starts = [], incl = [];
  for (const v of list) {
    const lv = v.toLowerCase();
    if (lv.startsWith(q)) starts.push(v); else if (lv.includes(q)) incl.push(v);
  }
  return starts.concat(incl);
}
function hideSug() { sugBox.hidden = true; sugItems = []; sugIdx = -1; }
function showSug() {
  clearTimeout(sugHideTimer);
  const q = $("#q").value.trim().toLowerCase();
  if (q.length < 2 || !P.length) { hideSug(); return; }
  const ix = sugIndex();
  const groups = [["企業", sugMatch(ix.comp, q)], ["キーワード", sugMatch(ix.kw, q)], ["タイトル", sugMatch(ix.ttl, q)]];
  sugItems = [];
  for (const [t, vs] of groups) for (const v of vs) { if (sugItems.length < 8) sugItems.push({ t, v }); }
  if (!sugItems.length) { hideSug(); return; }
  sugIdx = -1;
  sugBox.innerHTML = sugItems.map((it, i) => `<button type="button" class="sug-i" data-i="${i}"><span class="sug-t">${esc(it.t)}</span><span class="sug-x">${esc(it.v)}</span></button>`).join("");
  sugBox.hidden = false;
}
function pickSug(i) {
  const it = sugItems[i]; if (!it) return;
  $("#q").value = it.v; hideSug(); render(); $("#q").focus();
}
$("#q").addEventListener("input", showSug);
// 日本語入力: 変換中は composing を立て、確定した後に少し遅らせて候補を出し直す。
// 確定の前後で blur 側のタイマーが候補を消してしまうことがあるため、
// showSug 側でタイマーを解除し、フォーカスが戻ったら候補も戻す。
let composing = false, sugHideTimer = null;
$("#q").addEventListener("compositionstart", () => { composing = true; });
$("#q").addEventListener("compositionend", () => { composing = false; setTimeout(showSug, 30); });
$("#q").addEventListener("focus", () => { clearTimeout(sugHideTimer); showSug(); });
// タップは pointerdown で受ける: mousedown だとスマホでは入力欄の blur とキーボードの
// 閉動作によるレイアウト移動が先に起き、候補へのタップがずれてしまうため。
sugBox.addEventListener("pointerdown", e => { const b = e.target.closest("[data-i]"); if (b) { e.preventDefault(); pickSug(+b.dataset.i); } });
$("#q").addEventListener("keydown", e => {
  if (composing || e.isComposing) return;  // 変換中のキー操作は IME に任せる
  if (sugBox.hidden || !sugItems.length) return;
  if (e.key === "ArrowDown" || e.key === "ArrowUp") {
    e.preventDefault();
    sugIdx = (sugIdx + (e.key === "ArrowDown" ? 1 : sugItems.length - 1)) % sugItems.length;
    sugBox.querySelectorAll(".sug-i").forEach((el, i) => el.classList.toggle("on", i === sugIdx));
  } else if (e.key === "Enter" && sugIdx >= 0) { e.preventDefault(); pickSug(sugIdx); }
  else if (e.key === "Escape") hideSug();
});
$("#q").addEventListener("blur", () => { sugHideTimer = setTimeout(hideSug, 120); });
$("#sf").addEventListener("submit", hideSug);
["#fs", "#ft", "#fo", "#fd", "#fm", "#fc"].forEach(s => $(s).addEventListener("change", render));
$("#fl").addEventListener("change", () => { LANG = $("#fl").value; try { localStorage.setItem("trl-lang", LANG); } catch (e) { /* 保存できなくても切替は効く */ } render(); });
$("#rs").addEventListener("click", () => { $("#q").value = ""; $("#fs").value = "all"; $("#ft").value = "all"; $("#fo").value = "status"; $("#fd").value = "all"; $("#fm").value = "all"; $("#fc").value = "all"; sel = P[0] ? P[0].id : null; render(); });
$("#list").addEventListener("click", e => { const c = e.target.closest("[data-id]"); if (c) { sel = c.dataset.id; render(); } });
$("#det").addEventListener("click", e => {
  if (e.target.closest("#save")) {
    const saved = setNote(sel, $("#note").value);
    $("#msg").textContent = saved ? "この端末のブラウザに保存しました。" : "保存できませんでした(ブラウザの保存機能が無効の可能性があります)。";
  }
});
load();
