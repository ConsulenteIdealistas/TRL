/* 次回評価予定ページ: data.json の next_queue (選定順の ID 列) をその順で全件出し、
   各候補の再評価理由・次に集める材料・再評価の条件 (queue.long) を併記する。
   並び替えや再計算はしない (輸出された選定順が正) */
"use strict";
const $ = s => document.querySelector(s);
const esc = v => String(v ?? "").replace(/[&<>"']/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
const SL = {final: "選定可能", review: "要追加調査", hold: "保留", pending: "未評価", error: "エラー", rejected: "除外"};
const dispT = p => p.title_ja ? p.title_ja : p.title;
const candName = p => { const e = (p.entities || []).find(x => x.strength === "strong"); return e ? e.name : ""; };

async function load() {
  try {
    const res = await fetch("data.json", {cache: "no-cache"});
    const d = await res.json();
    const byId = new Map((d.candidates || []).map(p => [p.id, p]));
    const q = Array.isArray(d.next_queue) ? d.next_queue : [];
    const rows = q.filter(id => byId.has(id)).map((id, i) => {
      const p = byId.get(id), L = (p.queue && p.queue.long) || null, nm = candName(p);
      return `<div class="qrow"><div class="qt">${i + 1}. <a href="detail.html?id=${encodeURIComponent(p.id)}">${esc(dispT(p))}</a></div>`
        + `<div class="qm">${esc(SL[p.display_status] || p.display_status || "")}${nm ? ` / 企業: ${esc(nm)}` : ""}${p.eval_count ? ` / 評価 ${p.eval_count} 回目` : " / 初回評価"}`
        + (L && L.reason ? `<br>再評価の理由: ${esc(L.reason)}` : "")
        + (L && L.next_sources ? `<br>次に集める材料: ${esc(L.next_sources)}` : "")
        + (L && L.reeval_conditions ? `<br>再評価の条件: ${esc(L.reeval_conditions)}` : "")
        + `</div></div>`;
    });
    $("#qlist").innerHTML = rows.length ? rows.join("") : "<p>次回評価予定は今はありません。</p>";
  } catch (e) {
    $("#qerr").textContent = "data.json を読み込めませんでした。";
  }
}
load();

/* ページ移動のキーシーケンス: g の後にホームポジション周辺のキー
   (a=監査 c=採点基準 w=Look・See o=観測 q=次回評価予定 r=実行履歴 d=項目別の不足 l=用語集) */
{
  const GKEYS = {b: "brief.html", a: "audit.html", c: "criteria.html", w: "watch.html", o: "observe.html", q: "queue.html", r: "runs.html", d: "deductions.html", l: "glossary.html"};
  let gAt = 0;
  document.addEventListener("keydown", e => {
    if (e.metaKey || e.ctrlKey || e.altKey || e.isComposing) return;
    const t = e.target;
    if (t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.tagName === "SELECT")) return;
    if (gAt && Date.now() - gAt < 900 && GKEYS[e.key]) { e.preventDefault(); location.href = GKEYS[e.key]; gAt = 0; return; }
    gAt = e.key === "g" ? Date.now() : 0;
  });
}
