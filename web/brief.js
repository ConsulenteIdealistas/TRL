/* ブリーフィング: 前回このページを開いた時刻 (localStorage trl-last-visit) より
   後に生まれた評価だけを1画面にまとめる。集計は data.json の直読みと比較だけ */
"use strict";
const $ = s => document.querySelector(s);
const esc = v => String(v ?? "").replace(/[&<>"']/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
const SL = {final: "選定可能", review: "要追加調査", hold: "保留", pending: "未評価", error: "エラー", rejected: "除外"};
const candName = p => { const e = (p.entities || []).find(x => x.strength === "strong"); return e ? e.name : (p.title || "").slice(0, 24); };
const lvl = p => (p.long && p.long.trl_level && p.long.trl_level.level) ?? null;
const row = (p, meta) => `<div class="qrow"><a href="detail.html?id=${encodeURIComponent(p.id)}">${esc(candName(p))}</a><div class="qm">${meta}</div></div>`;
const empty = t => `<p class="qm">${t}</p>`;

async function load() {
  try {
    const res = await fetch("data.json", {cache: "no-cache"});
    const d = await res.json();
    const P = d.candidates || [];
    const genMs = Date.parse(d.generated_at || "") || Date.now();
    let last = 0;
    try { last = Number(localStorage.getItem("trl-last-visit") || 0) || 0; } catch (e) { last = 0; }
    const since = last || genMs - 7 * 86400000;
    $("#bsub").textContent = last
      ? `前回開いた ${new Date(last).toLocaleString("ja-JP")} 以降の変化だけをまとめます。`
      : "初めてのブリーフィングです。直近7日の変化をまとめます。";
    const at = p => Date.parse((p.long && p.long.evaluated_at) || "");
    // 新しい確定評価
    const news = P.filter(p => p.long && p.long.status === "final" && at(p) > since)
      .sort((a, b) => at(b) - at(a));
    $("#bnew").innerHTML = news.length
      ? news.map(p => row(p, `総合 ${p.long.total ?? "—"} / TRL ${lvl(p) ?? "未確定"} / ${esc((p.long.evaluated_at || "").slice(0, 10))}`)).join("")
      : empty("新しい確定評価はありません。");
    // 大きく動いた候補 (履歴の連続2点を比べ、窓の中に新しい点があるもの)
    const moved = [];
    for (const p of P) {
      const h = (p.history || []).filter(pt => pt && pt.evaluated_at);
      for (let i = 1; i < h.length; i++) {
        if (Date.parse(h[i].evaluated_at) <= since) continue;
        const a = h[i - 1], b = h[i];
        const dT = (typeof a.total === "number" && typeof b.total === "number") ? b.total - a.total : null;
        const dL = (a.trl_level != null && b.trl_level != null) ? b.trl_level - a.trl_level : null;
        if ((dT !== null && Math.abs(dT) >= 10) || (dL !== null && dL !== 0) || a.band !== b.band) {
          moved.push({p, dT, dL, at: b.evaluated_at});
          break;
        }
      }
    }
    moved.sort((x, y) => String(y.at).localeCompare(String(x.at)));
    $("#bmoved").innerHTML = moved.length
      ? moved.map(({p, dT, dL}) => row(p, `${dL ? `TRL ${dL > 0 ? "+" : ""}${dL} ` : ""}${dT !== null ? `総合 ${dT > 0 ? "+" : ""}${Math.round(dT * 10) / 10}` : ""}`)).join("")
      : empty("大きく動いた候補はありません。");
    // ピン候補の変化
    let pins = new Set();
    try { pins = new Set(JSON.parse(localStorage.getItem("trl-pins") || "[]")); } catch (e) { pins = new Set(); }
    const pinRows = P.filter(p => pins.has(p.id) && at(p) > since);
    $("#bpins").innerHTML = pinRows.length
      ? pinRows.map(p => row(p, `${esc(SL[p.display_status] || "")} / 総合 ${p.long && p.long.total != null ? p.long.total : "—"} / TRL ${lvl(p) ?? "未確定"}`)).join("")
      : empty("ピン候補に新しい評価はありません。");
    // 次回評価予定の先頭
    const byId = new Map(P.map(p => [p.id, p]));
    const q = (Array.isArray(d.next_queue) ? d.next_queue : []).filter(id => byId.has(id)).slice(0, 5);
    $("#bqueue").innerHTML = q.length
      ? q.map((id, i) => row(byId.get(id), `${i + 1} 番目 / ${esc(SL[byId.get(id).display_status] || "")}`)).join("")
      : empty("次回評価予定は今はありません。");
    // 翻訳使用量の注意
    const tu = d.translation_usage;
    const az = tu && tu.monthly && typeof tu.monthly.azure === "number" ? tu.monthly.azure : null;
    $("#bnote").innerHTML = az !== null && az >= 1500000
      ? `<h2>注意</h2><p>翻訳使用量 (Azure) が今月 ${az.toLocaleString()} 字で、上限 2,000,000 字に近づいています。</p>` : "";
    try { localStorage.setItem("trl-last-visit", String(Date.now())); } catch (e) { /* 保存できなくても表示はする */ }
  } catch (e) {
    $("#berr").textContent = "data.json を読み込めませんでした。";
  }
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
