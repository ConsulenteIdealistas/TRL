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
const SR = {abstract_too_short: "要旨が短いため評価対象外", published_after_as_of: "実行基準日より後の公表のため対象外"};  // 除外 (screening) の理由
const SO = {final: 0, review: 1, hold: 2, pending: 3, error: 4, rejected: 5};  // おすすめ順での状態の優先度
const IL = {technology_readiness: "技術成熟度", technological_advantage: "技術的優位性", monetization: "収益化能力",
  market_growth: "市場成長性", financial_sustainability: "財務的持続可能性", moat_durability: "競争優位性の持続性",
  management_execution: "経営陣・実行力"};
const ACH = {A1: "商用展開とTRLの混同", A2: "TRL 9 の継続証拠", A3: "TRLからの推論", A4: "TRL 9 の根拠が商用展開のみ",
  A5: "点と理由の整合", A6: "欠落判定（材料プール外）", A7: "減点理由の具体性", A8: "反証の欠落",
  A9: "根拠のない高得点", A10: "時点の不整合", A11: "数値の原文照合", A12: "出典リンクの形式",
  A13: "総合点の再導出照合", A14: "企業帰属（弱いリンクのみ）", A15: "上限の観測",
  A16: "TRLレベルと点数帯域の乖離", A17: "単一出所の高得点",
  E1: "価格反応との整合", E2: "反応データ未使用",
  P1: "TRL 9 到達の可能性", P2: "保留のままの豊富な事実", P3: "根拠が厚いのに低得点",
  P4: "未採点なのに根拠あり", P5: "企業項目の未採点", P6: "反応がないのに織り込み済み",
  CR1: "DOI の実在確認", CT1: "治験ステータス（中止系）の反証", CT2: "治験スポンサーの帰属"};
const AIL = {counter_points: "反証", candidate: "候補の時点", total: "総合点", companies: "企業帰属",
  not_priced_in: "未織り込み度", trl_level: "TRL判定"};
function auditGroupHtml(blocks, heading, link) {
  let nf = 0, ng = 0;
  const rows = [];
  for (const a of blocks) for (const e of a.entries || []) {
    if (e.verdict === "gray") ng++; else nf++;
    rows.push(`<li><b>${esc(e.check || "")}</b> ${e.verdict === "gray" ? "グレー" : "要確認"}: ${esc(ACH[e.check] || "")}${e.item ? `（${esc(AIL[e.item] || IL[e.item] || e.item)}）` : ""}</li>`);
  }
  const head = heading ? `<p><b>${esc(heading)}</b></p>` : "";
  if (!rows.length) return `${head}<p class="hint">監査で指摘はありませんでした。</p>`;
  return `${head}<p>要確認 ${nf} 件 / グレー ${ng} 件</p><ul class="pts">${rows.join("")}</ul><div class="acts"><a class="btn sec" href="${link}">監査ページでこの記事の指摘を見る →</a></div>`;
}
function auditSectionHtml(p) {
  const hasL = p.audit && p.audit.length, hasE = p.audit_event && p.audit_event.length;
  if (!hasL && !hasE) return "";
  let h = `<section class="ds"><h4>監査</h4>`;
  if (hasL) h += auditGroupHtml(p.audit, hasE ? "長期評価" : null, `audit.html?c=${encodeURIComponent(p.id)}`);
  if (hasE) h += auditGroupHtml(p.audit_event, "イベント評価", `audit.html?c=${encodeURIComponent(p.id)}&k=event`);
  return h + "</section>";
}
const $ = s => document.querySelector(s);
const esc = v => String(v ?? "").replace(/[&<>"']/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
const safeUrl = u => /^https?:\/\//i.test(String(u || "")) ? String(u) : "#";
const ok = t => Number.isInteger(t) && t >= 1 && t <= 9;
const lvl = p => (p.long && p.long.trl_level && p.long.trl_level.level) ?? null;
const SRC_CLASS = {arxiv: "s-arxiv", pubmed: "s-pubmed", gnews: "s-gnews", trials: "s-trials", edinet: "s-edinet", yahoo: "s-yahoo", openalex: "s-openalex", prtimes: "s-prtimes", edgar: "s-edgar"};
const SRC_LABEL = {arxiv: "arXiv", pubmed: "PubMed", gnews: "News", trials: "ClinicalTrials", edinet: "EDINET", yahoo: "Yahoo Finance", openalex: "OpenAlex", prtimes: "PR TIMES", edgar: "SEC EDGAR", roic: "ROIC.ai"};
const src = s => "badge " + (SRC_CLASS[s] || "");
const srcName = s => SRC_LABEL[s] || esc(s);
const stB = s => `<span class="st ${s === "final" ? "st-final" : "st-" + esc(s)}">${SL[s] || esc(s)}</span>`;
const DL = {positive: "ポジティブ", negative: "ネガティブ", mixed: "混在"};
const ML = {JP: "日本", US: "米国", GB: "英国", CH: "スイス", DE: "ドイツ", DK: "デンマーク", SE: "スウェーデン", IT: "イタリア", CA: "カナダ", AU: "オーストラリア", ES: "スペイン", FR: "フランス", NL: "オランダ", FI: "フィンランド", NO: "ノルウェー", BE: "ベルギー", IN: "インド", SG: "シンガポール", NZ: "ニュージーランド", PL: "ポーランド", AT: "オーストリア", TW: "台湾", HK: "香港", IE: "アイルランド", PT: "ポルトガル", CZ: "チェコ", GR: "ギリシャ"};
const MO = ["JP", "US", "GB", "CH", "DE", "DK", "SE", "IT", "CA", "AU", "ES", "FR", "NL", "FI", "NO", "BE", "IN", "SG", "NZ", "PL", "AT"];  // 国・市場別の並び
const CL = {space: "宇宙", bio: "バイオ", semiconductor: "半導体", ai: "AI", robotics: "ロボティクス", energy: "エネルギー・蓄電池", quantum: "量子技術", defense: "防衛"};
const CO = ["space", "bio", "semiconductor", "ai", "robotics", "energy", "quantum"];  // 分野別の並び
const TL = {photonics: "フォトニクス", medtech: "医療機器", earth_observation: "地球観測", constellation: "衛星コンステレーション", space_transport: "宇宙輸送", lunar: "月面開発", debris_removal: "デブリ除去", mrna: "mRNA", cell_therapy: "細胞治療", gene_therapy: "遺伝子治療・ゲノム編集", antibody: "抗体医薬", radiopharm: "放射性医薬品", vaccine: "ワクチン", regenerative_medicine: "再生医療", diagnostics: "診断", drug_ai: "創薬AI", surgical_robot: "手術ロボット", power_semi: "パワー半導体", advanced_packaging: "先端パッケージング", memory: "メモリ", semi_equipment: "半導体製造装置", ai_infra: "AIインフラ・データセンター", voice_ai: "音声AI", industrial_robotics: "産業用ロボット・自動化", warehouse_automation: "倉庫・物流自動化", autonomous_mobility: "自律移動・ドローン", humanoid: "ヒューマノイド", hydrogen: "水素・燃料電池", ess: "蓄電・ESS", ev_charging: "EV充電", solid_state_battery: "全固体電池", battery_materials: "電池材料", perovskite: "ペロブスカイト太陽電池", fusion: "核融合", smr: "SMR", quantum_computing: "量子コンピュータ", quantum_comm: "量子通信・暗号", quantum_sensing: "量子センシング"};  // タグ (分野と違い複数付けられる第二の分類)
const STG = {rd: "研究開発型", demo: "実証型", production: "量産型", commercial: "商業化型"};  // 企業の段階 (表示・分析専用)
// 導出 tier (tiers/v1) の表示名。W-1 で Core も含め全層を対称に併記する。
// export に無い古い data.json では基礎 tier から読み替える
// (watch → Look、reference → See、それ以外は Core)
const DTL = {core: "Core", conditional: "Conditional", look: "Look", see: "See"};
const dtierOf = e => e.derived_tier || (e.tier === "watch" ? "look" : e.tier === "reference" ? "see" : "core");
const dtLabel = e => DTL[dtierOf(e)] || "";
// Conditional は層名に未充足の理由 (導出の detail 文そのまま) を併記する (W-2)。
// derived_reasons が配列でない形 (将来の部分スキーマ変更等) でも例外にせず、
// 理由の併記だけを落として層名は出す (1社の壊れた値で一覧全体を止めない)
const dtText = e => {
  const l = dtLabel(e);
  if (!l || dtierOf(e) !== "conditional") return l;
  const rs = (Array.isArray(e.derived_reasons) ? e.derived_reasons : []).filter(Boolean);
  return rs.length ? `Conditional（${rs.join("／")}）` : l;
};
// 関門 (barriers/v1) の表示名・定義。trlcore/barriers.py の定義が正本で、
// ここはその転記 (条件を変えるときは barriers.py のルール版を上げて同期させる)
const BRL = {devil_river: "Devil River", valley_of_death: "Valley of Death", darwinian_sea: "Darwinian Sea"};
const BRO = ["devil_river", "valley_of_death", "darwinian_sea"];  // 関門の並び
const BRD = {devil_river: "「魔の川」と呼ばれる研究開発段階の不確実性（NEDO）。研究から開発へ進めるかどうかの関門", valley_of_death: "「死の谷」と呼ばれる、研究開発後の事業化に要する調達、生産、流通の体制構築や資金調達の困難（NEDO）。導出で機械的に観測するのは資金調達の困難の側のみ", darwinian_sea: "「ダーウィンの海」と呼ばれる市場獲得における生存競争の困難（NEDO）"};
const GEN_NOTE = "世代は、その評価に使われた規程・プロンプトの版です。世代の異なる評価どうしの点数差には、技術の変化ではなく物差し自体の変化が含まれます。";
// 世代表示は既定 OFF (監査的な情報で、常時表示は通常の読みのノイズになる)。
// 関門チップは常時表示 (該当が疎らで、あるときだけ目立つ方が情報として正しい)
let showGen = false;
try { showGen = localStorage.getItem("trl-showgen") === "1"; } catch (e) { /* 保存不可なら既定のまま */ }
const barrierHits = ev => (ev && ev.barriers) ? BRO.filter(k => ev.barriers[k] && ev.barriers[k].status === "hit") : [];
function genBadge(ev) {
  // 世代バッジ。世代に優劣はないため色は付けず、現行かどうかも文字列だけで区別する
  if (!showGen || !ev || !ev.gen) return "";
  const pr = Object.entries(ev.gen.prompts || {}).map(([k, v]) => `${k} ${v}`).join(" / ");
  const head = ev.gen.current ? `現行世代 ${ev.gen.config}` : `世代 ${ev.gen.config}`;
  return `<span class="genb" title="${esc(GEN_NOTE)}">${esc(head)}${pr ? `（${esc(pr)}）` : ""}</span>`;
}
function barrierChipsHtml(ev) {
  // カード用: 該当した関門だけ英語名チップを出す。該当なし・判定しない
  // では何も出さない。チップは折り返す行 (.brs) に置き、狭幅でも
  // カードからはみ出さない
  const hits = barrierHits(ev);
  return hits.length ? `<div class="brs">${hits.map(k => `<span class="brz">${esc(BRL[k])}</span>`).join("")}</div>` : "";
}
function barrierBlockHtml(ev) {
  // 詳細・ドロワー用: 該当した関門ごとに、導出側が生成した根拠文
  // (数値・状態・出典材料) と定義を併記する。該当しない関門と、
  // 材料不足で判定しない関門は出さない
  const hits = barrierHits(ev);
  if (!hits.length) return "";
  const rows = hits.map(k => `<li><b>${esc(BRL[k])}</b><span>${esc(ev.barriers[k].evidence || "")} — ${esc(BRD[k])}</span></li>`).join("");
  return `<p style="margin-top:10px">関門</p><ul class="pts">${rows}</ul>`
    + `<p class="hint">関門は一次情報（治験・財務）の証拠条件から導出した表示専用の判定です（barriers/v1）。採点・選定・企業の層の判定には使いません。関門の定義は NEDO によります。</p>`;
}
const STGO = ["rd", "demo", "production", "commercial"];
const TAGO = [...CO, "photonics", "medtech", "earth_observation", "constellation", "space_transport", "lunar", "debris_removal", "mrna", "cell_therapy", "gene_therapy", "antibody", "radiopharm", "vaccine", "regenerative_medicine", "diagnostics", "drug_ai", "surgical_robot", "power_semi", "advanced_packaging", "memory", "semi_equipment", "ai_infra", "voice_ai", "industrial_robotics", "warehouse_automation", "autonomous_mobility", "humanoid", "hydrogen", "ess", "ev_charging", "solid_state_battery", "battery_materials", "perovskite", "fusion", "smr", "quantum_computing", "quantum_comm", "quantum_sensing"];  // タグの表示順 (分野の後に固有タグ)
const tagLabel = t => CL[t] || TL[t] || t;
const primEnt = p => p.entities.find(e => e.strength === "strong") || p.entities[0] || null;
const marketOf = p => { const e = primEnt(p); return e ? e.market || "JP" : null; };
const sectorOf = p => { const e = primEnt(p); return e ? e.sector || null : null; };
const ord = (v, list) => { const i = list.indexOf(v); return i < 0 ? list.length : i; };
const dirOf = p => (p.long && p.long.status !== "error" && p.long.direction && p.long.direction.direction) || null;  // error 評価の方向性は出さない (詳細側の扱いと揃える)
const dirB = p => { const d = dirOf(p); return d ? `<span class="dir dir-${esc(d)}">${DL[d]}</span>` : ""; };
const jst = iso => { try { return new Date(iso).toLocaleString("ja-JP", {timeZone: "Asia/Tokyo", hour12: false}); } catch (e) { return iso; } };
const label = k => IL[k] || k;

function trl(t, d) {
  const k = ok(t);
  return `<div class="${d ? "det" : "trl"}"><div class="num ${k ? "" : "u"}"><span>TRL</span><strong>${k ? t : "未確定"}</strong></div><div class="track">${
    Array.from({length: 9}, (_, i) => `<span class="seg ${k && i < t ? "f" : ""}"></span>`).join("")}</div></div>`;
}
const trlSel = new Set();  // TRL絞り込みの選択状態: レベル1〜9と "u"(未確定) の集合。空なら絞らない
const stSel = new Set(), dirSel = new Set(), mkSel = new Set(), scSel = new Set(), tagSel = new Set(), stgSel = new Set(), tierSel = new Set();  // 状態・方向性・国・分野・タグ・段階・企業の層の選択状態 (空なら絞らない)
const ownSel = new Set();  // 自分の表示 (新着・注目) の選択状態。グループ内は OR で効く
function inR(t) {
  if (!trlSel.size) return true;
  return ok(t) ? trlSel.has(t) : trlSel.has("u");
}
function haystack(p) {
  const items = p.long ? Object.values(p.long.items).flatMap(i => i.missing_info) : [];
  const facets = p.entities.flatMap(e => [...(e.tags || []), ...(e.tags || []).map(tagLabel), CL[e.sector] || "", ML[e.market || "JP"] || "", STG[e.stage] || ""]);
  return [p.title, p.title_ja || "", p.source, p.abstract, p.abstract_ja || "", ...(p.fields || []), ...p.entities.map(e => e.name), ...facets, ...items].join(" ").toLowerCase();
}
function filtered() {
  const q = $("#q").value.trim().toLowerCase(), o = $("#fo").value;
  const r = P.filter(p => (!q || haystack(p).includes(q))
    && (stSel.size ? stSel.has(p.display_status) : p.display_status !== "rejected")  // 除外はチップを明示的に押した時だけ混ざる
    && inR(lvl(p))
    && (!dirSel.size || dirSel.has(dirOf(p) || "none"))
    && (!mkSel.size || p.entities.some(e => mkSel.has(e.market || "JP")))
    && (!scSel.size || p.entities.some(e => scSel.has(e.sector)))
    && (!tagSel.size || p.entities.some(e => (e.tags || []).some(t => tagSel.has(t))))
    && (!stgSel.size || p.entities.some(e => stgSel.has(e.stage)))
    && (!tierSel.size || p.entities.some(e => tierSel.has(dtierOf(e))))
    && (!ownSel.size || (ownSel.has("new") && isNewEval(p)) || (ownSel.has("pin") && pins.has(p.id))));
  const byStatus = (a, b) => (SO[a.display_status] - SO[b.display_status])
    || ((ok(lvl(b)) ? lvl(b) : -1) - (ok(lvl(a)) ? lvl(a) : -1))
    || String(b.published || "").localeCompare(String(a.published || ""));
  // 種別: 候補に明示の kind があればそれを第一に、無ければ長期評価が
  // あるものを長期、短期 (event) 評価だけのものを短期とする。どちらも
  // 無い未評価の候補は長期側に寄せる (書き出しに kind は含まれない)
  const kindOf = p => (p.kind === "long" || p.kind === "event") ? p.kind
    : (p.long || !p.event) ? "long" : "event";
  r.sort((a, b) => o === "status" ? byStatus(a, b)
    : o === "market" ? (ord(marketOf(a), MO) - ord(marketOf(b), MO)) || byStatus(a, b)
    : o === "sector" ? (ord(sectorOf(a), CO) - ord(sectorOf(b), CO)) || byStatus(a, b)
    : o === "kind" ? ((kindOf(a) === "event") - (kindOf(b) === "event")) || byStatus(a, b)
    : o === "trl" ? (ok(lvl(b)) ? lvl(b) : -1) - (ok(lvl(a)) ? lvl(a) : -1)
    : o === "trl_asc" ? (ok(lvl(a)) ? lvl(a) : 99) - (ok(lvl(b)) ? lvl(b) : 99)
    : o === "date_asc" ? String(a.published || "9999").localeCompare(String(b.published || "9999"))
    : o === "title" ? a.title.localeCompare(b.title, "ja") : String(b.published || "").localeCompare(String(a.published || "")));
  return r;
}
function facetChips() {
  // 絞り込みのチップは、掲載中の候補に実際にある国・分野・タグ・段階だけを順序つきで出す
  const fill = (id, set, values, order, labels) => {
    const present = order.filter(v => values.has(v)).concat([...values].filter(v => !order.includes(v)));
    $("#" + id).innerHTML = present.map(v => `<button type="button" data-v="${esc(v)}" aria-pressed="false">${esc(labels[v] || v)}</button>`).join("");
    [...set].forEach(v => { if (!values.has(v)) set.delete(v); });  // 母集団から消えた値は選択からも落とす
    syncChips(id, set);
  };
  const base = P.filter(p => p.display_status !== "rejected");  // 選択肢は通常の一覧 (除外を除く) にあるものだけ
  fill("fmc", mkSel, new Set(base.flatMap(p => p.entities.map(e => e.market || "JP"))), MO, ML);
  fill("fcc", scSel, new Set(base.flatMap(p => p.entities.map(e => e.sector).filter(Boolean))), CO, CL);
  fill("fgc", tagSel, new Set(base.flatMap(p => p.entities.flatMap(e => e.tags || []))), TAGO, {...CL, ...TL});
  fill("fstc", stgSel, new Set(base.flatMap(p => p.entities.map(e => e.stage).filter(Boolean))), STGO, STG);
  fill("ftierc", tierSel, new Set(base.flatMap(p => p.entities.map(dtierOf))), ["core", "conditional", "look", "see"], DTL);
  syncTagN();
}
function sc(p, k) { const it = p.long && p.long.items[k]; return it ? it.score : null; }
function evShort(p) { const E = p.event && p.event.status !== "error" ? p.event : null; return E ? (E.total != null ? E.total + "点" : "保留") : null; }
function fmtScore(v) { return typeof v === "number" && isFinite(v) ? v : "—"; }

function facetText(p) {
  const m = marketOf(p), c = sectorOf(p);
  return [m ? ML[m] || m : "", c ? CL[c] || c : ""].filter(Boolean).join("・");
}
function holdCauseTxt(ev) {
  // 保留の主因 (export 側で項目の理由文から切り分けたもの) の説明文
  if (!ev || ev.status !== "hold" || !ev.hold_cause) return "";
  return ev.hold_cause === "unidentified"
    ? "保留の主因: 対象企業を資料から一意に特定できないため、企業固有の項目が算出できませんでした。"
    : "保留の主因: 対象企業は特定できていますが、必須項目の根拠が資料にありませんでした。";
}
function prevDiffTxt(p) {
  // 一覧カード用の前回比 (TRL と総合点が変わった分だけを短く出す)
  const pv = p.long && p.long.prev;
  if (!pv) return "";
  const parts = [], cl = lvl(p);
  if (pv.trl_level != null && cl != null && pv.trl_level !== cl) parts.push(`TRL ${pv.trl_level}→${cl}${cl > pv.trl_level ? "↑" : "↓"}`);
  if (pv.total != null && p.long.total != null && pv.total !== p.long.total) parts.push(`総合 ${pv.total}→${p.long.total}`);
  return parts.join(" / ");
}
function prevCmp(ev, curTrl) {
  // 詳細用の前回比較: 前回の日時・状態と、TRL・総合点・項目点の変化
  const pv = ev.prev;
  if (!pv) return "";
  const head = [`前回評価: ${jst(pv.evaluated_at)}（${SL[pv.status] || pv.status}）`];
  // 世代が異なる前回比は、点数差に物差し自体の変化が含まれる。スイッチの
  // 状態によらず常時注記する (P-5)
  if (ev.gen && pv.gen_config && pv.gen_config !== ev.gen.config) head.push("（世代が異なる）");
  const parts = [];
  if (pv.trl_level != null || curTrl != null) {
    // TRL の昇降だけは色と矢印で示す (総合点は良い悪いの含みを持たせないため無色)
    const moved = pv.trl_level != null && curTrl != null && pv.trl_level !== curTrl;
    const cls = moved ? (curTrl > pv.trl_level ? "td-up" : "td-dn") : "";
    parts.push(`<span class="${cls}">TRL ${pv.trl_level ?? "未確定"} → ${curTrl ?? "未確定"}${moved ? (curTrl > pv.trl_level ? "↑" : "↓") : ""}</span>`);
  }
  if (pv.total != null || ev.total != null) parts.push(esc(`総合点 ${pv.total ?? "未確定"} → ${ev.total ?? "未確定"}`));
  const diffs = Object.keys(ev.items).filter(k => k in pv.items && pv.items[k] !== ev.items[k].score)
    .map(k => `${label(k)}: ${pv.items[k] ?? "—"} → ${ev.items[k].score ?? "—"}`);
  return `<p class="hint">${esc(head.join(" "))}</p>`
    + (parts.length ? `<p>${parts.join(" / ")}</p>` : "")
    + (diffs.length ? `<ul class="pts">${diffs.map(d => `<li><b>→</b><span>${esc(d)}</span></li>`).join("")}</ul>`
      : `<p class="hint">項目点に変化はありません。</p>`);
}
function card(p) {
  const s = p.id === sel;
  const ents = p.entities.filter(e => e.strength === "strong").length;
  const facet = facetText(p);
  const diffTxt = prevDiffTxt(p);
  return `<article class="card ${s ? "sel" : ""}" data-id="${esc(p.id)}"><div class="main"><div style="min-width:0"><div class="top"><span class="${src(p.source)}">${srcName(p.source)}</span><span class="date">${esc(p.published || "")}</span>${LANG === "ja" && srcOf(p) ? `<span class="date">訳: ${esc(srcOf(p))}</span>` : ""}${p.long && p.long.llm_model ? `<span class="date">評価: ${esc(p.long.llm_model)}</span>` : ""}${genBadge(p.long) || genBadge(p.event)}${isNewEval(p) ? `<span class="nw">新着</span>` : ""}</div><h3 class="title">${esc(dispT(p))}</h3><p class="abs">${esc(dispA(p))}</p><div class="tags">${(p.fields || []).slice(0, 4).map(t => `<span class="tag">${esc(t)}</span>`).join("")}</div><div class="mi"><span>技術成熟度 <strong>${fmtScore(sc(p, "technology_readiness"))}</strong></span><span>技術的優位性 <strong>${fmtScore(sc(p, "technological_advantage"))}</strong></span><span>関連企業 <strong>${ents ? ents + "社" : "未特定"}</strong></span>${facet ? `<span>国・分野 <strong>${esc(facet)}</strong></span>` : ""}${diffTxt ? `<span>前回比 <strong>${esc(diffTxt)}</strong></span>` : ""}${evShort(p) ? `<span>イベント評価 <strong>${esc(evShort(p))}</strong></span>` : ""}</div>${barrierChipsHtml(p.long)}</div><div class="side">${stB(p.display_status)}${dirB(p)}${trl(lvl(p))}<button type="button" class="pin ${pins.has(p.id) ? "on" : ""}" data-pin="${esc(p.id)}" aria-pressed="${pins.has(p.id) ? "true" : "false"}" title="注目ピン (この端末に保存)">${pins.has(p.id) ? "★" : "☆"}</button></div></div><div class="foot"><span>${p.long && p.long.critical_missing.length ? `必須の不足 ${p.long.critical_missing.length}件` : p.display_status === "pending" ? (p.next_up ? "次回評価予定" : "評価待ち") : p.display_status === "rejected" ? "除外" + (p.screen_reason ? ": " + (SR[p.screen_reason] || p.screen_reason) : "") : ""}</span><span class="facts"><button type="button" class="clink" data-ccite="${esc(p.id)}">出典コピー</button><button type="button" class="clink" data-clink="${esc(p.id)}">リンクコピー</button><a class="tg" href="detail.html?id=${encodeURIComponent(p.id)}">詳細を見る →</a></span></div></article>`;
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

async function copyText(t, btn) {
  // ブラウザ内だけで完結するコピー。Clipboard API が使えない環境では
  // 一時テキストエリア経由にフォールバックする
  let ok = false;
  try { await navigator.clipboard.writeText(t); ok = true; } catch (e) {}
  if (!ok) {
    const ta = document.createElement("textarea");
    ta.value = t; ta.style.position = "fixed"; ta.style.opacity = "0";
    document.body.appendChild(ta); ta.select();
    try { ok = document.execCommand("copy"); } catch (e) {}
    ta.remove();
  }
  if (btn) { const o = btn.textContent; btn.textContent = ok ? "コピーしました" : "コピーできませんでした"; setTimeout(() => { btn.textContent = o; }, 1600); }
}
function copyBuilt(build, btn) {
  // テキスト生成側が例外を投げても無反応にならないよう、生成に失敗したら失敗表示だけは出す
  let t;
  try { t = build(); } catch (e) { if (btn) { const o = btn.textContent; btn.textContent = "コピーできませんでした"; setTimeout(() => { btn.textContent = o; }, 1600); } return; }
  copyText(t, btn);
}
function getNote(id) { try { return localStorage.getItem("trl-note:" + id) || ""; } catch (e) { return ""; } }
const noteDrafts = {};  // 未保存のメモ下書き。再描画で入力途中のメモが消えないように保持する
function setNote(id, v) { try { localStorage.setItem("trl-note:" + id, v); return true; } catch (e) { return false; } }

// 端末内の状態 (注目ピンと最終訪問時刻)。サーバを持たない静的サイトなので
// 端末ごとに保持する (調査メモと同じ)。可用性の担保として、注目ピンと
// 新着の基準時刻 (最終訪問時刻) は共有リンク (?pins=候補ID,...&seen=ISO時刻)
// で別端末へ持ち運べるようにする
let pins = new Set();
try { pins = new Set(JSON.parse(localStorage.getItem("trl-pins") || "[]")); } catch (e) { /* 壊れた保存値は空として扱う */ }
function savePins() { try { localStorage.setItem("trl-pins", JSON.stringify([...pins])); } catch (e) { /* 保存不可でも表示は続ける */ } }
function togglePin(id) { if (!id) return; if (pins.has(id)) pins.delete(id); else pins.add(id); savePins(); render(); }
function importPinsFromUrl() {
  // 共有リンクで届いた注目ピンを、この端末の保存分へ統合する (上書きはしない)
  try {
    const q = new URLSearchParams(location.search).get("pins");
    if (!q) return;
    let added = false;
    for (const id of q.split(",").map(s => s.trim()).filter(Boolean)) if (!pins.has(id)) { pins.add(id); added = true; }
    if (added) savePins();
    history.replaceState(null, "", location.pathname);
  } catch (e) { /* 取り込みに失敗しても通常表示は続ける */ }
}
// 最終訪問時刻はページを開いた時点の保存値で判定する (セッション中に
// バッジが消えないように、判定に使う値は読み込み時のまま固定する)。
// 初回訪問では基準だけを置いてバッジは付けない (全部が新着になるため)
let lastVisit = null;
try {
  lastVisit = localStorage.getItem("trl-last-visit");
  // 共有リンク (?seen=) で届いた基準時刻を取り込む。既読の統合なので、
  // 保存値と届いた値のうち遅い方を基準にする (ピンの統合と同じ考え方)。
  // 未来の時刻は「今」で頭打ちにする (まだ見ていない分まで既読にしない)
  const seenQ = new URLSearchParams(location.search).get("seen");
  if (seenQ && !Number.isNaN(Date.parse(seenQ))) {
    const seenIso = new Date(Math.min(Date.parse(seenQ), Date.now())).toISOString();
    if (!lastVisit || seenIso > lastVisit) lastVisit = seenIso;
    const u = new URL(location.href); u.searchParams.delete("seen");
    history.replaceState(null, "", u.pathname + u.search);
  }
  localStorage.setItem("trl-last-visit", new Date().toISOString());
} catch (e) { /* 保存不可なら新着判定は無効 */ }
const isNewEval = p => !!(lastVisit && p.long && p.long.evaluated_at && p.long.evaluated_at > lastVisit);
function citeText(p) { return [p.title, srcName(p.source) + (p.published ? " ・ " + p.published : ""), p.url].filter(Boolean).join("\n"); }
function detailUrl(p) { return location.origin + location.pathname.replace(/[^/]*$/, "") + "detail.html?id=" + encodeURIComponent(p.id); }
function evalText(p) {
  // 評価まるごとコピー用テキスト (項目点・理由・TRL・不足・前回比)
  const L = p.long && p.long.status !== "error" ? p.long : null;
  if (!L) return "";
  const lines = [dispT(p), citeText(p)];
  const ents = p.entities.filter(e => e.strength === "strong").map(e => e.name);
  if (ents.length) lines.push(`関連企業: ${ents.join("、")}`);
  const lv = lvl(p);
  lines.push(`TRL: ${lv != null ? lv : "未確定"}${L.trl_level && L.trl_level.reason ? " — " + L.trl_level.reason : ""}`);
  if (L.total != null) lines.push(`総合点: ${L.total} / 100${L.band ? `（${L.band}）` : ""}${L.total_scope === "technology" ? " ※技術軸のみで算出" : ""}`);
  if (L.total_orig != null && L.total != null && L.total_orig !== L.total) lines.push(`評価当時の総合点: ${L.total_orig}${L.band_orig ? `（${L.band_orig}）` : ""} ※現行の重みで再計算した値が上の総合点`);
  const G = META.config.item_groups;
  const order = [...G.technology, ...G.business, ...Object.keys(L.items)];
  for (const k of [...new Set(order)]) {
    const it = L.items[k];
    if (!it) continue;
    lines.push(`■ ${label(k)}: ${it.score != null ? it.score + " / 100" : "未算出"}`);
    if (it.reason) lines.push(it.reason);
    if (it.score == null && it.missing_info && it.missing_info.length) lines.push(`不足: ${it.missing_info.join(" / ")}`);
  }
  if (L.critical_missing && L.critical_missing.length) lines.push(`必須項目の不足: ${L.critical_missing.map(label).join("、")}`);
  if (L.direction) lines.push(`材料の方向性: ${DL[L.direction.direction] || L.direction.direction}${L.direction.reason ? " — " + L.direction.reason : ""}`);
  if (L.prev) lines.push(`前回評価: ${jst(L.prev.evaluated_at)}（${SL[L.prev.status] || L.prev.status}） TRL ${L.prev.trl_level ?? "未確定"} / 総合点 ${L.prev.total ?? "未確定"}`);
  lines.push(`評価日: ${jst(L.evaluated_at)}${L.llm_model ? ` / 評価モデル: ${L.llm_model}` : ""}`);
  return lines.join("\n");
}

function mdNote(p) {
  // Obsidian 等へ貼るための Markdown ノート (trlcore/vault.py の評価ノートと同じ形式)
  const L = p.long && p.long.status !== "error" ? p.long : null;
  if (!L) return "";
  const lv = lvl(p);
  const e0 = (p.entities || [])[0] || {};
  // 評価基準の版は、その評価自身の世代 (gen) を正とする (G-3)。gen が無い
  // 旧データでは export 時点の現行版にフォールバックする
  const ver = (L.gen && L.gen.config) || (META.config && META.config.version) || "";
  const fm = ["---", "type: evaluation", `candidate: "${p.id}"`];
  if (e0.entity_id) fm.push(`company: "${e0.entity_id}"`);
  fm.push(`status: "${p.display_status}"`);
  if (L.total != null) fm.push(`total: ${L.total}`);
  if (L.band) fm.push(`band: "${L.band}"`);
  if (lv != null) fm.push(`trl: ${lv}`);
  if (L.direction) fm.push(`direction: "${L.direction.direction}"`);
  if (L.llm_model) fm.push(`model: "${L.llm_model}"`);
  fm.push(`evaluated: "${(L.evaluated_at || "").slice(0, 10)}"`);
  if (ver) fm.push(`config_version: "${ver}"`);
  fm.push(`source: "${p.source}"`);
  if (p.published) fm.push(`published: "${String(p.published).slice(0, 10)}"`);
  if (p.retrieved_at) fm.push(`retrieved: "${String(p.retrieved_at).slice(0, 10)}"`);
  fm.push("---");
  const out = [...fm, "", `# ${dispT(p)}`, ""];
  if (p.title_ja) out.push(`原題: ${p.title}`);
  for (const e of p.entities || []) out.push(`企業: ${e.name}（${e.market || "JP"} / ${e.sector || "未取得"}）`);
  out.push(`出典: [${p.url}](${p.url})（${p.source}）`);
  out.push(`公開: ${p.published ? String(p.published).slice(0, 10) : "未取得"} / 取得: ${p.retrieved_at ? String(p.retrieved_at).slice(0, 10) : "未取得"}`);
  out.push(`評価日時: ${L.evaluated_at || "未取得"} / 評価モデル: ${L.llm_model || "未取得"} / 評価基準: ${ver || "未取得"}`);
  out.push("", "## 評価サマリ");
  if (L.total != null) out.push(`総合点: ${L.total}（${L.band || ""}）`);
  else out.push("総合点: 未取得（保留のため算出せず）");
  if (L.total_orig != null && L.total != null && L.total_orig !== L.total) out.push(`評価当時の総合点: ${L.total_orig}（${L.band_orig || ""}）`);
  out.push(`TRL: ${lv != null ? lv : "未確定"}`);
  if (L.trl_level && L.trl_level.reason) out.push(`TRL の理由: ${L.trl_level.reason}`);
  if (L.direction) out.push(`方向性: ${L.direction.direction}${L.direction.reason ? " — " + L.direction.reason : ""}`);
  if (L.prev) out.push(`前回評価: 総合 ${L.prev.total ?? "未取得"} / TRL ${L.prev.trl_level ?? "未確定"} / 状態 ${L.prev.status}（${L.prev.evaluated_at || ""}）`);
  out.push("", "## 項目別");
  const G = META.config.item_groups;
  const order = [...G.technology, ...G.business, ...Object.keys(L.items)];
  for (const k of [...new Set(order)]) {
    const it = L.items[k];
    if (!it) continue;
    out.push(`### ${label(k)}: ${it.score != null ? it.score + "点" : "未取得（不足）"}`);
    if (it.reason) out.push(it.reason);
    for (const e of it.evidence || []) out.push(`- 根拠: ${e.summary}${e.ref ? `（[${e.ref}](${e.ref})）` : ""}`);
    for (const ce of it.counter_evidence || []) out.push(`- 反証: ${ce}`);
    const miss = (it.missing_info || []).filter(m => m && m !== "特になし");
    if (miss.length) out.push(`- 不足: ${miss.join(" / ")}`);
    if ((it.awaiting_info || []).length) out.push(`- 結果待ち: ${it.awaiting_info.join(" / ")}`);
    out.push("");
  }
  if (L.critical_missing && L.critical_missing.length) { out.push("## 決定的な不足", ...L.critical_missing.map(k => "- " + label(k)), ""); }
  out.push("## 要旨（原文）", p.abstract || "未取得", "", "## 要旨（日本語訳）", p.abstract_ja || "未取得（未翻訳）");
  return out.join("\n");
}

function detail() {
  const p = P.find(x => x.id === sel);
  if (!p) { $("#det").innerHTML = `<div class="ph"><i>⌕</i><h3>論文が選択されていません</h3><p class="hint">検索条件を変更するか、一覧から論文を選択してください。</p></div>`; return; }
  const LL = p.long, G = META.config.item_groups;
  const REF = (META.config && META.config.reference_items) || [];  // 参考項目 (総合点・不足集計に含めない)
  const L = LL && LL.status !== "error" ? LL : null;  // error は評価結果ではないので点数欄には出さない
  const EV = p.event && p.event.status !== "error" ? p.event : null;
  const tr = L && L.trl_level;
  const strong = p.entities.filter(e => e.strength === "strong");
  const missAll = L ? [...new Set(Object.entries(L.items).filter(([k]) => !REF.includes(k)).flatMap(([, i]) => i.missing_info))] : [];
  const waitAll = L ? [...new Set(Object.entries(L.items).filter(([k]) => !REF.includes(k)).flatMap(([, i]) => i.awaiting_info || []))] : [];
  const q = p.queue && p.queue.long;
  const total = L && L.total != null ? `${L.total} / 100(${esc(L.band || "")})` : "未確定";
  $("#det").innerHTML = `<div class="dc"><header class="dh"><div class="top"><span class="${src(p.source)}">${srcName(p.source)}</span>${stB(p.display_status)}${isNewEval(p) ? `<span class="nw">新着</span>` : ""}<button type="button" class="pin ${pins.has(p.id) ? "on" : ""}" id="pint" aria-pressed="${pins.has(p.id) ? "true" : "false"}" title="注目ピン (この端末に保存)">${pins.has(p.id) ? "★" : "☆"}</button></div><h3>${esc(dispT(p))}</h3><div class="mi"><span>公開日 ${esc(p.published || "—")}</span>${L ? `<span>評価日 ${esc(jst(L.evaluated_at))}</span>` : ""}${p.eval_count ? `<span>評価 ${p.eval_count} 回目</span>` : ""}${LL && LL.llm_model ? `<span>評価モデル ${esc(LL.llm_model)}</span>` : ""}${genBadge(L) || genBadge(EV)}</div>${showGen && L && L.gen && !L.gen.current ? `<p class="note" style="margin-top:8px">${esc(GEN_NOTE)}</p>` : ""}</header>
<section class="ds"><h4>研究概要</h4><p>${esc(dispA(p)) || "（要旨なし）"}</p>${LANG === "ja" && srcOf(p) ? `<p class="hint">日本語訳: ${esc(srcOf(p))}</p>` : ""}<div class="acts"><button class="btn sec" type="button" id="cpyo">原文をコピー</button>${(p.title_ja || p.abstract_ja) ? `<button class="btn sec" type="button" id="cpyj">日本語訳をコピー</button>` : ""}</div></section>
<section class="ds"><h4>技術成熟度（TRL）</h4>${trl(lvl(p), 1)}${tr && tr.reason ? `<p>${esc(tr.reason)}</p>` : ""}${tr && tr.level == null && tr.missing_info.length ? `<p class="hint">不足: ${tr.missing_info.map(esc).join(" / ")}</p>` : ""}${tr && tr.awaiting_info && tr.awaiting_info.length ? `<p class="hint">結果待ち: ${tr.awaiting_info.map(esc).join(" / ")}</p>` : ""}<p class="hint">TRLは資料から根拠付きで判断できた場合のみ表示します。技術成熟度スコアからの換算はしません。</p>${L ? barrierBlockHtml(L) : ""}</section>
<section class="ds"><h4>技術評価</h4>${L ? G.technology.map(k => scoreRow(k, L.items[k])).join("") : `<p class="hint">未評価です。</p>`}</section>
<section class="ds"><h4>事業評価</h4>${L && L.business_stage === "pre_stage" ? `<p class="note">この技術は前商業段階(TRL 3以下)のため、事業項目は「段階未達」として不足とは区別しています。点が付いている項目はそのまま表示します。</p>` : ""}${L ? G.business.map(k => scoreRow(k, L.items[k])).join("") : `<p class="hint">未評価です。</p>`}${L && !strong.length ? `<p class="note">関連企業が特定できていないため、企業固有の項目は算出されません。総合点は6項目すべてが揃うまで確定せず、重みの再配分もしません。</p>` : ""}</section>
${L && REF.some(k => L.items[k]) ? `<section class="ds"><h4>参考項目</h4><p class="note">参考項目は採点して表示しますが、総合点・必須判定には含めません。根拠が無い場合は未算出となり、それを理由に保留にもなりません。</p>${REF.filter(k => L.items[k]).map(k => scoreRow(k, L.items[k])).join("")}</section>` : ""}
<section class="ds"><h4>総合点（長期）</h4><p>${total}</p>${L && L.total_scope === "technology" ? `<p class="note">これは技術評価の総合点です。事業項目は段階未達のため対象外で、技術軸の項目だけで算出しています。</p>` : ""}${holdCauseTxt(L) ? `<p class="note">${esc(holdCauseTxt(L))}</p>` : ""}${L && L.total_orig != null && L.total != null && L.total_orig !== L.total ? `<p class="note">評価当時の総合点は ${L.total_orig}${L.band_orig ? `（${esc(L.band_orig)}）` : ""} でした。上の総合点は、重みの改訂に合わせて現行の物差しで計算し直した値です。</p>` : ""}${L ? `<div class="acts"><button class="btn sec" type="button" id="cpye">評価をコピー</button><button class="btn obsidian" type="button" id="cpym">Markdownでコピー</button></div>` : ""}</section>
${auditSectionHtml(p)}
${EV ? `<section class="ds"><h4>イベント評価（短期）</h4>${genBadge(EV) ? `<p style="margin-bottom:8px">${genBadge(EV)}</p>` : ""}${Object.keys(EV.items).map(k => scoreRow(k, EV.items[k])).join("")}<p>総合点 ${EV.total != null ? EV.total + " / 100(" + esc(EV.band || "") + ")" : "未確定（保留）"}</p></section>` : ""}
${L && L.prev ? `<section class="ds"><h4>前回評価との比較</h4>${prevCmp(L, lvl(p))}</section>` : ""}
<section class="ds"><h4>材料の方向性</h4>${L && L.direction ? `<p>${dirB(p)} ${esc(L.direction.reason || "")}</p><p class="hint">方向性は点数やTRLとは独立に、資料が示す出来事の追い風・逆風を判定したものです。</p>` : `<p class="hint">未判定です（方向を決める出来事が資料に無いか、旧基準の評価です）。</p>`}</section>
<section class="ds"><h4>不足している観点</h4>${L && L.critical_missing.length ? `<p class="m" style="color:#a96500;font-weight:700">必須項目の不足: ${L.critical_missing.map(k => esc(label(k))).join("、")}</p>` : ""}${pts(missAll, 1)}${waitAll.length ? `<p class="hint">結果待ち（不足ではなく、治験の結果公表など時間の経過でしか解けないもの）:</p><ul class="pts">${waitAll.map(x => `<li><b>◷</b><span>${esc(x)}</span></li>`).join("")}</ul>` : ""}${q && q.next_sources.length ? `<p class="hint">次に確認する情報源: ${q.next_sources.map(esc).join(" / ")}</p>` : ""}${q && q.reeval_conditions.length ? `<p class="hint">再評価の条件: ${q.reeval_conditions.map(esc).join(" / ")}</p>` : ""}</section>
${p.display_status === "error" && LL && LL.errors.length ? `<section class="ds"><h4>処理上の注意</h4>${pts(LL.errors, 1)}</section>` : ""}
${p.display_status === "rejected" ? `<section class="ds"><h4>除外理由</h4><p>${esc(p.screen_reason ? (SR[p.screen_reason] || p.screen_reason) : "理由は記録されていません")}</p><p class="hint">除外は関連性スクリーニングの機械的な判定で、評価 (採点) は行われていません。</p></section>` : ""}
<section class="ds"><h4>関連企業</h4>${p.entities.map(e => `<span class="chip">${esc(e.name)}${e.product_name ? "（" + esc(e.product_name) + "）" : ""}${e.market ? "・" + esc(ML[e.market] || e.market) : ""}${e.sector ? "・" + esc(CL[e.sector] || e.sector) : ""}${(e.tags || []).length ? "・" + e.tags.map(t => esc(tagLabel(t))).join("・") : ""}${e.stage ? "・" + esc(STG[e.stage] || e.stage) : ""} ${e.strength === "strong" ? "" : "・弱"}${dtText(e) ? "・" + esc(dtText(e)) : ""}</span>`).join("") || '<span class="hint">関連企業は未特定です。</span>'}</section>
<section class="ds"><h4>調査メモ</h4><textarea id="note" aria-label="調査メモ" placeholder="仮説、確認したい点、次の調査などを記録...">${esc(p.id in noteDrafts ? noteDrafts[p.id] : getNote(p.id))}</textarea><div class="acts"><button class="btn sec" type="button" id="save">メモを保存</button><button class="btn sec" type="button" id="cpys">出典をコピー</button><a class="btn src" href="${esc(safeUrl(p.url))}" target="_blank" rel="noopener noreferrer">原文サイトを開く ↗</a></div><p id="msg" class="hint" aria-live="polite">メモはこの端末のブラウザにだけ保存されます。</p></section></div>`;
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
  // 要素が無い場合はその項目だけ飛ばす。HTML と JS の版がキャッシュでずれて
  // 混ざっても、1項目の欠けで後続の統計まで止まらないようにするため
  const set = (id, v) => { const el = $(id); if (el) el.textContent = v; };
  set("#c0", P.filter(p => p.display_status !== "rejected").length);
  set("#c0p", P.filter(p => p.long && p.long.prev).length);
  set("#c1", n(["final"]));
  set("#c2", n(["review"]));
  set("#c3", n(["hold"]));
  // 内訳は見出し (保留 = display_status が hold) と同じ母数を数える。
  // 評価が hold でも必須の不足が無いものは review (要追加調査) に出るため、
  // hold_cause だけで数えると見出しと合わなくなる
  set("#c3u", P.filter(p => p.display_status === "hold" && p.long && p.long.hold_cause === "unidentified").length);
  set("#c3e", P.filter(p => p.display_status === "hold" && p.long && p.long.hold_cause === "no_evidence").length);
  set("#c4", n(["pending"]));
  set("#c4n", P.filter(p => p.next_up).length);
  set("#c5", n(["error"]));
  set("#c6", P.filter(p => lvl(p) != null).length);
  set("#c7", n(["rejected"]));
}
function tierBand() {
  // 企業の層 (導出 tier) の分布と警告 (W-3)。数字は export (summary.tiers)
  // の直読みで、表示側で閾値や丸めは作らない。警告は D-3 どおり出すだけ。
  // summary.tiers が部分的な形 (shares 欠落・warnings が配列でない等) でも
  // 例外にせず、欠けた部分だけを落とす (この枠の不調で一覧全体を止めない)
  const el = $("#tierband");
  if (!el) return;
  const t = META && META.summary && META.summary.tiers;
  if (!t || !t.counts) { el.hidden = true; return; }
  const shares = t.shares && typeof t.shares === "object" ? t.shares : {};
  const order = ["core", "conditional", "look", "see"];
  const dist = order.map(k => `${DTL[k]} ${t.counts[k] || 0}社（${shares[k] ?? 0}%）`).join(" / ");
  const warns = (Array.isArray(t.warnings) ? t.warnings : [])
    .filter(w => w && typeof w === "object")
    .map(w =>
    `${DTL[w.tier] || w.tier} は目標 ${w.target_share}% から ${w.diff_pt > 0 ? "+" : "−"}${Math.abs(w.diff_pt)}pt（許容 ±${t.tolerance_pt}pt の範囲外）`);
  el.innerHTML = `<span>企業の層: ${esc(dist)}</span>`
    + (warns.length ? `<span class="twarn">警告: ${esc(warns.join("、"))}</span>` : "");
  el.hidden = false;
}
function showError(msg) {
  $("#list").innerHTML = "";
  $("#det").innerHTML = "";
  $("#gen").textContent = "データなし";
  $("#empty").textContent = msg;
  const b = document.createElement("button");
  b.className = "btn sec"; b.type = "button"; b.textContent = "再読み込み";
  b.style.marginLeft = "8px";
  b.addEventListener("click", () => load());
  $("#empty").appendChild(b);
  $("#empty").hidden = false;
}
// 統計の枠を押すと、その状態で絞り込む (同じ枠をもう一度押すと解除)。
// TRL判定済みの枠は、TRLチップ 1〜9 の全選択への近道として働く
document.querySelectorAll(".stat[data-st]").forEach(el => el.addEventListener("click", () => {
  const v = el.dataset.st;
  if (v === "__trl") {
    const all = [1, 2, 3, 4, 5, 6, 7, 8, 9].every(x => trlSel.has(x));
    trlSel.clear();
    if (!all) for (let i = 1; i <= 9; i++) trlSel.add(i);
    $("#ftn").value = ""; $("#ftm").value = "";
    syncTrlUI(); render(); return;
  }
  const on = v && stSel.size === 1 && stSel.has(v);
  stSel.clear();
  if (v && !on) stSel.add(v);
  syncChips("fsc", stSel); render();
}));
function warn(d) {
  const w = [];
  const run = d.run;
  if (run && run.llm_degraded) w.push("直近の実行ではLLM APIの失敗が続いたため、評価を途中で打ち切りました。未評価の論文は次回の実行で再評価されます。");
  if (run && run.status === "failed") w.push("直近の実行は、すべての情報源の取得に失敗しました。");
  if (run && run.status === "partial") w.push("直近の実行は、一部の情報源の取得または評価に失敗しました。");
  if (run && run.as_of) {
    const age = (Date.now() - new Date(run.as_of + "T00:00:00")) / 86400000;
    if (age > 10) w.push(`直近の実行は ${Math.floor(age)} 日前です。定期実行が止まっている可能性があります。`);
  }
  // 出典記録の無い旧訳は再翻訳の対象として残っている分。借の見える化
  const stale = (d.candidates || []).filter(p => p.title_ja && !(p.translation_src && p.translation_src.title)).length;
  if (stale) w.push(`翻訳の出典記録が無い旧訳が ${stale} 件あります (順次、新しい翻訳に置き換わります)。`);
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
  importPinsFromUrl();
  $("#gen").textContent = "更新 " + jst(d.generated_at);
  $("#fl").value = LANG;
  facetChips(); warn(d); stats(); tierBand(); render();
}
$("#sf").addEventListener("submit", e => { e.preventDefault(); render(); });
document.addEventListener("keydown", e => {
  // "/" で検索欄へ移動 (入力中は邪魔しない)
  if (e.key === "/" && !/INPUT|TEXTAREA|SELECT/.test((document.activeElement || {}).tagName || "")) {
    e.preventDefault(); $("#q").focus();
  }
});
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
$("#fo").addEventListener("change", render);
function syncChips(id, set) {
  document.querySelectorAll("#" + id + " button").forEach(b => {
    const on = set.has(b.dataset.v);
    b.classList.toggle("on", on); b.setAttribute("aria-pressed", on ? "true" : "false");
  });
}
function bindChips(id, set, after) {
  $("#" + id).addEventListener("click", e => {
    const b = e.target.closest("button"); if (!b) return;
    if (set.has(b.dataset.v)) set.delete(b.dataset.v); else set.add(b.dataset.v);
    syncChips(id, set); if (after) after(); render();
  });
}
const syncTagN = () => { $("#fgn").textContent = tagSel.size ? `（${tagSel.size}件選択中）` : ""; };
const CHIPG = [["fsc", stSel], ["fdc", dirSel], ["fmc", mkSel], ["fcc", scSel], ["fstc", stgSel], ["ftierc", tierSel], ["fown", ownSel]];
CHIPG.forEach(([id, set]) => bindChips(id, set));
bindChips("fgc", tagSel, syncTagN);
const syncTrlUI = () => document.querySelectorAll("#ftc button").forEach(b => {
  // 「判定済み」チップの選択表示は、1〜9 が全部そろっているかどうかで決まる (近道なので専用の状態は持たない)
  const on = b.dataset.l === "all" ? [1, 2, 3, 4, 5, 6, 7, 8, 9].every(i => trlSel.has(i))
    : trlSel.has(b.dataset.l === "u" ? "u" : Number(b.dataset.l));
  b.classList.toggle("on", on); b.setAttribute("aria-pressed", on ? "true" : "false");
});
$("#ftc").addEventListener("click", e => {
  const b = e.target.closest("button"); if (!b) return;
  if (b.dataset.l === "all") {
    // 判定済み = レベルが確定したもの全部。範囲入力と同じ「チップ状態への近道」で、
    // 全選択なら全解除、そうでなければ 1〜9 を全選択にする (未確定チップはそのまま)
    const all = [1, 2, 3, 4, 5, 6, 7, 8, 9].every(i => trlSel.has(i));
    for (let i = 1; i <= 9; i++) { if (all) trlSel.delete(i); else trlSel.add(i); }
  } else {
    const k = b.dataset.l === "u" ? "u" : Number(b.dataset.l);
    if (trlSel.has(k)) trlSel.delete(k); else trlSel.add(k);
  }
  $("#ftn").value = ""; $("#ftm").value = "";  // 個別に触ったら範囲入力は空に戻す (表示と選択状態を一致させる)
  syncTrlUI(); render();
});
const applyRange = () => {  // n〜m を入れたら、その範囲のレベルだけを選択状態にする (未確定チップはそのまま)
  if ($("#ftn").value === "" || $("#ftm").value === "") return;
  const n = Number($("#ftn").value), m = Number($("#ftm").value);
  if (!Number.isInteger(n) || !Number.isInteger(m)) return;
  const lo = Math.min(9, Math.max(1, Math.min(n, m))), hi = Math.min(9, Math.max(1, Math.max(n, m)));
  for (let i = 1; i <= 9; i++) { if (i >= lo && i <= hi) trlSel.add(i); else trlSel.delete(i); }
  syncTrlUI(); render();
};
["#ftn", "#ftm"].forEach(s => $(s).addEventListener("change", applyRange));
$("#fl").addEventListener("change", () => { LANG = $("#fl").value; try { localStorage.setItem("trl-lang", LANG); } catch (e) { /* 保存できなくても切替は効く */ } render(); });
const syncTh = () => { $("#fth").value = document.documentElement.dataset.theme === "light" ? "light" : "dark"; };
$("#fth").addEventListener("change", () => { document.documentElement.dataset.theme = $("#fth").value; try { localStorage.setItem("trl-theme", $("#fth").value); } catch (e) { /* 保存できなくても切替は効く */ } });
syncTh();
// 世代表示のスイッチ。状態は localStorage (trl-showgen) で詳細ページと共有する
function applyViewSwitches() {
  const sg = $("#swgen");
  if (sg) sg.checked = showGen;
}
$("#swgen").addEventListener("change", () => { showGen = $("#swgen").checked; try { localStorage.setItem("trl-showgen", showGen ? "1" : "0"); } catch (e) { /* 保存できなくても切替は効く */ } render(); });
applyViewSwitches();
$("#rs").addEventListener("click", () => { $("#q").value = ""; [...CHIPG, ["fgc", tagSel]].forEach(([id, set]) => { set.clear(); syncChips(id, set); }); syncTagN(); trlSel.clear(); $("#ftn").value = ""; $("#ftm").value = ""; syncTrlUI(); $("#fo").value = "status"; sel = P[0] ? P[0].id : null; render(); });
$("#list").addEventListener("click", e => {
  const pb = e.target.closest("[data-pin]");
  if (pb) { togglePin(pb.dataset.pin); return; }
  const cc = e.target.closest("[data-ccite]"), cl = e.target.closest("[data-clink]");
  if (cc || cl) {
    const p = P.find(x => x.id === (cc ? cc.dataset.ccite : cl.dataset.clink));
    if (!p) return;
    if (cc) copyText(citeText(p), cc); else copyText(detailUrl(p), cl);
    return;
  }
  const c = e.target.closest("[data-id]"); if (c) { sel = c.dataset.id; render(); }
});
$("#cpypins").addEventListener("click", e => {
  const btn = e.currentTarget;
  // 共有リンクには注目ピンと新着の基準時刻 (?seen=) を含める。基準時刻は
  // コピーの時点 (= ここまで見た) とし、この端末の保存値も同じ時刻へ
  // 進める (送り先と自分の次回訪問で新着の見え方が揃うように)。
  // 表示中の判定値 (lastVisit) は変えないので、今見ているバッジは消えない
  const seen = new Date().toISOString();
  try { localStorage.setItem("trl-last-visit", seen); } catch (err) { /* 保存不可でもリンクは作る */ }
  const parts = [];
  if (pins.size) parts.push(`pins=${[...pins].map(encodeURIComponent).join(",")}`);
  parts.push(`seen=${encodeURIComponent(seen)}`);
  copyText(`${location.origin}${location.pathname}?${parts.join("&")}`, btn);
});
$("#det").addEventListener("input", e => { if (e.target.id === "note" && sel) noteDrafts[sel] = e.target.value; });
$("#det").addEventListener("click", e => {
  if (e.target.closest("#save")) {
    const saved = setNote(sel, $("#note").value); if (saved) delete noteDrafts[sel];
    $("#msg").textContent = saved ? "この端末のブラウザに保存しました。" : "保存できませんでした(ブラウザの保存機能が無効の可能性があります)。";
  }
  if (e.target.closest("#pint")) { togglePin(sel); return; }
  const co = e.target.closest("#cpyo"), cj = e.target.closest("#cpyj"), cs = e.target.closest("#cpys"), ce = e.target.closest("#cpye"), cm = e.target.closest("#cpym");
  if (co || cj || cs || ce || cm) {
    const p = P.find(x => x.id === sel);
    if (!p) return;
    if (co) copyBuilt(() => [p.title, p.abstract].filter(Boolean).join("\n\n"), co);
    else if (cj) copyBuilt(() => [p.title_ja || p.title, p.abstract_ja || p.abstract].filter(Boolean).join("\n\n"), cj);
    else if (ce) copyBuilt(() => evalText(p), ce);
    else if (cm) copyBuilt(() => mdNote(p), cm);
    else copyBuilt(() => citeText(p), cs);
  }
});
load();
