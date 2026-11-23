/* 減点理由の集計ページ: data.json の最新の long 評価を項目ごとに数え直す。
   null・低スコア・不足文面の頻度と、複数候補に付いた材料を出す。
   新しい判定はせず、輸出値の集計だけを行う */
"use strict";
const $ = s => document.querySelector(s);
const esc = s => String(s == null ? "" : s).replace(/[&<>"']/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
const IL = {technology_readiness: "技術成熟度", technological_advantage: "技術的優位性",
  monetization: "収益化能力", market_growth: "市場成長性",
  financial_sustainability: "財務的持続可能性", moat_durability: "競争優位性の持続性"};

function render(d) {
  const keys = Object.keys(IL);
  const agg = {};
  for (const k of keys) agg[k] = {n: 0, nulls: 0, low: 0, sum: 0, scored: 0, miss: new Map()};
  const byUrl = new Map();
  for (const p of d.candidates || []) {
    const L = p.long;
    if (L && L.status !== "error" && L.items) {
      for (const k of keys) {
        const it = L.items[k];
        if (!it) continue;
        const a = agg[k];
        a.n++;
        if (typeof it.score !== "number") a.nulls++;
        else { a.scored++; a.sum += it.score; if (it.score < 40) a.low++; }
        for (const m of it.missing_info || []) a.miss.set(m, (a.miss.get(m) || 0) + 1);
      }
    }
    const seen = new Set();
    for (const m of [...(p.related_news || []), ...(p.related_financials || [])]) {
      if (!m.url || seen.has(m.url)) continue;
      seen.add(m.url);
      const cur = byUrl.get(m.url) || {n: 0, title: m.title || ""};
      cur.n++;
      byUrl.set(m.url, cur);
    }
  }
  const rows = keys.map(k => {
    const a = agg[k];
    const top = [...a.miss.entries()].sort((x, y) => y[1] - x[1]).slice(0, 5)
      .map(([m, n]) => `${esc(m)} ×${n}`).join("<br>");
    return `<tr><td>${esc(IL[k])}</td><td>${a.n}</td><td>${a.nulls}</td><td>${a.low}</td>`
      + `<td>${a.scored ? Math.round(a.sum / a.scored * 10) / 10 : "—"}</td><td>${top || "—"}</td></tr>`;
  }).join("");
  const shared = [...byUrl.entries()].filter(([, v]) => v.n >= 3)
    .sort((a, b) => b[1].n - a[1].n).slice(0, 10);
  const sharedHtml = shared.length
    ? `<h2>複数候補に付いた材料（3候補以上）</h2><div class="tscroll"><table class="rt"><thead><tr><th>材料</th><th>候補数</th></tr></thead><tbody>`
      + shared.map(([u, v]) => `<tr><td><a href="${esc(u)}" target="_blank" rel="noopener noreferrer">${esc(v.title || u)}</a></td><td>${v.n}</td></tr>`).join("")
      + `</tbody></table></div>`
    : "";
  $("#dbody").innerHTML = `<div class="tscroll"><table class="rt"><thead><tr><th>項目</th><th>評価件数</th><th>null</th><th>低スコア (&lt;40)</th><th>平均</th><th>不足の文面（頻出）</th></tr></thead><tbody>${rows}</tbody></table></div>` + sharedHtml;
}

(async () => {
  try {
    const res = await fetch("data.json", {cache: "no-cache"});
    render(await res.json());
  } catch (e) {
    $("#dbody").innerHTML = `<p class="hint">data.json を読み込めませんでした。</p>`;
  }
})();

/* ページ移動のキーシーケンス: g の後にホームポジション周辺のキー
   (a=監査 c=採点基準 w=Look・See o=観測 q=次回評価予定 r=実行履歴 d=項目別の不足 l=用語集) */
{
  const GKEYS = {a: "audit.html", c: "criteria.html", w: "watch.html", o: "observe.html", q: "queue.html", r: "runs.html", d: "deductions.html", l: "glossary.html"};
  let gAt = 0;
  document.addEventListener("keydown", e => {
    if (e.metaKey || e.ctrlKey || e.altKey || e.isComposing) return;
    const t = e.target;
    if (t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.tagName === "SELECT")) return;
    if (gAt && Date.now() - gAt < 900 && GKEYS[e.key]) { e.preventDefault(); location.href = GKEYS[e.key]; gAt = 0; return; }
    gAt = e.key === "g" ? Date.now() : 0;
  });
}
