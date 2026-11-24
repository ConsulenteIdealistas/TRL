/* 実行履歴ページ: data.json の run_history (直近30ラン) と source_stats
   (基準ランのソース別集計) をそのまま表にする。率は2つの輸出値の
   割り算だけで、新しい集計はしない */
"use strict";
const $ = s => document.querySelector(s);
const esc = s => String(s == null ? "" : s).replace(/[&<>"']/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
const pct = (a, b) => (typeof a === "number" && typeof b === "number" && b > 0) ? Math.round(a / b * 100) + "%" : "—";
const num = v => typeof v === "number" ? v : "—";

function rateGeom(hist, W = 560, H = 90) {
  const rates = (hist || []).map(r => {
    const c = r.counts || {}, ok = c.sources_ok, ng = c.sources_failed;
    return (typeof ok === "number" && typeof ng === "number" && ok + ng > 0) ? ok / (ok + ng) : null;
  }).filter(v => v !== null);
  return rates.map((v, i) => ({
    x: rates.length <= 1 ? Math.round(W / 2) : Math.round(i / (rates.length - 1) * (W - 6) + 3),
    y: Math.round((H - 6) - v * (H - 16)), rate: v}));
}

/* 定期実行の予定 (app.js と同じ定義の複製): 火〜金 06:30 JST */
function nextRuns(fromMs, n) {
  const out = [];
  const JST = 9 * 3600000;
  const d = new Date(fromMs + JST);
  const base = Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate());
  for (let i = 0; out.length < n && i < 21; i++) {
    const day = new Date(base + i * 86400000);
    const wd = day.getUTCDay();
    if (wd >= 2 && wd <= 5) {
      const t = base + i * 86400000 + 6.5 * 3600000 - JST;
      if (t > fromMs) out.push(t);
    }
  }
  return out;
}
const fmtRunAt = t => new Date(t).toLocaleString("ja-JP", {timeZone: "Asia/Tokyo", month: "numeric", day: "numeric", weekday: "short", hour: "2-digit", minute: "2-digit"});

function render(d) {
  const hist = d.run_history || [], ss = d.source_stats || [];
  if (!hist.length && !ss.length) { $("#rbody").innerHTML = `<p class="hint">実行履歴はまだ data.json に含まれていません（次の発行から載ります）。</p>`; return; }
  const pts = rateGeom(hist);
  const chart = pts.length > 1 ? `<h2>収集の成功率の推移</h2><svg viewBox="0 0 560 90" width="100%" height="90" role="img" style="max-width:560px"><polyline points="${pts.map(p => p.x + "," + p.y).join(" ")}" fill="none" stroke="var(--accent)"/>${pts.map(p => `<circle cx="${p.x}" cy="${p.y}" r="2.5" fill="var(--accent)"/>`).join("")}</svg>` : "";
  const rows = [...hist].reverse().map(r => {
    const c = r.counts || {};
    return `<tr><td>${esc((r.as_of || "").slice(0, 10))}</td><td>${esc(r.status || "")}</td>`
      + `<td>${r.duration_sec ? Math.round(r.duration_sec / 60) + "分" : "—"}</td>`
      + `<td>${num(c.sources_ok)} / ${num(c.sources_failed)}</td><td>${pct(c.sources_ok, (c.sources_ok || 0) + (c.sources_failed || 0))}</td>`
      + `<td>${num(c.fetched)}</td><td>${num(c.new)}</td>`
      + `<td>${num(c.final)} / ${num(c.hold)} / ${num(c.error)}</td><td>${num(c.translated)}</td></tr>`;
  }).join("");
  const table = hist.length ? `<h2>実行の一覧（新しい順）</h2><div class="tscroll"><table class="rt"><thead><tr><th>日付</th><th>状態</th><th>所要</th><th>収集 成功/失敗</th><th>成功率</th><th>取得</th><th>新規</th><th>評価 確定/保留/エラー</th><th>翻訳数</th></tr></thead><tbody>${rows}</tbody></table></div>` : "";
  const srows = ss.map(s => `<tr><td>${esc(s.source)}${(s.top_errors || []).length ? `<br><span class="hint">${s.top_errors.map(e => `${esc(e.error)} ×${e.count}`).join(" / ")}</span>` : ""}</td><td>${s.calls}</td><td>${pct(s.calls - s.ok_calls, s.calls)}</td><td>${s.fetched}</td><td>${s.new}</td><td>${s.duplicates}</td><td>${s.invalid}</td><td>${Math.round(s.seconds)}</td></tr>`).join("");
  const stable = ss.length ? `<h2>ソース別（基準ラン ${(d.run && d.run.as_of) || ""}）</h2><div class="tscroll"><table class="rt"><thead><tr><th>ソース</th><th>呼び出し</th><th>失敗率</th><th>取得</th><th>新規</th><th>重複</th><th>無効</th><th>秒</th></tr></thead><tbody>${srows}</tbody></table></div>` : "";
  const sched = nextRuns(Date.now(), 4);
  const schedHtml = sched.length ? `<h2>次回の実行予定</h2><p>${sched.map(t => esc(fmtRunAt(t)) + " JST").join(" / ")}</p><p class="hint">定期実行は火〜金 06:30 JST（火曜のみ深掘り）。GitHub のスケジューラの都合で数時間遅れることがあります。</p>` : "";
  $("#rbody").innerHTML = schedHtml + chart + table + stable;
}

(async () => {
  try {
    const res = await fetch("data.json", {cache: "no-cache"});
    render(await res.json());
  } catch (e) {
    $("#rbody").innerHTML = `<p class="hint">data.json を読み込めませんでした。</p>`;
  }
})();

/* ページ移動のキーシーケンス: g の後にホームポジション周辺のキー
   (a=監査 c=採点基準 w=Look・See o=観測 q=次回評価予定 r=実行履歴 d=項目別の不足 l=用語集) */
{
  const GKEYS = {b: "brief.html", a: "audit.html", c: "criteria.html", w: "watch.html", o: "observe.html", q: "queue.html", r: "runs.html", d: "deductions.html", l: "glossary.html", k: "keys.html"};
  let gAt = 0;
  document.addEventListener("keydown", e => {
    if (e.metaKey || e.ctrlKey || e.altKey || e.isComposing) return;
    const t = e.target;
    if (t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.tagName === "SELECT")) return;
    if (gAt && Date.now() - gAt < 900 && GKEYS[e.key]) { e.preventDefault(); location.href = GKEYS[e.key]; gAt = 0; return; }
    gAt = e.key === "g" ? Date.now() : 0;
  });
}
