/* 横並び比較: URL の ids (候補 ID) で指定した候補の最新の long 評価を
   行列に並べる。値は data.json の直読みで、行ごとの最大値だけを太字にする */
"use strict";
const $ = s => document.querySelector(s);
const esc = s => String(s == null ? "" : s).replace(/[&<>"']/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
let P = [], META = {}, IDS = [];
const MAXN = 4;

const nameOf = p => { const e = (p.entities || []).find(x => x.strength === "strong"); return e ? e.name : (p.title || "").slice(0, 40); };
const itemKeys = () => {
  const g = (META.config && META.config.item_groups) || {};
  return [...(g.technology || ["technology_readiness", "technological_advantage"]),
    ...(g.business || ["monetization", "market_growth", "financial_sustainability", "moat_durability"])];
};
const IL = {technology_readiness: "技術成熟度", technological_advantage: "技術的優位性", monetization: "収益化能力",
  market_growth: "市場成長性", financial_sustainability: "財務的持続可能性", moat_durability: "競争優位性の持続性"};
const label = k => IL[k] || k;

function radarGeom(vals, R = 52) {
  return vals.map((v, i) => {
    const a = -Math.PI / 2 + i * Math.PI / 3, r = Math.max(0, Math.min(100, v)) / 100 * R;
    return [Math.round((70 + r * Math.cos(a)) * 10) / 10, Math.round((70 + r * Math.sin(a)) * 10) / 10];
  });
}
const COLORS = ["var(--accent)", "#e08a00", "#3f9e4d", "#8a5fd0"];

function render() {
  const sel = IDS.map(id => P.find(p => p.id === id)).filter(Boolean);
  const keys = itemKeys();
  if (!sel.length) { $("#cbody").innerHTML = `<p class="hint">上の検索から候補を追加してください。</p>`; return; }
  const rowOf = (lab, get, fmt) => {
    const vals = sel.map(get);
    const nums = vals.filter(v => typeof v === "number");
    const best = nums.length > 1 ? Math.max(...nums) : null;
    return `<tr><td>${esc(lab)}</td>${vals.map(v => `<td class="${typeof v === "number" && v === best ? "best" : ""}">${v == null ? "—" : esc(fmt ? fmt(v) : v)}</td>`).join("")}</tr>`;
  };
  const head = `<tr><th></th>${sel.map(p => `<th><a href="detail.html?id=${encodeURIComponent(p.id)}">${esc(nameOf(p))}</a> <button class="chip" data-rm="${esc(p.id)}" title="比較から外す">×</button></th>`).join("")}</tr>`;
  const rows = [
    rowOf("状態", p => p.display_status),
    rowOf("総合点", p => p.long && typeof p.long.total === "number" ? p.long.total : null),
    rowOf("帯域", p => p.long && p.long.band ? p.long.band.label : null),
    rowOf("TRL", p => p.long && p.long.trl_level ? p.long.trl_level.level : null),
    rowOf("方向性", p => p.long && p.long.direction ? p.long.direction.direction : null),
    rowOf("評価日", p => p.long ? (p.long.evaluated_at || "").slice(0, 10) : null),
    rowOf("評価回数", p => p.eval_count || null),
    ...keys.map(k => rowOf(label(k), p => p.long && p.long.items && p.long.items[k] ? p.long.items[k].score : null)),
  ].join("");
  const radars = sel.map((p, i) => {
    const vals = keys.map(k => p.long && p.long.items && p.long.items[k] ? p.long.items[k].score : null);
    if (vals.some(v => typeof v !== "number")) return "";
    const grid = [50, 100].map(gv => `<polygon points="${radarGeom(keys.map(() => gv)).map(pt => pt.join(",")).join(" ")}" fill="none" stroke="var(--line)"/>`).join("");
    return `<span style="display:inline-block;text-align:center;margin:0 8px 8px 0"><svg viewBox="0 0 140 140" width="140" height="140" role="img">${grid}<polygon points="${radarGeom(vals).map(pt => pt.join(",")).join(" ")}" fill="${COLORS[i % 4]}" fill-opacity="0.2" stroke="${COLORS[i % 4]}" stroke-width="1.5"/></svg><br><small>${esc(nameOf(p))}</small></span>`;
  }).join("");
  $("#cbody").innerHTML = `<div class="tscroll"><table class="rt"><thead>${head}</thead><tbody>${rows}</tbody></table></div>` + radars;
  document.querySelectorAll("[data-rm]").forEach(b => b.addEventListener("click", () => {
    IDS = IDS.filter(x => x !== b.dataset.rm);
    history.replaceState(null, "", "compare.html" + (IDS.length ? "?ids=" + IDS.map(encodeURIComponent).join(",") : ""));
    render();
  }));
}

function picks() {
  const q = $("#q").value.trim().toLowerCase();
  if (!q) { $("#picks").innerHTML = ""; return; }
  const hits = P.filter(p => !IDS.includes(p.id) && (p.title || "").toLowerCase().includes(q)).slice(0, 8);
  $("#picks").innerHTML = hits.map(p => `<button class="pick" data-add="${esc(p.id)}">${esc(nameOf(p))} — ${esc((p.title || "").slice(0, 60))}</button>`).join("");
  document.querySelectorAll("[data-add]").forEach(b => b.addEventListener("click", () => {
    if (IDS.length >= MAXN) return;
    IDS.push(b.dataset.add);
    history.replaceState(null, "", "compare.html?ids=" + IDS.map(encodeURIComponent).join(","));
    $("#q").value = ""; $("#picks").innerHTML = ""; render();
  }));
}

(async () => {
  try {
    const res = await fetch("data.json", {cache: "no-cache"});
    const d = await res.json();
    META = d; P = d.candidates || [];
    IDS = (new URLSearchParams(location.search).get("ids") || "").split(",").filter(Boolean).slice(0, MAXN);
    $("#q").addEventListener("input", picks);
    render();
  } catch (e) {
    $("#cbody").innerHTML = `<p class="hint">data.json を読み込めませんでした。</p>`;
  }
})();

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
