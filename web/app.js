"use strict";
/* TRL Explorer: trl.py export が出力した data.json を読んで表示する(契約: schema.json)。
   評価値はすべて data.json の値をそのまま表示し、画面側で再計算・換算・平均はしない。 */
let P = [], META = null, sel = null;
let RANK = new Map();  // おすすめ順のときの「状態グループ内の順位」(render が毎回作る)
let VIEW = [];  // 直近の render で並んだ候補の列 (キー移動の対象)
let LANG = "ja";
try { LANG = localStorage.getItem("trl-lang") || "ja"; } catch (e) { /* 保存不可なら既定のまま */ }
const dispT = p => LANG === "ja" && p.title_ja ? p.title_ja : p.title;
const dispA = p => LANG === "ja" && p.abstract_ja ? p.abstract_ja : p.abstract || "";
const ABS_N = 300;  // 要旨の折りたたみ閾値。サイトの抜粋基準 (PubMed 冒頭2文・最大300字) と同値
const absHtml = t => { const s = t || ""; if (!s) return "<p>（要旨なし）</p>"; if (s.length <= ABS_N) return `<p>${esc(s)}</p>`; return `<p>${esc(s.slice(0, ABS_N))}…</p><details class="absfull"><summary>続きを表示</summary><p>${esc(s.slice(ABS_N))}</p></details>`; };
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
/* 詳細の監査節も監査ページと同じ畳み方にする: 既定で出すのは系統ごとに
   最新の監査バッチの、未処分の指摘だけ。過去バッチと処分済みの指摘は
   展開 (details) の中に入れる。処分の状態は監査ページと同じ localStorage
   (trl-review) を読む (読み取り専用。ここでは変更しない) */
function latestAuditBlocks(blocks) {
  const best = new Map();
  for (const a of blocks || []) {
    const key = `${a.evaluation_id}:${a.provider || ""}`;
    const cur = best.get(key);
    if (!cur || String(a.created_at || "") >= String(cur.created_at || "")) best.set(key, a);
  }
  return [...best.values()];
}
let REVIEW_DEC = {};
try {
  const r = JSON.parse(localStorage.getItem("trl-review") || "null");
  if (r && r.decisions) REVIEW_DEC = r.decisions;
} catch (e) { /* 読めなければ処分なしとして扱う */ }
const DISP_L = {confirmed: "問題あり", false_positive: "誤検知", watch: "様子見"};
/* 処分は localStorage (監査ページと共有) を優先し、無い分は監査ブロックに
   同梱された DB 側の有効な処分 (decisions) で補う。この端末で監査ページを
   開いていなくても、反映済みの処分はここでも同じに見える */
const decOf = (a, e) => REVIEW_DEC[`${a.evaluation_id}:${e.check || ""}:${e.item || ""}`]
  || (a.decisions || []).find(d => (d.check || "") === (e.check || "") && (d.item || "") === (e.item || "")) || null;
function auditGroupHtml(blocks, heading, link) {
  const latSet = new Set(latestAuditBlocks(blocks));
  const rowOf = (e, tag) => `<li><b>${esc(e.check || "")}</b> ${e.verdict === "gray" ? "グレー" : "要確認"}: ${esc(ACH[e.check] || "")}${e.item ? `（${esc(AIL[e.item] || IL[e.item] || e.item)}）` : ""}${tag || ""}</li>`;
  let nf = 0, ng = 0;
  const rows = [], rest = [];
  for (const a of blocks) for (const e of a.entries || []) {
    const d = decOf(a, e);
    if (latSet.has(a) && !(d && d.disposition)) {
      if (e.verdict === "gray") ng++; else nf++;
      rows.push(rowOf(e));
    } else if (d && d.disposition) {
      rest.push(rowOf(e, `（処分済み: ${esc(DISP_L[d.disposition] || d.disposition)}）`));
    } else {
      rest.push(rowOf(e, `（過去の監査: ${esc(a.auditor || a.provider || "")}）`));
    }
  }
  const head = heading ? `<p><b>${esc(heading)}</b></p>` : "";
  if (!rows.length && !rest.length) return `${head}<p class="hint">監査で指摘はありませんでした。</p>`;
  let h = `${head}<p>要確認 ${nf} 件 / グレー ${ng} 件</p>`;
  h += rows.length ? `<ul class="pts">${rows.join("")}</ul>`
    : `<p class="hint">最新の監査に未処分の指摘はありません。</p>`;
  if (rest.length) h += `<details class="apast"><summary>処分済み・過去の監査を見る（${rest.length}件）</summary><ul class="pts">${rest.join("")}</ul></details>`;
  return `${h}<div class="acts"><a class="btn sec" href="${link}">監査ページでこの記事の指摘を見る →</a></div>`;
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
// 世代表示は既定 ON (旧世代と現行世代が混在する間は、どの物差しで
// 測った評価かが見えることが通常の読みの前提になるため)。端末に保存
// 済みの設定があればそちらを尊重する。
// 関門チップは常時表示 (該当が疎らで、あるときだけ目立つ方が情報として正しい)
let showGen = true;
try { showGen = localStorage.getItem("trl-showgen") !== "0"; } catch (e) { /* 保存不可なら既定のまま */ }
const barrierHits = ev => (ev && ev.barriers) ? BRO.filter(k => ev.barriers[k] && ev.barriers[k].status === "hit") : [];
// 関門の状態ラベル。導出 (barriers/v1) の status の読み替えで、表示側で
// 状態を作ることはしない (hit=該当 / clear=非該当 / unknown=判定しない
// (材料不足) / na=対象外 (TRL のゲート外))
const BRS = {hit: "該当", clear: "非該当", unknown: "判定しない", na: "対象外"};
const ANNEX_AXIS = {arl: "導入", irl: "統合", srl: "システム"};
const ANNEX_KIND = {first_sale: "初回販売・提供開始", supply_agreement: "有償の供給・販売契約", paid_pilot: "有償PoC・パイロット", licensee_launch: "導出先での販売開始", scale_deployment: "複数顧客・地域への展開", integration_demo: "他システムへの統合実証", platform_adoption: "プラットフォーム・OEMへの組み込み", interoperability: "相互運用性の実証", standard_adoption: "標準規格への採用", system_demo: "実運用環境でのシステム実証", system_deployment: "システムの運用開始", operational_use: "継続運用"};
function annexBlockHtml(ev) {
  // 付記イベント (規程第11条の4): 材料の中で観測された出来事の記録。
  // レベルではないので、観測分がある場合のみ TRL 節の下に出す
  const ax = ev && ev.annex;
  if (!Array.isArray(ax) || !ax.length) return "";
  const rows = ax.map(e => `<p>・［${ANNEX_AXIS[e.axis] || e.axis}］ ${esc(ANNEX_KIND[e.kind] || e.kind)}${e.date ? `（${esc(e.date)}）` : ""}: ${esc(e.summary || "")}</p>`).join("");
  return `<p style="margin-top:10px">付記: 観測されたイベント</p>` + rows
    + `<p class="hint">導入（ARL）・統合（IRL）・システム（SRL）の観点で、評価の材料に実際に現れた出来事の記録です。成熟度の段階判定ではなく、TRL・総合点には影響しません（規程第3章の2）。</p>`;
}
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
  // 詳細・ドロワー用: 全関門について状態チップ (該当 / 非該当 /
  // 判定しない / 対象外) を出し、該当した関門には導出側が生成した
  // 根拠文 (数値・状態・出典材料) と定義を併記する。状態は導出の
  // status の直読みで、表示側で判定や理由は作らない
  if (!ev || !ev.barriers) return "";
  const rows = BRO.filter(k => ev.barriers[k]).map(k => {
    const b = ev.barriers[k];
    const st = BRS[b.status] || b.status;
    if (b.status === "hit")
      return `<li><b>${esc(BRL[k])}</b> <span class="brz">${esc(st)}</span><span>${esc(b.evidence || "")} — ${esc(BRD[k])}</span></li>`;
    return `<li><b>${esc(BRL[k])}</b> <span class="brz off">${esc(st)}</span></li>`;
  }).join("");
  if (!rows) return "";
  return `<p style="margin-top:10px">関門</p><ul class="pts">${rows}</ul>`
    + `<p class="hint">関門は一次情報（治験・財務）の証拠条件から導出した表示専用の判定です（barriers/v1）。採点・選定・企業の層の判定には使いません。関門の定義は NEDO によります。</p>`;
}
const STGO = ["rd", "demo", "production", "commercial"];
const TAGO = [...CO, "photonics", "medtech", "earth_observation", "constellation", "space_transport", "lunar", "debris_removal", "mrna", "cell_therapy", "gene_therapy", "antibody", "radiopharm", "vaccine", "regenerative_medicine", "diagnostics", "drug_ai", "surgical_robot", "power_semi", "advanced_packaging", "memory", "semi_equipment", "ai_infra", "voice_ai", "industrial_robotics", "warehouse_automation", "autonomous_mobility", "humanoid", "hydrogen", "ess", "ev_charging", "solid_state_battery", "battery_materials", "perovskite", "fusion", "smr", "quantum_computing", "quantum_comm", "quantum_sensing"];  // タグの表示順 (分野の後に固有タグ)
const tagLabel = t => CL[t] || TL[t] || t;
const primEnt = p => p.entities.find(e => e.strength === "strong") || p.entities[0] || null;
const marketOf = p => { const e = primEnt(p); return e ? e.market || "JP" : null; };
const sectorOf = p => { const e = primEnt(p); return e ? e.sector || null : null; };
const exchangeOf = p => { const e = primEnt(p); return e ? e.exchange || null : null; };
/* 取引所の並び (チップと「取引所別」ソートで共用): 掲載中 (除外を除く) の
   企業数 の多い順、同数は名前順。辞書に37種あり固定表は持たない */
let EXO = null;
function excOrder() {
  if (EXO) return EXO;
  const cnt = {};
  for (const p of P) { if (p.display_status === "rejected") continue; for (const e of p.entities || []) if (e.exchange) cnt[e.exchange] = (cnt[e.exchange] || 0) + 1; }
  EXO = Object.keys(cnt).sort((a, b) => cnt[b] - cnt[a] || (a < b ? -1 : 1));
  return EXO;
}
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
const stSel = new Set(), dirSel = new Set(), mkSel = new Set(), excSel = new Set(), scSel = new Set(), tagSel = new Set(), stgSel = new Set(), tierSel = new Set();  // 状態・方向性・国・取引所・分野・タグ・段階・企業の層の選択状態 (空なら絞らない)
const ownSel = new Set();  // 自分の表示 (新着・注目) の選択状態。グループ内は OR で効く
const genSel = new Set();  // 評価の世代の選択状態 ("cur" = 現行世代のみ。空なら絞らない)
// 候補の世代は長期評価を主に見て、長期が無ければ短期 (event) 評価の
// 世代で見る。世代の印が無い候補 (未評価など) は現行世代とはみなさない
const genOf = p => (p.long && p.long.gen) || (p.event && p.event.gen) || null;
const isCurGen = p => { const g = genOf(p); return !!(g && g.current); };
const holdSel = new Set();  // 保留の理由 (long.hold_cause) の選択状態。空なら絞らない
const moveSel = new Set();  // 前回比の選択状態 ("any" / "up" / "down")。空なら絞らない
const histSel = new Set();  // 履歴の選択状態 ("first" / "moved" / "band" / "stale")。空なら絞らない
/* 前回評価との差分 (純粋関数)。TRL を主軸に方向を決め、TRL が同点の
   ときだけ総合点の増減で決める。両方が逆向きに動いたものは "mixed" で、
   「変動あり」にだけ含める (上昇でも下降でもないため) */
const dTrlOf = p => { const pv = p.long && p.long.prev, ct = lvl(p); return pv && pv.trl_level != null && ct != null ? ct - pv.trl_level : 0; };
const dTotOf = p => { const pv = p.long && p.long.prev; return pv && pv.total != null && p.long.total != null ? p.long.total - pv.total : 0; };
function moveOf(p) {
  if (!(p.long && p.long.prev)) return null;
  const dt = dTrlOf(p), ds = dTotOf(p);
  if (!dt && !ds) return null;
  if ((dt > 0 && ds < 0) || (dt < 0 && ds > 0)) return "mixed";
  return (dt > 0 || ds > 0) ? "up" : "down";
}
let evWin = null;           // 評価日の窓: null=絞らない / "latest"=最新の評価日と同日 / 7 / 30=最新の評価時刻から日数以内
let evalMaxTs = null;       // データ内で最新の評価時刻 (load で確定する)
const evalTs = p => { const t = (p.long && p.long.evaluated_at) || (p.event && p.event.evaluated_at) || null; const ms = t ? Date.parse(t) : NaN; return Number.isNaN(ms) ? null : ms; };
/* 評価日の窓に入るか (純粋関数)。窓の基準は暦の今日ではなくデータ内の
   最新の評価時刻で、データの鮮度が変わっても意味が変わらないようにする */
function evalInWindow(ts, maxTs, win) {
  if (win == null) return true;
  if (ts == null || maxTs == null) return false;
  if (win === "latest") return new Date(ts).toISOString().slice(0, 10) === new Date(maxTs).toISOString().slice(0, 10);
  return ts >= maxTs - win * 86400000;
}
/* 総合点の範囲に入るか。範囲が指定されたら、総合点の無い候補 (未評価・
   保留で未確定) は範囲の外として扱う (TRL の範囲指定と同じ考え方) */
function scoreInRange(p) {
  const a = $("#fsn").value, b = $("#fsm").value;
  if (a === "" && b === "") return true;
  const t = p.long && typeof p.long.total === "number" ? p.long.total : null;
  if (t == null) return false;
  if (a !== "" && t < Number(a)) return false;
  if (b !== "" && t > Number(b)) return false;
  return true;
}
function inR(t) {
  if (!trlSel.size) return true;
  return ok(t) ? trlSel.has(t) : trlSel.has("u");
}
function haystack(p) {
  const items = p.long ? Object.values(p.long.items).flatMap(i => i.missing_info) : [];
  const facets = p.entities.flatMap(e => [...(e.tags || []), ...(e.tags || []).map(tagLabel), CL[e.sector] || "", ML[e.market || "JP"] || "", STG[e.stage] || ""]);
  return [p.title, p.title_ja || "", p.source, p.abstract, p.abstract_ja || "", ...(p.fields || []), ...p.entities.map(e => e.name), ...p.entities.map(e => e.product_name || ""), ...facets, ...items].join(" ").toLowerCase();
}
/* 検索クエリの解釈 (純粋関数)。空白区切りで、key:value 形式の語は条件に、
   -語 は除外に、それ以外は全文検索の語にする (全て AND)。
   key: tier / trl (7 または 7-9) / dir / status / model / src /
   item (item:monetization>=60) / missing (不足項目の key) */
function parseQuery(q) {
  const out = {text: [], excl: [], conds: []};
  for (const w of String(q || "").toLowerCase().split(/\s+/).filter(Boolean)) {
    if (w.startsWith("-") && w.length > 1) { out.excl.push(w.slice(1)); continue; }
    const m = w.match(/^([a-z]+):(.+)$/);
    if (m && ["tier", "trl", "dir", "status", "model", "item", "missing", "src"].includes(m[1])) out.conds.push({key: m[1], val: m[2]});
    else out.text.push(w);
  }
  return out;
}
function condOk(p, c) {
  const v = c.val;
  switch (c.key) {
    case "tier": return p.entities.some(e => dtierOf(e) === v);
    case "trl": { const L = lvl(p); if (L == null) return false; const r = v.split("-").map(Number); return r.length === 2 ? (L >= r[0] && L <= r[1]) : L === r[0]; }
    case "dir": return (dirOf(p) || "none") === v;
    case "status": return p.display_status === v;
    case "model": return !!(p.long && p.long.llm_model && p.long.llm_model.toLowerCase().includes(v));
    case "src": return (p.source || "").toLowerCase().includes(v);
    case "missing": return !!(p.long && Array.isArray(p.long.critical_missing) && p.long.critical_missing.includes(v));
    case "item": {
      const im = v.match(/^([a-z_]+)(>=|<=|>|<|=)(\d+(?:\.\d+)?)$/);
      if (!im || !p.long || !p.long.items || !p.long.items[im[1]]) return false;
      const s = p.long.items[im[1]].score;
      if (typeof s !== "number") return false;
      const n = Number(im[3]);
      return im[2] === ">=" ? s >= n : im[2] === "<=" ? s <= n : im[2] === ">" ? s > n : im[2] === "<" ? s < n : s === n;
    }
  }
  return true;
}
function queryOk(p, q) {
  const hs = haystack(p);
  return q.text.every(w => hs.includes(w)) && q.excl.every(w => !hs.includes(w)) && q.conds.every(c => condOk(p, c));
}
/* 検索欄が /pattern/ 形式なら正規表現として解釈する (壊れた正規表現は通常検索に落とす) */
function regexOf(q) {
  const m = String(q || "").trim().match(/^\/(.+)\/([a-z]*)$/s);
  if (!m) return null;
  try { return new RegExp(m[1], m[2].includes("i") ? m[2] : m[2] + "i"); } catch (e) { return null; }
}
function filtered() {
  const q = $("#q").value.trim().toLowerCase(), o = $("#fo").value;
  const re = regexOf($("#q").value), pq = re ? null : parseQuery(q);
  const r = P.filter(p => (re ? re.test(haystack(p)) : queryOk(p, pq))
    && !banned(p)
    && !chipBans.fsc.has(p.display_status)
    && !(ok(lvl(p)) ? trlBan.has(lvl(p)) : trlBan.has("u"))
    && !chipBans.fdc.has(dirOf(p) || "none")
    && !(p.display_status === "hold" && p.long && chipBans.fhcc.has(p.long.hold_cause))
    && !p.entities.some(e => chipBans.fmc.has(e.market || "JP"))
    && !p.entities.some(e => chipBans.fexc.has(e.exchange))
    && !p.entities.some(e => chipBans.fcc.has(e.sector))
    && !p.entities.some(e => (e.tags || []).some(t => chipBans.fgc.has(t)))
    && !p.entities.some(e => chipBans.fstc.has(e.stage))
    && !p.entities.some(e => chipBans.ftierc.has(dtierOf(e)))
    && !(chipBans.fgenc.size && isCurGen(p))
    && !(() => { const m = moveOf(p); return !!m && (chipBans.fmvc.has("any") || chipBans.fmvc.has(m)); })()
    && ![...chipBans.fhistc].some(v => { const f = histFacts(p); return (v === "first" && f.first) || (v === "moved" && f.moved) || (v === "band" && f.bandCross) || (v === "dirflip" && f.dirFlip) || (v === "edge" && isEdge(p)) || (v === "stale" && p.display_status === "final" && p.long && p.long.evaluated_at && evalMaxTs && evalMaxTs - Date.parse(p.long.evaluated_at) > 30 * 864e5); })
    && !((chipBans.fown.has("new") && isNewEval(p)) || (chipBans.fown.has("pin") && pins.has(p.id)) || (chipBans.fown.has("seen") && seen.has(p.id)) || (chipBans.fown.has("unseen") && !seen.has(p.id)))
    && (stSel.size ? stSel.has(p.display_status) : p.display_status !== "rejected")  // 除外はチップを明示的に押した時だけ混ざる
    && inR(lvl(p))
    && (!dirSel.size || dirSel.has(dirOf(p) || "none"))
    && scoreInRange(p)
    && evalInWindow(evalTs(p), evalMaxTs, evWin)
    && ![...chipBans.fevc].some(v => evalInWindow(evalTs(p), evalMaxTs, v === "latest" ? "latest" : Number(v)))
    && (!holdSel.size || (p.display_status === "hold" && p.long && holdSel.has(p.long.hold_cause)))
    && (!moveSel.size || (() => { const m = moveOf(p); return !!m && (moveSel.has("any") || moveSel.has(m)); })())
    && (!histSel.size || (() => { const f = histFacts(p); return (histSel.has("first") && f.first)
      || (histSel.has("moved") && f.moved) || (histSel.has("band") && f.bandCross)
      || (histSel.has("dirflip") && f.dirFlip)
      || (histSel.has("edge") && isEdge(p))
      || (histSel.has("stale") && p.display_status === "final" && p.long && p.long.evaluated_at && evalMaxTs && evalMaxTs - Date.parse(p.long.evaluated_at) > 30 * 864e5); })())
    && (!mkSel.size || p.entities.some(e => mkSel.has(e.market || "JP")))
    && (!excSel.size || p.entities.some(e => excSel.has(e.exchange)))
    && (!scSel.size || p.entities.some(e => scSel.has(e.sector)))
    && (!tagSel.size || p.entities.some(e => (e.tags || []).some(t => tagSel.has(t))))
    && (!stgSel.size || p.entities.some(e => stgSel.has(e.stage)))
    && (!tierSel.size || p.entities.some(e => tierSel.has(dtierOf(e))))
    && (!genSel.size || isCurGen(p))
    && (!ownSel.size || (ownSel.has("new") && isNewEval(p)) || (ownSel.has("pin") && pins.has(p.id)) || (ownSel.has("seen") && seen.has(p.id)) || (ownSel.has("unseen") && !seen.has(p.id))));
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
    : o === "exchange" ? (ord(exchangeOf(a), excOrder()) - ord(exchangeOf(b), excOrder())) || byStatus(a, b)
    : o === "sector" ? (ord(sectorOf(a), CO) - ord(sectorOf(b), CO)) || byStatus(a, b)
    : o === "kind" ? ((kindOf(a) === "event") - (kindOf(b) === "event")) || byStatus(a, b)
    : o === "trl" ? (ok(lvl(b)) ? lvl(b) : -1) - (ok(lvl(a)) ? lvl(a) : -1)
    : o === "trl_asc" ? (ok(lvl(a)) ? lvl(a) : 99) - (ok(lvl(b)) ? lvl(b) : 99)
    : o === "move" ? (Math.abs(dTrlOf(b)) - Math.abs(dTrlOf(a))) || (Math.abs(dTotOf(b)) - Math.abs(dTotOf(a))) || byStatus(a, b)
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
    banModeBtn(id);  // innerHTML で作り直したので BAN チップを末尾に戻す
  };
  const base = P.filter(p => p.display_status !== "rejected");  // 選択肢は通常の一覧 (除外を除く) にあるものだけ
  fill("fmc", mkSel, new Set(base.flatMap(p => p.entities.map(e => e.market || "JP"))), MO, ML);
  fill("fexc", excSel, new Set(base.flatMap(p => p.entities.map(e => e.exchange).filter(Boolean))), excOrder(), {});
  fill("fcc", scSel, new Set(base.flatMap(p => p.entities.map(e => e.sector).filter(Boolean))), CO, CL);
  fill("fgc", tagSel, new Set(base.flatMap(p => p.entities.flatMap(e => e.tags || []))), TAGO, {...CL, ...TL});
  fill("fstc", stgSel, new Set(base.flatMap(p => p.entities.map(e => e.stage).filter(Boolean))), STGO, STG);
  fill("ftierc", tierSel, new Set(base.flatMap(p => p.entities.map(dtierOf))), ["core", "conditional", "look", "see"], DTL);
  // 方向性のチップには母数 (除外を除く掲載分) での件数を併記する。
  // 数える対象は絞り込み前の母集団で、チップの選択状態には依存しない
  const dlabels = {positive: DL.positive, negative: DL.negative, mixed: DL.mixed, none: "未判定"};
  document.querySelectorAll("#fdc button:not([data-banmode])").forEach(b => {
    const v = b.dataset.v;
    b.textContent = `${dlabels[v] || v} [${base.filter(p => (dirOf(p) || "none") === v).length}]`;
  });
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
/* 同分野・同TRLの母集団との項目比較 (純粋関数)。他の選定可能候補で主企業の
   分野と TRL が同じものの項目中央値と、この候補の項目点の差を返す */
function peerDiffs(p) {
  const sec = p.entities && p.entities[0] && p.entities[0].sector;
  const L = lvl(p);
  if (!sec || L == null || !p.long) return null;
  const peers = P.filter(x => x !== p && x.display_status === "final" && x.long
    && x.entities && x.entities[0] && x.entities[0].sector === sec && lvl(x) === L);
  if (!peers.length) return null;
  const diffs = {};
  for (const k of Object.keys(p.long.items)) {
    const mine = p.long.items[k] && p.long.items[k].score;
    if (typeof mine !== "number") continue;
    const vals = peers.map(x => x.long.items[k] && x.long.items[k].score).filter(v => typeof v === "number");
    if (vals.length) diffs[k] = Math.round((mine - statsOf(vals).median) * 10) / 10;
  }
  return Object.keys(diffs).length ? {n: peers.length, sector: sec, trl: L, diffs} : null;
}
function peerDiffHtml(p) {
  const pd = peerDiffs(p);
  if (!pd) return "";
  const rows = Object.entries(pd.diffs).map(([k, d]) => `${label(k)} ${d > 0 ? "+" : ""}${d}`).join(" / ");
  return `<section class="ds"><h4>同分野・同TRLとの比較</h4><p class="hint">${esc(CL[pd.sector] || pd.sector)}・TRL ${pd.trl} の選定可能な他候補 ${pd.n}件の項目中央値との差です。</p><p>${esc(rows)}</p></section>`;
}
function card(p) {
  const s = p.id === sel;
  const ents = p.entities.filter(e => e.strength === "strong").length;
  const facet = facetText(p);
  const diffTxt = prevDiffTxt(p);
  return `<article class="card ${s ? "sel" : ""}${VMODE && VSEL.has(p.id) ? " vsel" : ""}" data-id="${esc(p.id)}"><div class="main"><div style="min-width:0"><div class="top"><span class="${src(p.source)}">${srcName(p.source)}</span><span class="date">${esc(p.published || "")}</span>${p.retrieved_at ? `<span class="date">収集 ${esc(String(p.retrieved_at).slice(0, 10))}</span>` : ""}${LANG === "ja" && srcOf(p) ? `<span class="date">訳: ${esc(srcOf(p))}</span>` : ""}${p.long && p.long.llm_model ? `<span class="date">評価: ${esc(p.long.llm_model)}</span>` : ""}${genBadge(p.long) || genBadge(p.event)}${isNewEval(p) ? `<span class="nw">新着</span>` : ""}${bookedState(p) ? `<span class="rsv">予約</span>` : ""}</div><h3 class="title">${esc(dispT(p))}</h3><p class="abs">${esc(dispA(p))}</p><div class="tags">${(p.fields || []).slice(0, 4).map(t => `<span class="tag">${esc(t)}</span>`).join("")}</div><div class="mi"><span>技術成熟度 <strong>${fmtScore(sc(p, "technology_readiness"))}</strong></span><span>技術的優位性 <strong>${fmtScore(sc(p, "technological_advantage"))}</strong></span><span>関連企業 <strong>${ents ? ents + "社" : "未特定"}</strong></span>${facet ? `<span>国・分野 <strong>${esc(facet)}</strong></span>` : ""}${diffTxt ? `<span>前回比 <strong>${esc(diffTxt)}</strong></span>` : ""}${RANK.has(p.id) ? `<span>並び <strong>${esc(RANK.get(p.id))}</strong></span>` : ""}${evShort(p) ? `<span>イベント評価 <strong>${esc(evShort(p))}</strong></span>` : ""}${seen.has(p.id) ? `<span>既読</span>` : ""}</div>${barrierChipsHtml(p.long)}</div><div class="side">${stB(p.display_status)}${dirB(p)}${trl(lvl(p))}<button type="button" class="pin ${pins.has(p.id) ? "on" : ""}" data-pin="${esc(p.id)}" aria-pressed="${pins.has(p.id) ? "true" : "false"}" title="注目ピン (この端末に保存)">${pins.has(p.id) ? "★" : "☆"}</button></div></div><div class="foot"><span>${p.long && p.long.critical_missing.length ? `必須の不足 ${p.long.critical_missing.length}件` : p.display_status === "pending" ? (p.next_up ? "次回評価予定" : "評価待ち") : p.display_status === "rejected" ? "除外" + (p.screen_reason ? ": " + (SR[p.screen_reason] || p.screen_reason) : "") : ""}</span><span class="facts"><button type="button" class="clink" data-ccite="${esc(p.id)}">出典コピー</button><button type="button" class="clink" data-clink="${esc(p.id)}">リンクコピー</button><a class="tg" href="detail.html?id=${encodeURIComponent(p.id)}">詳細を見る →</a></span></div></article>`;
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

// 端末内の状態 (注目ピンと新着の基準時刻)。サーバを持たない静的サイト
// なので端末ごとに保持する (調査メモと同じ)。可用性の担保として、
// 注目ピンと新着の基準時刻は共有リンク (?pins=候補ID,...&seen=ISO時刻)
// で別端末へ持ち運べるようにする
let pins = new Set();
try { pins = new Set(JSON.parse(localStorage.getItem("trl-pins") || "[]")); } catch (e) { /* 壊れた保存値は空として扱う */ }
let seen = new Set();
try { seen = new Set(JSON.parse(localStorage.getItem("trl-seen") || "[]")); } catch (e) { /* 壊れた保存値は空として扱う */ }
let lastMarked = null;
/* 詳細を開いた候補を既読にし、最近見た候補 (直近8件) を端末に記録する */
function markSeen(id) {
  if (!id || id === lastMarked) return;
  lastMarked = id;
  seen.add(id);
  try {
    localStorage.setItem("trl-seen", JSON.stringify([...seen].slice(-500)));
    const rec = [id, ...JSON.parse(localStorage.getItem("trl-recent") || "[]").filter(x => x !== id)].slice(0, 8);
    localStorage.setItem("trl-recent", JSON.stringify(rec));
  } catch (e) { /* 保存不可でも表示は続ける */ }
  recentBand();
}
function recentBand() {
  const el = $("#recentband");
  if (!el) return;
  let rec = [];
  try { rec = JSON.parse(localStorage.getItem("trl-recent") || "[]"); } catch (e) { /* 同上 */ }
  const rows = rec.map(id => P.find(p => p.id === id)).filter(Boolean);
  if (!rows.length) { el.hidden = true; return; }
  el.innerHTML = `<span>最近見た候補</span>` + rows.map(p => `<a class="tz" href="detail.html?id=${encodeURIComponent(p.id)}">${esc(candName(p))}</a>`).join("");
  el.hidden = false;
}
function savePins() { try { localStorage.setItem("trl-pins", JSON.stringify([...pins])); } catch (e) { /* 保存不可でも表示は続ける */ } }
function togglePin(id) { if (!id) return; if (pins.has(id)) pins.delete(id); else pins.add(id); savePins(); render(); pinBand(); }
/* BANした瞬間の軽い振動 (スマホのみ。対応しない環境では何もしない) */
const buzz = () => { try { if (navigator.vibrate) navigator.vibrate(15); } catch (e) { /* 非対応は無視 */ } };
/* 短い通知 (キー操作の結果を返すためだけの小さな帯) */
function toast(msg, undoFn) {
  let el = $("#toast");
  if (!el) { el = document.createElement("div"); el.id = "toast"; el.className = "toast"; document.body.appendChild(el); }
  el.textContent = msg; el.hidden = false;
  if (undoFn) {
    const b = document.createElement("button");
    b.type = "button"; b.className = "tz"; b.textContent = "戻す";
    b.addEventListener("click", () => { clearTimeout(toast._t); el.hidden = true; undoFn(); });
    el.appendChild(b);
  }
  clearTimeout(toast._t); toast._t = setTimeout(() => { el.hidden = true; }, undoFn ? 6000 : 2600);
}
/* キー操作からのBAN: 選択中の候補の企業 (指名BAN) / 市場 (市場チップのBAN) を切り替える */
function banKeyOf(id, kind) {
  const p = P.find(x => x.id === id);
  if (!p) return;
  const e = (p.entities || []).find(x => x.strength === "strong");
  if (kind === "company") {
    if (!e) { toast("企業リンクが無い候補は企業BANできません"); return; }
    const i = BANS.findIndex(b => b.t === "company" && b.v === e.name);
    if (i >= 0) { const rm = BANS[i]; BANS.splice(i, 1); toast(`企業BANを解除: ${e.name}`, () => { BANS.splice(Math.min(i, BANS.length), 0, rm); saveBans(); buildBanUI(); render(); }); }
    else { const nb = {t: "company", v: e.name, on: true}; BANS.push(nb); buzz(); toast(`企業をBAN: ${e.name}`, () => { const j = BANS.indexOf(nb); if (j >= 0) BANS.splice(j, 1); saveBans(); buildBanUI(); render(); }); }
    saveBans(); buildBanUI(); render();
  } else {
    if (!e || !e.market) { toast("市場が無い候補は市場BANできません"); return; }
    if (chipBans.fmc.has(e.market)) { chipBans.fmc.delete(e.market); toast(`市場BANを解除: ${e.market}`, () => { chipBans.fmc.add(e.market); saveChipBans(); syncChips("fmc", mkSel); render(); }); }
    else { chipBans.fmc.add(e.market); buzz(); toast(`市場をBAN: ${e.market}`, () => { chipBans.fmc.delete(e.market); saveChipBans(); syncChips("fmc", mkSel); render(); }); }
    saveChipBans(); syncChips("fmc", mkSel); render();
  }
}
/* 比較バスケット: c キーで候補を積み (最大4件)、帯から横並び比較へ渡す */
let basket = [];
try { basket = JSON.parse(localStorage.getItem("trl-basket") || "[]"); } catch (e) { basket = []; }
function saveBasket() { try { localStorage.setItem("trl-basket", JSON.stringify(basket)); } catch (e) { /* 保存できなくても動く */ } }
function toggleBasket(id) {
  if (!id) return;
  const i = basket.indexOf(id);
  if (i >= 0) { basket.splice(i, 1); toast("比較バスケットから外しました"); }
  else if (basket.length >= 4) { toast("比較バスケットは4件までです"); return; }
  else { basket.push(id); toast(`比較バスケットに追加 (${basket.length}件)`); }
  saveBasket(); cmpBand();
}
function cmpBand() {
  const el = $("#cmpband");
  if (!el) return;
  basket = basket.filter(id => P.some(p => p.id === id));
  if (!basket.length) { el.hidden = true; return; }
  el.innerHTML = `<span>比較バスケット <strong>${basket.length}件</strong></span>`
    + basket.map(id => { const p = P.find(x => x.id === id); return `<a class="tz" href="detail.html?id=${encodeURIComponent(id)}">${esc(p ? candName(p) : id)}</a>`; }).join("")
    + `<a class="tz" href="compare.html?ids=${basket.map(encodeURIComponent).join(",")}">横並び比較を開く</a>`
    + `<button class="btn quiet" type="button" id="cmpclear">空にする</button>`;
  el.hidden = false;
  const cc = $("#cmpclear");
  if (cc) cc.addEventListener("click", () => { basket = []; saveBasket(); cmpBand(); });
}
/* 複数選択モード (v キー): カードのクリックが選択でなく集合への追加になる。
   溜めた集合に一括でピン/バスケット/企業BANを適用する */
let VMODE = false;
const VSEL = new Set();
function toggleVMode() {
  VMODE = !VMODE;
  if (!VMODE) VSEL.clear();
  vBar(); render();
  toast(VMODE ? "複数選択モード: カードをクリックで選ぶ" : "複数選択モードを終了");
}
function toggleVSel(id) { if (!id) return; if (VSEL.has(id)) VSEL.delete(id); else VSEL.add(id); vBar(); render(); }
function vBar() {
  const el = $("#vbar");
  if (!el) return;
  if (!VMODE) { el.hidden = true; return; }
  el.innerHTML = `<span>複数選択 <strong>${VSEL.size}件</strong></span>`
    + `<button class="btn quiet" type="button" data-va="pin">一括ピン</button>`
    + `<button class="btn quiet" type="button" data-va="basket">バスケットへ</button>`
    + `<button class="btn quiet" type="button" data-va="ban">企業BAN</button>`
    + `<button class="btn quiet" type="button" data-va="clear">選択解除</button>`
    + `<button class="btn quiet" type="button" data-va="exit">終了 (v)</button>`;
  el.hidden = false;
}
document.addEventListener("click", e => {
  const b = e.target.closest && e.target.closest("[data-va]");
  if (!b) return;
  const ids = [...VSEL];
  if (b.dataset.va === "pin") { for (const id of ids) if (!pins.has(id)) pins.add(id); savePins(); toast(`${ids.length}件をピン留め`); }
  else if (b.dataset.va === "basket") { let added = 0; for (const id of ids) { if (!basket.includes(id) && basket.length < 4) { basket.push(id); added++; } } saveBasket(); cmpBand(); toast(added ? `バスケットに ${added}件追加` : "バスケットは4件までです"); }
  else if (b.dataset.va === "ban") { let n = 0; for (const id of ids) { const p = P.find(x => x.id === id); const en = p && (p.entities || []).find(x => x.strength === "strong"); if (en && !BANS.some(x => x.t === "company" && x.v === en.name)) { BANS.push({t: "company", v: en.name, on: true}); n++; } } if (n) { saveBans(); buildBanUI(); buzz(); } toast(n ? `${n}社を企業BAN` : "企業BANできる候補がありません"); }
  else if (b.dataset.va === "clear") { VSEL.clear(); }
  else if (b.dataset.va === "exit") { toggleVMode(); return; }
  vBar(); render();
});
/* あとで読む: ピン (注目) とは別の「後で目を通す」印。端末に保存する */
let later = new Set();
try { later = new Set(JSON.parse(localStorage.getItem("trl-later") || "[]")); } catch (e) { later = new Set(); }
function saveLater() { try { localStorage.setItem("trl-later", JSON.stringify([...later])); } catch (e) { /* 保存できなくても動く */ } }
function toggleLater(id) {
  if (later.has(id)) { later.delete(id); toast("あとで読むから外しました"); }
  else { later.add(id); toast("あとで読むに追加"); }
  saveLater(); laterBand(); render();
}
function laterBand() {
  const el = $("#laterband");
  if (!el) return;
  const rows = [...later].map(id => P.find(p => p.id === id)).filter(Boolean);
  if (!rows.length) { el.hidden = true; return; }
  el.innerHTML = `<span>あとで読む <strong>${rows.length}件</strong></span>`
    + rows.map(p => `<a class="tz" href="detail.html?id=${encodeURIComponent(p.id)}">${esc(candName(p))}</a>`).join("");
  el.hidden = false;
}
/* 文字サイズ (表示の拡大率)。対応しないブラウザでは何も起きない */
const ZOOMS = ["", "1.15", "1.3"];
function applyZoom() {
  let z = "";
  try { z = localStorage.getItem("trl-zoom") || ""; } catch (e) { /* 保存不可なら標準 */ }
  document.body.style.zoom = z;
}
function cycleZoom() {
  let z = "";
  try { z = localStorage.getItem("trl-zoom") || ""; } catch (e) { /* 同上 */ }
  const next = ZOOMS[(ZOOMS.indexOf(z) + 1) % ZOOMS.length];
  try { localStorage.setItem("trl-zoom", next); } catch (e) { /* 同上 */ }
  document.body.style.zoom = next;
}
/* ピン候補の現況まとめ: 状態・総合点・前回比・評価日を1行ずつ。
   値は候補の直読みで、並びはピンした順 (保存値の順) を保つ */
function pinBand() {
  const el = $("#pinband");
  if (!el) return;
  const rows = [...pins].map(id => P.find(p => p.id === id)).filter(Boolean);
  if (!rows.length) { el.hidden = true; return; }
  el.innerHTML = `<span>★注目（${rows.length}件）</span>` + rows.map(p => {
    const mv = moveOf(p);
    const mvTxt = mv === "up" ? " ↑前回より上昇" : mv === "down" ? " ↓前回より下降" : mv === "mixed" ? " ・前回と混在" : "";
    return `<a class="tz" href="detail.html?id=${encodeURIComponent(p.id)}">${esc(candName(p))}: ${esc(SL[p.display_status] || p.display_status)}${p.long && typeof p.long.total === "number" ? ` 総合 ${p.long.total}` : ""}${mvTxt}${p.long && p.long.evaluated_at ? ` (${esc(p.long.evaluated_at.slice(0, 10))})` : ""}</a>`;
  }).join("");
  el.hidden = false;
}
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
// 新着の基準は「前回のデータで最新だった評価時刻」。基準が前進するのは
// 新しい評価が来たときだけなので、訪問を繰り返しても、評価の増えない
// サイト再生成があっても、新着は次の新しい評価が来るまで消えない。
// 判定に使う値は読み込み時に確定させ、セッション中は固定する。
// 初回訪問では基準だけを置いてバッジは付けない (全部が新着になるため)
let newBase = null;    // 新着の基準時刻 (これより後の long 評価が新着)
let newSeenMax = null; // 基準を決めた時点で最新だった long 評価の時刻
try {
  newBase = localStorage.getItem("trl-new-base");
  newSeenMax = localStorage.getItem("trl-new-max");
  // 旧方式 (最終訪問時刻) の保存値があれば基準として引き継ぐ
  if (!newBase) newBase = localStorage.getItem("trl-last-visit");
  // 共有リンク (?seen=) で届いた基準時刻を取り込む。既読の統合なので、
  // 保存値と届いた値のうち遅い方を基準にする (ピンの統合と同じ考え方)。
  // 未来の時刻は「今」で頭打ちにする (まだ見ていない分まで既読にしない)
  const seenQ = new URLSearchParams(location.search).get("seen");
  if (seenQ && !Number.isNaN(Date.parse(seenQ))) {
    const seenIso = new Date(Math.min(Date.parse(seenQ), Date.now())).toISOString();
    if (!newBase || seenIso > newBase) newBase = seenIso;
    const u = new URL(location.href); u.searchParams.delete("seen");
    history.replaceState(null, "", u.pathname + u.search);
  }
} catch (e) { /* 保存不可なら新着判定は無効 */ }
const isNewEval = p => !!(newBase && p.long && p.long.evaluated_at && p.long.evaluated_at > newBase);
// 基準の前進規則 (純粋関数)。curMax は今回のデータで最新の long 評価時刻。
// 新しい評価が来ていたら基準を前回の最新時刻へ進め、来ていなければ据え置く。
// 基準は後退させない (共有リンクで新しい基準を取り込んだ直後の巻き戻し防止)
function advanceNewBase(base, seenMax, curMax) {
  if (!curMax) return [base, seenMax];
  if (!seenMax) return [base || curMax, curMax];
  if (curMax > seenMax) return [base && base > seenMax ? base : seenMax, curMax];
  return [base, seenMax];
}
// データ読み込み後に1回だけ呼び、基準を確定させて保存する
function resolveNewBase() {
  let curMax = null;
  for (const p of P) if (p.long && p.long.evaluated_at && (!curMax || p.long.evaluated_at > curMax)) curMax = p.long.evaluated_at;
  [newBase, newSeenMax] = advanceNewBase(newBase, newSeenMax, curMax);
  try {
    if (newBase) localStorage.setItem("trl-new-base", newBase);
    if (newSeenMax) localStorage.setItem("trl-new-max", newSeenMax);
  } catch (e) { /* 保存不可でも表示は続ける */ }
}
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
  markSeen(p.id);
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
  $("#det").innerHTML = `<div class="dc"><header class="dh"><div class="top"><span class="${src(p.source)}">${srcName(p.source)}</span>${stB(p.display_status)}${isNewEval(p) ? `<span class="nw">新着</span>` : ""}<button type="button" class="pin ${pins.has(p.id) ? "on" : ""}" id="pint" aria-pressed="${pins.has(p.id) ? "true" : "false"}" title="注目ピン (この端末に保存)">${pins.has(p.id) ? "★" : "☆"}</button></div><h3>${esc(dispT(p))}</h3><div class="mi"><span>公開日 ${esc(p.published || "—")}</span>${L ? `<span>評価日 ${esc(jst(L.evaluated_at))}</span>` : ""}${p.eval_count ? `<span>評価 ${p.eval_count} 回目</span>` : ""}<span><a href="compare.html?ids=${encodeURIComponent(p.id)}">横並び比較</a></span>${LL && LL.llm_model ? `<span>評価モデル ${esc(LL.llm_model)}</span>` : ""}${(() => { const es = (META.run && META.run.counts && META.run.counts.eval_elapsed_sec) || []; const hit = Array.isArray(es) ? es.find(x => x.candidate_id === p.id) : null; return hit ? `<span>評価所要 ${Math.round(hit.seconds)}秒（最新ラン）</span>` : ""; })()}${(p.related_news || []).length || (p.related_financials || []).length ? `<span>根拠プール: ニュース ${(p.related_news || []).length}件（一次情報 ${(p.related_news || []).filter(n => n.origin === "企業発表").length}件）/ 財務 ${(p.related_financials || []).length}件</span>` : ""}${genBadge(L) || genBadge(EV)}</div>${showGen && L && L.gen && !L.gen.current ? `<p class="note" style="margin-top:8px">${esc(GEN_NOTE)}</p>` : ""}</header>
<section class="ds"><h4>研究概要</h4>${absHtml(dispA(p))}${LANG === "ja" && srcOf(p) ? `<p class="hint">日本語訳: ${esc(srcOf(p))}</p>` : ""}<div class="acts"><button class="btn sec" type="button" id="cpyo">原文をコピー</button>${(p.title_ja || p.abstract_ja) ? `<button class="btn sec" type="button" id="cpyj">日本語訳をコピー</button>` : ""}${p.abstract && p.abstract_ja ? `<button class="btn sec" type="button" id="absboth">原文と訳を並べる</button>` : ""}</div>${p.abstract && p.abstract_ja ? `<div id="absbothbox" hidden style="grid-template-columns:1fr 1fr;gap:12px"><div><p class="hint">原文</p><p>${esc(p.abstract)}</p></div><div><p class="hint">日本語訳</p><p>${esc(p.abstract_ja)}</p></div></div>` : ""}</section>
<section class="ds"><h4>技術成熟度（TRL）</h4>${trl(lvl(p), 1)}${tr && tr.reason ? `<p>${esc(tr.reason)}</p>` : ""}${tr && tr.level == null && tr.missing_info.length ? `<p class="hint">不足: ${tr.missing_info.map(esc).join(" / ")}</p>` : ""}${tr && tr.awaiting_info && tr.awaiting_info.length ? `<p class="hint">結果待ち: ${tr.awaiting_info.map(esc).join(" / ")}</p>` : ""}<p class="hint">TRLは資料から根拠付きで判断できた場合のみ表示します。技術成熟度スコアからの換算はしません。</p>${L ? annexBlockHtml(L) : ""}${L ? barrierBlockHtml(L) : ""}</section>
<section class="ds"><h4>技術評価</h4>${L ? G.technology.map(k => scoreRow(k, L.items[k])).join("") : `<p class="hint">未評価です。</p>`}</section>
<section class="ds"><h4>事業評価</h4>${L && L.business_stage === "pre_stage" ? `<p class="note">この技術は前商業段階(TRL 3以下)のため、事業項目は「段階未達」として不足とは区別しています。点が付いている項目はそのまま表示します。</p>` : ""}${L ? G.business.map(k => scoreRow(k, L.items[k])).join("") : `<p class="hint">未評価です。</p>`}${L && !strong.length ? `<p class="note">関連企業が特定できていないため、企業固有の項目は算出されません。総合点は6項目すべてが揃うまで確定せず、重みの再配分もしません。</p>` : ""}</section>
${L && REF.some(k => L.items[k]) ? `<section class="ds"><h4>参考項目</h4><p class="note">参考項目は採点して表示しますが、総合点・必須判定には含めません。根拠が無い場合は未算出となり、それを理由に保留にもなりません。</p>${REF.filter(k => L.items[k]).map(k => scoreRow(k, L.items[k])).join("")}</section>` : ""}
<section class="ds"><h4>総合点（長期）</h4><p>${total}</p>${L && L.total_scope === "technology" ? `<p class="note">これは技術評価の総合点です。事業項目は段階未達のため対象外で、技術軸の項目だけで算出しています。</p>` : ""}${holdCauseTxt(L) ? `<p class="note">${esc(holdCauseTxt(L))}</p>` : ""}${L && L.total_orig != null && L.total != null && L.total_orig !== L.total ? `<p class="note">評価当時の総合点は ${L.total_orig}${L.band_orig ? `（${esc(L.band_orig)}）` : ""} でした。上の総合点は、重みの改訂に合わせて現行の物差しで計算し直した値です。</p>` : ""}${L ? `<div class="acts"><button class="btn sec" type="button" id="cpye">評価をコピー</button><button class="btn obsidian" type="button" id="cpym">Markdownでコピー</button><button class="btn sec" type="button" id="cpyb">BibTeX風にコピー</button><button class="btn sec" type="button" id="cardimg">カード画像を保存</button></div>` : ""}</section>
${auditSectionHtml(p)}
${EV ? `<section class="ds"><h4>イベント評価（短期）</h4>${genBadge(EV) ? `<p style="margin-bottom:8px">${genBadge(EV)}</p>` : ""}${Object.keys(EV.items).map(k => scoreRow(k, EV.items[k])).join("")}<p>総合点 ${EV.total != null ? EV.total + " / 100(" + esc(EV.band || "") + ")" : "未確定（保留）"}</p></section>` : ""}
${L && L.prev ? `<section class="ds"><h4>前回評価との比較</h4>${prevCmp(L, lvl(p))}</section>` : ""}
${Array.isArray(p.history) && p.history.length >= 2 ? `<section class="ds"><h4>評価の推移</h4>${histHtml(p)}</section>` : ""}
<section class="ds"><h4>材料の方向性</h4>${L && L.direction ? `<p>${dirB(p)} ${esc(L.direction.reason || "")}</p><p class="hint">方向性は点数やTRLとは独立に、資料が示す出来事の追い風・逆風を判定したものです。</p>` : `<p class="hint">未判定です（方向を決める出来事が資料に無いか、旧基準の評価です）。</p>`}</section>
<section class="ds"><h4>不足している観点</h4>${L && L.critical_missing.length ? `<p class="m" style="color:#a96500;font-weight:700">必須項目の不足: ${L.critical_missing.map(k => esc(label(k))).join("、")}</p>` : ""}${pts(missAll, 1)}${waitAll.length ? `<p class="hint">結果待ち（不足ではなく、治験の結果公表など時間の経過でしか解けないもの）:</p><ul class="pts">${waitAll.map(x => `<li><b>◷</b><span>${esc(x)}</span></li>`).join("")}</ul>` : ""}${q && q.next_sources.length ? `<p class="hint">次に確認する情報源: ${q.next_sources.map(esc).join(" / ")}</p>` : ""}${q && q.reeval_conditions.length ? `<p class="hint">再評価の条件: ${q.reeval_conditions.map(esc).join(" / ")}</p>` : ""}</section>
${p.display_status === "error" && LL && LL.errors.length ? `<section class="ds"><h4>処理上の注意</h4>${pts(LL.errors, 1)}</section>` : ""}
${p.display_status === "rejected" ? `<section class="ds"><h4>除外理由</h4><p>${esc(p.screen_reason ? (SR[p.screen_reason] || p.screen_reason) : "理由は記録されていません")}</p><p class="hint">除外は関連性スクリーニングの機械的な判定で、評価 (採点) は行われていません。</p></section>` : ""}
${radarHtml(p)}${peerDiffHtml(p)}<section class="ds"><h4>関連企業</h4>${p.entities.map(e => `<span class="chip"><a class="colink" href="company.html?e=${encodeURIComponent(e.entity_id)}">${esc(e.name)}</a>${e.product_name ? "（" + esc(e.product_name) + "）" : ""}${e.market ? "・" + esc(ML[e.market] || e.market) : ""}${e.exchange ? "・" + esc(e.exchange) : ""}${e.sector ? "・" + esc(CL[e.sector] || e.sector) : ""}${(e.tags || []).length ? "・" + e.tags.map(t => esc(tagLabel(t))).join("・") : ""}${e.stage ? "・" + esc(STG[e.stage] || e.stage) : ""} ${e.strength === "strong" ? "" : "・弱"}${dtText(e) ? "・" + esc(dtText(e)) : ""}</span>`).join("") || '<span class="hint">関連企業は未特定です。</span>'}</section>
<section class="ds"><h4>調査メモ</h4><textarea id="note" aria-label="調査メモ" placeholder="仮説、確認したい点、次の調査などを記録...">${esc(p.id in noteDrafts ? noteDrafts[p.id] : getNote(p.id))}</textarea><div class="acts"><button class="btn sec" type="button" id="save">メモを保存</button><button class="btn sec" type="button" id="cpys">出典をコピー</button><a class="btn src" href="${esc(safeUrl(p.url))}" target="_blank" rel="noopener noreferrer">原文サイトを開く ↗</a></div><p id="msg" class="hint" aria-live="polite">メモはこの端末のブラウザにだけ保存されます。</p></section></div>`;
}
function render() {
  const r = filtered();
  $("#rc").textContent = r.length;
  VIEW = r;
  $("#empty").hidden = r.length !== 0;
  // おすすめ順の根拠: 並びは「状態の優先度 → TRL → 公開日」で決まるため、
  // 状態グループ内での順位をカードに出す (それ以外の並び順では出さない)
  RANK = new Map();
  if ($("#fo").value === "status") {
    const seen = {};
    for (const p of r) { const g = p.display_status; seen[g] = (seen[g] || 0) + 1; RANK.set(p.id, `${SL[g] || g}・${seen[g]}位`); }
  }
  if (!r.some(p => p.id === sel)) sel = r[0] ? r[0].id : null;
  $("#list").innerHTML = r.map(card).join("");
  detail();
  renderBanBand();
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
/* 監査の未処分件数 (純粋関数)。監査ページ・詳細パネルと同じ畳み方
   (系統ごとの最新バッチ × 未処分) で要確認 (グレーは含めない) を数える。
   処分の判定は詳細パネルと同じ decOf (端末内の処分を優先し、無ければ
   監査ブロック同梱の DB 側の有効な処分) を使うため、ページ間で数が揃う */
function auditOpenFlags(cands) {
  let n = 0;
  for (const p of cands || []) for (const blocks of [p.audit, p.audit_event]) {
    if (!blocks) continue;
    const lat = new Set(latestAuditBlocks(blocks));
    for (const a of blocks) {
      if (!lat.has(a)) continue;
      for (const e of a.entries || []) {
        const d = decOf(a, e);
        if (e.verdict !== "gray" && !(d && d.disposition)) n++;
      }
    }
  }
  return n;
}
function digestBand() {
  // 変化ダイジェスト: 最新ランで生まれた評価 (export の digest) を、
  // 新規と再評価に分けて出す。値は評価の直読みで、再評価には前回の
  // 値を併記するだけ (新しい判定は作らない)。digest が無い古い
  // data.json では枠ごと隠す
  const el = $("#dgst");
  if (!el) return;
  const dg = META && Array.isArray(META.digest) ? META.digest : [];
  if (!dg.length) { el.hidden = true; return; }
  const byId = new Map(P.map(p => [p.id, p]));
  const fmtPt = d => `${d.total != null ? d.total + "点" : (SL[d.status] || d.status)} / TRL ${d.trl_level != null ? d.trl_level : "未確定"}`;
  const row = d => {
    const p = byId.get(d.candidate_id);
    const name = p ? dispT(p) : d.candidate_id;
    const kind = d.kind === "event" ? "（イベント評価）" : "";
    const link = `<a href="detail.html?id=${encodeURIComponent(d.candidate_id)}">${esc(name)}</a>`;
    if (!d.prev) return `<span class="dgr"><b>新規</b> ${link}${kind} — ${esc(fmtPt(d))}</span>`;
    const dt = d.prev.trl_level != null && d.trl_level != null ? d.trl_level - d.prev.trl_level : 0;
    const arrow = dt ? ` <span class="${dt > 0 ? "td-up" : "td-dn"}">TRL ${d.prev.trl_level}→${d.trl_level}${dt > 0 ? "↑" : "↓"}</span>` : "";
    return `<span class="dgr"><b>再評価</b> ${link}${kind} — ${esc(fmtPt(d.prev))} → ${esc(fmtPt(d))}${arrow}</span>`;
  };
  const news = dg.filter(d => !d.prev).length;
  el.innerHTML = `<span>最新の実行の変化: 新規評価 ${news}件 / 再評価 ${dg.length - news}件</span>` + dg.map(row).join("");
  el.hidden = false;
}
/* 分布の集計 (純粋関数)。母数は掲載中 (除外を除く) の候補で、総合点は
   点数がある評価、TRL は error でない long 評価がある候補だけを数える */
/* 履歴から読める事実 (純粋関数)。history は古い順の軽い点で、最新と
   1つ前を比べる。点が無い項目は null のままにし、差は作らない */
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
/* 帯の境界近傍か (総合点が境目 20/40/60/80 から ±5点以内。±5 は帯幅20の1/4) */
function isEdge(p) {
  const h = Array.isArray(p.history) ? p.history : [];
  const cur = h.length ? h[h.length - 1] : null;
  const t = cur && cur.total != null ? cur.total : (p.long && p.long.total != null ? p.long.total : null);
  if (t == null) return false;
  return [20, 40, 60, 80].some(b => Math.abs(t - b) <= 5);
}
/* その候補の評価時点の列 (週間集計用)。履歴が無い候補は最新の評価だけ */
function evalPoints(p) {
  const h = Array.isArray(p.history) ? p.history : [];
  if (h.length) return h.map((pt, i) => ({at: pt.evaluated_at, first: i === 0}));
  if (p.long && p.long.evaluated_at) return [{at: p.long.evaluated_at, first: (p.eval_count || 1) <= 1}];
  return [];
}
const candName = p => { const e = (p.entities || []).find(x => x.strength === "strong"); return e ? e.name : (p.title || "").slice(0, 24); };
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
/* 変動の散布 (純粋関数): 再評価の (総合点の差, TRLの差) を座標にする。
   軸の範囲は総合点 ±50・TRL ±8 で固定し、範囲外は端に寄せる */
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
/* 項目別の未確定 (null) の分布: 最新の long 評価 (error を除く) のうち、
   その項目に点が付いていない候補の数。材料の不足がどこに出るかの観測用 */
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
/* 統計量 (純粋関数群)。定義は固定: パーセンタイルは type 7 (線形補間)、
   SD は標本標準偏差 (n-1)、Sn/Qn は Rousseeuw & Croux のロバスト尺度で
   有限標本修正は Sn が CR92 の表と予測式、Qn が robustbase の精密表
   (n<=12) と Qn.finite.c 多項式。2026-11-24 に R robustbase 0.99.2 と
   n=2..60 の掃引で照合済み (Qn の残差 2.0e-6 は R 側の定数切捨て分) */
function pct7(xs, p) {
  const s = [...xs].sort((a, b) => a - b);
  if (s.length === 1) return s[0];
  const k = (s.length - 1) * p, f = Math.floor(k), c = Math.min(f + 1, s.length - 1);
  return s[f] + (s[c] - s[f]) * (k - f);
}
const meanOf = xs => xs.reduce((a, b) => a + b, 0) / xs.length;
function sdOf(xs) { if (xs.length < 2) return null; const m = meanOf(xs); return Math.sqrt(xs.reduce((a, b) => a + (b - m) ** 2, 0) / (xs.length - 1)); }
const SN_FINITE = {2: 0.743, 3: 1.851, 4: 0.954, 5: 1.351, 6: 0.993, 7: 1.198, 8: 1.005, 9: 1.131};
const snCorr = n => SN_FINITE[n] !== undefined ? SN_FINITE[n] : (n % 2 === 1 ? n / (n - 0.9) : 1.0);
function snOf(xs) {
  const s = [...xs].sort((a, b) => a - b), n = s.length;
  if (n === 1) return 0;
  const inner = s.map(x => { const d = s.map(y => Math.abs(x - y)).sort((a, b) => a - b); return d[Math.floor(n / 2)]; });
  inner.sort((a, b) => a - b);
  return 1.1926 * snCorr(n) * inner[Math.floor((n + 1) / 2) - 1];
}
const QN_FINITE = {2: 0.399356, 3: 0.99365, 4: 0.51321, 5: 0.84401, 6: 0.6122, 7: 0.85877, 8: 0.66993, 9: 0.87344, 10: 0.72014, 11: 0.88906, 12: 0.75743};
function qnFiniteC(n) { const t = 1 / n; return n % 2 === 1 ? 1 + (1.60188 + (-2.1284 - 5.172 * t) * t) * t : 1 + (3.67561 + (1.9654 + (6.987 - 77 * t) * t) * t) * t; }
const qnCorr = n => QN_FINITE[n] !== undefined ? QN_FINITE[n] : 1 / qnFiniteC(n);
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
/* 母集団 (コホート): 既定は表示時点で選定可能 (final) の候補の最新の
   long 評価。カレンダーで基準日 (ASOF) を選ぶと、各候補についてその日
   以前の最新の long 評価を履歴から再構成し、その日時点で final だった
   候補だけを母集団にする。項目の並びは config の群定義に従う */
let COHORT = null, ITEM_KEYS = [], ASOF = null, CALM = null, CALOPEN = false;
function pointOf(p) {
  if (!p.long) return null;
  const items = {};
  for (const k of Object.keys(p.long.items || {})) items[k] = p.long.items[k] && p.long.items[k].score;
  return {evaluated_at: p.long.evaluated_at, status: p.long.status, total: p.long.total, items};
}
/* 基準日以前の最新の評価点 (純粋関数)。履歴は最新の評価も含む全件 */
function asOfPoint(p, asOf) {
  const pts = [...(p.history || []), ...(p.history && p.history.length ? [] : [pointOf(p)])]
    .filter(pt => pt && pt.evaluated_at && pt.evaluated_at.slice(0, 10) <= asOf);
  if (!pts.length) return null;
  pts.sort((a, b) => a.evaluated_at < b.evaluated_at ? -1 : 1);
  return pts[pts.length - 1];
}
/* 評価があった日の件数 (カレンダーのドット用、純粋関数) */
function evalDayCounts() {
  const m = new Map();
  const add = ts => { if (!ts) return; const d = ts.slice(0, 10); m.set(d, (m.get(d) || 0) + 1); };
  for (const p of P) {
    if (p.history && p.history.length) p.history.forEach(pt => add(pt.evaluated_at));
    else if (p.long) add(p.long.evaluated_at);
  }
  return m;
}
/* 月のマス目 (純粋関数): 月曜始まり、先行・後続の空白は null */
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
const fmt1 = v => v == null ? "—" : (Math.round(v * 10) / 10).toFixed(1);
function boxGeom(st, W = 170) { const X = v => Math.round((v / 100 * (W - 4) + 2) * 10) / 10; return {min: X(st.min), q1: X(st.q1), med: X(st.median), q3: X(st.q3), max: X(st.max)}; }
function boxSvg(st) {
  const b = boxGeom(st);
  return `<svg viewBox="0 0 170 18" width="170" height="18" role="img"><line x1="${b.min}" y1="9" x2="${b.max}" y2="9" stroke="var(--muted)"/><rect x="${b.q1}" y="4" width="${Math.max(1, b.q3 - b.q1)}" height="10" fill="var(--accent)" opacity="0.35" stroke="var(--accent)"/><line x1="${b.med}" y1="3" x2="${b.med}" y2="15" stroke="var(--accent)" stroke-width="2"/></svg>`;
}
/* 収集成功率の推移の幾何 (純粋関数): run_history の sources_ok /
   (sources_ok + sources_failed) を古い順に点列化する */
function rateGeom(hist, W = 260, H = 34) {
  const rates = (hist || []).map(r => {
    const c = r.counts || {}, ok = c.sources_ok, ng = c.sources_failed;
    return (typeof ok === "number" && typeof ng === "number" && ok + ng > 0) ? ok / (ok + ng) : null;
  }).filter(v => v !== null);
  return rates.map((v, i) => ({
    x: rates.length <= 1 ? Math.round(W / 2) : Math.round(i / (rates.length - 1) * (W - 4) + 2),
    y: Math.round((H - 4) - v * (H - 10)), rate: v}));
}
/* 定期実行の予定 (純粋関数): 火〜金 06:30 JST。fromMs より後の実行時刻を
   n 件返す (UTC ミリ秒)。cron の定義 (main.yaml) の写しで、表示専用 */
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
const fmtRunAt = t => new Date(t).toLocaleString("ja-JP", {timeZone: "Asia/Tokyo", month: "numeric", day: "numeric", weekday: "short", hour: "2-digit", minute: "2-digit"});
/* 前回見た後に新しい確定評価があれば知らせる帯 (開いたときの通知) */
/* BAN (除外のピックアップ): 企業・分野・出所を指名して一覧から外す。
   NOT検索の視覚版。各BANはスイッチで有効/無効を切り替えられる */
let BANS = [];
try { BANS = JSON.parse(localStorage.getItem("trl-bans") || "[]"); } catch (e) { BANS = []; }
function saveBans() { try { localStorage.setItem("trl-bans", JSON.stringify(BANS)); } catch (e) { /* 保存できなくても動く */ } }
function banMatch(p, b) {
  if (b.t === "company") return (p.entities || []).some(e => e.strength === "strong" && e.name === b.v);
  if (b.t === "market") return (p.entities || []).some(e => e.strength === "strong" && e.market === b.v);
  if (b.t === "exchange") return (p.entities || []).some(e => e.strength === "strong" && e.exchange === b.v);
  if (b.t === "sector") return (p.fields || []).includes(b.v);
  if (b.t === "source") return p.source === b.v;
  return false;
}
function banned(p) { return BANS.some(b => b.on && banMatch(p, b)); }
function banOptions(t) {
  const s = new Set();
  for (const p of P) {
    if (t === "company") { for (const e of p.entities || []) if (e.strength === "strong") s.add(e.name); }
    else if (t === "market") { for (const e of p.entities || []) if (e.strength === "strong" && e.market) s.add(e.market); }
    else if (t === "sector") { for (const f of p.fields || []) s.add(f); }
    else if (t === "source" && p.source) s.add(p.source);
  }
  return [...s].sort();
}
function buildBanUI() {
  const list = $("#banlist");
  if (!list) return;
  const TL = {company: "企業", market: "国・市場", exchange: "取引所", sector: "分野", source: "出所"};
  list.innerHTML = BANS.map((b, i) => `<div class="brow"><label class="sw"><input type="checkbox" data-ban="${i}" ${b.on ? "checked" : ""}><span>${esc(TL[b.t] || b.t)}: ${esc(b.v)}</span></label><button class="btn quiet" type="button" data-banx="${i}" aria-label="BANを取り消す">×</button></div>`).join("");
  const bc = $("#bancount");
  if (bc) bc.textContent = BANS.some(b => b.on) ? `（現在 ${P.filter(banned).length}件を除外中）` : "";
  const dl = $("#banvals"), ty = $("#bantype");
  if (dl && ty) dl.innerHTML = banOptions(ty.value).map(v => `<option value="${esc(v)}">`).join("");
  renderBanBand();
}
/* BAN帯: BAN中の企業・市場を一覧の上に常時出し、その場で戻せるようにする
   (BANした瞬間に候補が消えて、復元の入口が遠い問題への対処) */
function renderBanBand() {
  const el = $("#banband");
  if (!el) return;
  const comps = BANS.filter(b => b.on && b.t === "company");
  const mkts = [...chipBans.fmc];
  const excs = [...chipBans.fexc];
  if (!comps.length && !mkts.length && !excs.length) { el.hidden = true; el.innerHTML = ""; return; }
  const n = P.filter(p => banned(p) || (p.entities || []).some(e => e.strength === "strong" && e.market && chipBans.fmc.has(e.market)) || (p.entities || []).some(e => e.exchange && chipBans.fexc.has(e.exchange))).length;
  el.innerHTML = `<span><strong>BAN中の企業・市場・取引所</strong>（${n}件を除外中）</span>`
    + comps.map(b => `<button class="tz" type="button" data-unbanc="${esc(b.v)}">企業: ${esc(b.v)} ×</button>`).join("")
    + mkts.map(m => `<button class="tz" type="button" data-unbanm="${esc(m)}">市場: ${esc(m)} ×</button>`).join("")
    + excs.map(m => `<button class="tz" type="button" data-unbanx="${esc(m)}">取引所: ${esc(m)} ×</button>`).join("")
    + `<button class="tz" type="button" id="unbanall">全部戻す</button>`;
  el.hidden = false;
  el.querySelectorAll("[data-unbanc]").forEach(b => b.addEventListener("click", () => {
    const i = BANS.findIndex(x => x.t === "company" && x.v === b.dataset.unbanc);
    if (i >= 0) BANS.splice(i, 1);
    saveBans(); buildBanUI(); render();
  }));
  el.querySelectorAll("[data-unbanm]").forEach(b => b.addEventListener("click", () => {
    chipBans.fmc.delete(b.dataset.unbanm);
    saveChipBans(); syncChips("fmc", mkSel); buildBanUI(); render();
  }));
  el.querySelectorAll("[data-unbanx]").forEach(b => b.addEventListener("click", () => {
    chipBans.fexc.delete(b.dataset.unbanx);
    saveChipBans(); syncChips("fexc", excSel); buildBanUI(); render();
  }));
  const all = el.querySelector("#unbanall");
  if (all) all.addEventListener("click", () => {
    for (let i = BANS.length - 1; i >= 0; i--) if (BANS[i].t === "company" && BANS[i].on) BANS.splice(i, 1);
    chipBans.fmc.clear();
    chipBans.fexc.clear();
    saveBans(); saveChipBans(); syncChips("fmc", mkSel); syncChips("fexc", excSel); buildBanUI(); render();
    toast("企業・市場・取引所のBANを全部戻しました");
  });
}
/* 初回ガイド: 見たことがなければ一度だけ出す。閉じたら端末に記録する */
function tourBand() {
  const el = $("#tour");
  if (!el) return;
  let seenTour = false;
  try { seenTour = !!localStorage.getItem("trl-tour"); } catch (e) { seenTour = true; }
  el.hidden = seenTour;
  const btn = $("#tourclose");
  if (btn) btn.addEventListener("click", () => {
    el.hidden = true;
    try { localStorage.setItem("trl-tour", "1"); } catch (e) { /* 保存できなくても閉じる */ }
  });
}
function newNote() {
  const el = $("#newnote");
  if (!el) return;
  /* 通知の基準は data.json の生成時刻ではなく、確定評価そのものの評価時刻。
     「前回 (確認した時点) より後に評価された確定」だけを数えるので、
     材料が実際に更新されるまで再表示されない */
  let wm = 0;
  try { wm = Date.parse(localStorage.getItem("trl-note-wm") || "") || 0; } catch (e) { return; }
  const finals = P.filter(p => p.display_status === "final" && p.long && p.long.evaluated_at && Date.parse(p.long.evaluated_at) > wm);
  if (!wm) {
    const maxTs = Math.max(0, ...P.map(p => (p.long && p.long.evaluated_at) ? Date.parse(p.long.evaluated_at) : 0));
    try { localStorage.setItem("trl-note-wm", new Date(maxTs || Date.now()).toISOString()); } catch (e) { /* 保存できなくても害はない */ }
    return;
  }
  if (!finals.length) return;
  el.innerHTML = `<span>前回見た後に、新しい確定評価が ${finals.length}件あります。</span><button class="tz" id="nnok" type="button">確認した</button>`;
  el.hidden = false;
  $("#nnok").addEventListener("click", () => {
    const maxTs = Math.max(wm, ...finals.map(p => Date.parse(p.long.evaluated_at)));
    try { localStorage.setItem("trl-note-wm", new Date(maxTs).toISOString()); } catch (e) { /* 同上 */ }
    el.hidden = true;
  });
}
/* 帯は中身があるときだけ出す (各帯の描画関数が hidden を管理する)。
   帯の表示設定と折りたたみは廃止した (折りたたみは実機で不具合が多く、
   表示設定は消した帯を忘れるため)。旧保存値は掃除だけする */
try { localStorage.removeItem("trl-bands"); localStorage.removeItem("trl-bandfolds"); localStorage.removeItem("trl-last-gen"); } catch (e) { /* 掃除できなくても害はない */ }
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
/* レーダーの幾何 (純粋関数): 6項目を正六角形の頂点に置く。中心 60,60、
   半径 52 が 100 点。値は 0..100 に丸める */
function radarGeom(vals, R = 52) {
  return vals.map((v, i) => {
    const a = -Math.PI / 2 + i * Math.PI / 3, r = Math.max(0, Math.min(100, v)) / 100 * R;
    return [Math.round((60 + r * Math.cos(a)) * 10) / 10, Math.round((60 + r * Math.sin(a)) * 10) / 10];
  });
}
function radarHtml(p) {
  if (!p.long || !p.long.items || !COHORT) return "";
  const vals = ITEM_KEYS.map(k => p.long.items[k] && p.long.items[k].score);
  if (vals.some(v => typeof v !== "number")) return "";  // 未確定の項目がある候補は形が作れない
  const meds = ITEM_KEYS.map(k => COHORT.items[k] ? COHORT.items[k].median : null);
  if (meds.some(v => v == null)) return "";
  const poly = pts => pts.map(pt => pt.join(",")).join(" ");
  const grid = [25, 50, 75, 100].map(g => `<polygon points="${poly(radarGeom(ITEM_KEYS.map(() => g)))}" fill="none" stroke="var(--line)"/>`).join("");
  return `<section class="ds"><h4>項目の形（母集団の中央値との比較）</h4>`
    + `<svg viewBox="0 0 120 120" width="150" height="150" role="img">${grid}`
    + `<polygon points="${poly(radarGeom(meds))}" fill="none" stroke="var(--muted)" stroke-width="1.5" stroke-dasharray="3 2"/>`
    + `<polygon points="${poly(radarGeom(vals))}" fill="var(--accent)" fill-opacity="0.22" stroke="var(--accent)" stroke-width="1.5"/></svg>`
    + `<p class="hint">実線がこの候補、破線が母集団（${COHORT.asOf ? COHORT.asOf + " 時点で選定可能" : "選定可能"} ${COHORT.total ? COHORT.total.n : 0}件）の項目別中央値です。</p>`
    + `<ul class="pts">${ITEM_KEYS.map((k, i) => `<li><b>${esc(label(k))}</b><span>候補 ${fmt1(vals[i])} / 中央値 ${fmt1(meds[i])}</span></li>`).join("")}</ul></section>`;
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
function alertBand() {
  // 実行の失敗と、データの鮮度の警告。通常の発行間隔 (火〜金の実行と
  // 月曜のサイト更新) の最長空白は4日程度で、5日を超えたら異常側に倒す
  const el = $("#alertband");
  if (!el) return;
  const msgs = [];
  if (META && META.run && META.run.status === "error") msgs.push("最新の実行はエラーで終了しました。表示されているのは前回までのデータです。");
  const gen = META && META.generated_at ? Date.parse(META.generated_at) : null;
  if (gen && Date.now() - gen > 5 * 864e5) msgs.push(`このデータは生成から ${Math.floor((Date.now() - gen) / 864e5)} 日が経過しています（通常の発行間隔は最長でも4日程度）。`);
  if (!msgs.length) { el.hidden = true; return; }
  el.innerHTML = msgs.map(m => `<span>${esc(m)}</span>`).join("");
  el.hidden = false;
}
function nqBand() {
  // 次回キュー: export の next_queue (選定順の ID 列) をその順で出す。
  // next_up フラグと同じ集合で、並び順だけが追加の情報
  const el = $("#nqband");
  if (!el) return;
  const q = META && Array.isArray(META.next_queue) ? META.next_queue : [];
  const byId = new Map(P.map(p => [p.id, p]));
  const items = q.filter(id => byId.has(id)).map((id, i) =>
    `<a class="brz lk nq" href="detail.html?id=${encodeURIComponent(id)}">${i + 1}. ${esc(dispT(byId.get(id)))}</a>`);
  if (!items.length) { el.hidden = true; return; }
  el.innerHTML = `<span>次回評価予定（選定順）:</span>` + items.join("");
  el.hidden = false;
}
/* 評価履歴の推移図の幾何 (純粋関数)。x は評価時刻に比例させ (時刻が
   全部同じ場合は等間隔に倒す)、y は総合点 0-100 をそのまま使う。
   総合点の無い評価 (保留・エラー) は線に含めず、下端の点で示す */
function histGeom(hist, W, H) {
  const ts = hist.map(h => Date.parse(h.evaluated_at));
  const t0 = Math.min(...ts), t1 = Math.max(...ts);
  const xs = hist.map((h, i) => {
    const f = t1 > t0 ? (ts[i] - t0) / (t1 - t0) : (hist.length > 1 ? i / (hist.length - 1) : 0.5);
    return 8 + f * (W - 16);
  });
  const y = v => H - 8 - (v / 100) * (H - 16);
  const dots = hist.map((h, i) => ({x: xs[i], y: h.total != null ? y(h.total) : H - 8, total: h.total, trl: h.trl_level, status: h.status}));
  const segs = [];
  let cur = [];
  for (const d of dots) {
    if (d.total != null) cur.push(d);
    else { if (cur.length) segs.push(cur); cur = []; }
  }
  if (cur.length) segs.push(cur);
  return {dots, segs, y};
}
function histHtml(p) {
  const hist = p.history;
  const W = 320, H = 96;
  const g = histGeom(hist, W, H);
  const lines = g.segs.filter(s => s.length > 1).map(s =>
    `<polyline points="${s.map(d => `${d.x.toFixed(1)},${d.y.toFixed(1)}`).join(" ")}" fill="none" stroke="var(--accent)" stroke-width="2"/>`).join("");
  const dots = g.dots.map(d => d.total != null
    ? `<circle cx="${d.x.toFixed(1)}" cy="${d.y.toFixed(1)}" r="3.5" fill="var(--accent)"><title>${esc(d.total)}点</title></circle>`
    : `<circle cx="${d.x.toFixed(1)}" cy="${d.y.toFixed(1)}" r="3" fill="none" stroke="var(--muted)"><title>総合点なし（${esc(SL[d.status] || d.status)}）</title></circle>`).join("");
  const grid = [0, 50, 100].map(v => { const yy = g.y(v); return `<line x1="8" x2="${W - 8}" y1="${yy.toFixed(1)}" y2="${yy.toFixed(1)}" stroke="var(--line)" stroke-width="1"/><text x="10" y="${(yy - 2).toFixed(1)}" font-size="8" fill="var(--muted)">${v}</text>`; }).join("");
  const rows = hist.map(h => `<li><b>${esc(String(h.evaluated_at).slice(0, 10))}</b><span>${esc(SL[h.status] || h.status)} / 総合 ${h.total != null ? h.total + "点" : "—"} / TRL ${h.trl_level != null ? h.trl_level : "未確定"}${h.gen_config ? ` / 世代 ${esc(h.gen_config)}` : ""}</span></li>`).join("");
  return `<svg viewBox="0 0 ${W} ${H}" width="100%" height="${H}" role="img" aria-label="総合点の推移">${grid}${lines}${dots}</svg>`
    + `<ul class="pts">${rows}</ul>`
    + `<p class="hint">総合点の推移です。線は総合点がある評価どうしをつなぎ、総合点の無い評価（保留・エラー）は下端の輪で示します。世代が異なる点どうしの差には、物差し自体の変化が含まれます。</p>`;
}
function auditBadge() {
  // ナビの並びは他ページと同形に保つ (test_web が文字列で固定している) ため、
  // id は足さず href で引く
  const el = document.querySelector('.pagelinks a[href="audit.html"]');
  if (!el) return;
  const n = auditOpenFlags(P);
  el.textContent = n ? `監査（要確認 ${n}件）` : "監査";
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
function barrierBand() {
  // 関門の該当一覧: 候補の long 評価に付いた関門 (barriers) から、関門
  // ごとに該当した企業を集約して企業チップで出す。帰属は導出の置き方に
  // 合わせ、strong リンクが一意な企業だけに付ける (別企業の評価を
  // 貼らない)。同じ企業の複数評価は、最新の評価の候補へリンクする。
  // 表示側で判定・件数の丸めは作らない。該当が無ければ枠ごと隠す
  const el = $("#brband");
  if (!el) return;
  const byGate = {};
  for (const p of P) {
    const b = p.long && p.long.barriers;
    if (!b) continue;
    const strong = (p.entities || []).filter(e => e.strength === "strong");
    if (strong.length !== 1) continue;
    const name = strong[0].name;
    if (!name) continue;
    for (const k of BRO) {
      if (b[k] && b[k].status === "hit") {
        const m = byGate[k] || (byGate[k] = new Map());
        const at = p.long.evaluated_at || "";
        if (!m.has(name) || m.get(name).at < at) m.set(name, {id: p.id, at});
      }
    }
  }
  const parts = BRO.filter(k => byGate[k] && byGate[k].size).map(k => {
    const chips = [...byGate[k].entries()]
      .sort((a, b2) => a[0].localeCompare(b2[0]))
      .map(([name, v]) => `<a class="brz lk" href="detail.html?id=${encodeURIComponent(v.id)}">${esc(name)}</a>`).join("");
    return `<span class="brg"><b>${esc(BRL[k])}</b> ${byGate[k].size}社 ${chips}</span>`;
  });
  if (!parts.length) { el.hidden = true; return; }
  el.innerHTML = `<span>関門の該当:</span>` + parts.join("");
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
    // 判定済みチップと同じ近道 (BAN中のレベルは対象外、未確定チップはそのまま)
    if (trlAllOn()) { for (let i = 1; i <= 9; i++) trlSel.delete(i); }
    else { for (const i of trlAvail()) trlSel.add(i); }
    $("#ftn").value = ""; $("#ftm").value = "";
    syncTrlUI(); render(); return;
  }
  if (v && chipBans.fsc.has(v)) { toast("その状態はBAN中です (チップの長押し/右クリックで解除)"); return; }
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
/* 詳細から戻ったときの位置の印 (詳細ページが sessionStorage に残す)。
   ここで一度だけ消費する。30分より古い印は、戻る操作とは見なさず捨てる */
function takeReturnMark() {
  try {
    const m = JSON.parse(sessionStorage.getItem("trl-return") || "null");
    sessionStorage.removeItem("trl-return");
    if (m && typeof m.id === "string" && m.t && Date.now() - m.t <= 30 * 60 * 1000) return m.id;
  } catch (e) { /* 読めなければ印なし */ }
  return null;
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
  const prm = new URLSearchParams(location.search);  // ピン取り込みが URL を消す前に控える
  importPinsFromUrl();
  importFiltersFromUrl(prm);
  const retId = takeReturnMark();  // 詳細から戻ってきたら、見ていた候補を選択に戻す
  if (retId && P.some(p => p.id === retId)) sel = retId;
  resolveNewBase();
  evalMaxTs = null;
  for (const p of P) { const t = evalTs(p); if (t != null && (evalMaxTs == null || t > evalMaxTs)) evalMaxTs = t; }
  // 生成情報は常設で出す (いつの・どの実行の・どの規程のデータか)
  const cv = d.config && d.config.version;
  $("#gen").textContent = "更新 " + jst(d.generated_at)
    + (d.run ? ` · run ${d.run.id} · ${d.run.status}` : "")
    + (cv ? ` · 規程 ${cv}` : "");
  $("#fl").value = LANG;
  buildCohort();
  facetChips(); warn(d); stats(); tierBand(); barrierBand(); auditBadge(); digestBand(); nqBand(); distBand(); moveBand(); nearBand(); usageBand(); alertBand(); runBand(); statsBand(); pinBand(); cmpBand(); laterBand(); searchHistBand(); recentBand(); tourBand(); buildBanUI(); newNote(); applyZoom(); render();
  // 印の候補が絞り込み後も一覧に残っている (render が選択を保った) ときだけ、
  // そのカードが見える位置まで寄せる。外れていたら先頭のままにしておく
  if (retId && sel === retId) { const el = document.querySelector(`.card[data-id="${(window.CSS && CSS.escape) ? CSS.escape(retId) : retId}"]`); if (el) el.scrollIntoView({block: "center"}); }
}
$("#sf").addEventListener("submit", e => { e.preventDefault(); saveSearch($("#q").value); render(); });
/* 検索履歴 (この端末のみ、直近8件)。チップを押すとその検索を再実行する */
function saveSearch(q) {
  q = (q || "").trim();
  if (!q) return;
  let h = [];
  try { h = JSON.parse(localStorage.getItem("trl-searches") || "[]"); } catch (e) { /* 壊れた保存値は空として扱う */ }
  h = [q, ...h.filter(x => x !== q)].slice(0, 8);
  try { localStorage.setItem("trl-searches", JSON.stringify(h)); } catch (e) { /* 保存不可なら履歴なし */ }
  searchHistBand();
}
function searchHistBand() {
  const el = $("#shist");
  if (!el) return;
  let h = [];
  try { h = JSON.parse(localStorage.getItem("trl-searches") || "[]"); } catch (e) { /* 同上 */ }
  if (!h.length) { el.hidden = true; return; }
  el.innerHTML = `<span>最近の検索</span>` + h.map(x => `<button class="tz" data-q="${esc(x)}">${esc(x)}</button>`).join("");
  el.hidden = false;
  el.querySelectorAll("[data-q]").forEach(b => b.addEventListener("click", () => { $("#q").value = b.dataset.q; render(); }));
}
/* 母集団の基準日カレンダー: 日を選ぶと母集団をその日時点で再構成する
   (統計帯は観測ページへ移設したため、一覧に #statband が無い場合は何もしない) */
const statbandEl = $("#statband");
if (statbandEl) statbandEl.addEventListener("click", e => {
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
    ASOF = null; CALOPEN = false; buildCohort(); statsBand(); detail();
  } else if (mv && CALM) {
    const d = new Date(Date.UTC(CALM.y, CALM.m + Number(mv.dataset.mv), 1));
    CALM = {y: d.getUTCFullYear(), m: d.getUTCMonth()}; statsBand();
  } else if (day && !day.disabled) {
    ASOF = day.dataset.d; CALOPEN = false;
    const b = ASOF.split("-").map(Number); CALM = {y: b[0], m: b[1] - 1};
    buildCohort(); statsBand(); detail();
  }
});
/* 滑らかスクロールは全ページ共通の smooth.js が正本 (氷の摩擦モデル+
   指のノイズ補正)。このページでは Shift+S の窓口だけを残す */
function toggleSmooth() { if (window.trlSmooth) window.trlSmooth.toggle(); }
/* キー操作とコマンドパレット。よく使う操作はホームポジション
   (j/k/f/d/s/l/;) に寄せ、残りはパレットから名前で呼べるようにする。
   ACTIONS が正本で、キー割り当て・パレット・ヘルプはすべてここから出る */
/* 動いた候補だけを巡回する (u/U)。「動いた」= TRL か総合点が変わった・
   帯域を跨いだ・方向性が反転した、のいずれか (histFacts の定義そのまま)。
   n は新着トグルで使用済みのため、update の u に割り当てた */
function jumpMoved(dir) {
  const isMoved = p => { const f = histFacts(p); return f.moved || f.bandCross || f.dirFlip; };
  const idx = [];
  VIEW.forEach((p, i) => { if (isMoved(p)) idx.push(i); });
  if (!idx.length) { toast("前回から動いた候補はありません"); return; }
  const cur = VIEW.findIndex(p => p.id === sel);
  let n = dir > 0 ? idx.find(i => i > cur) : [...idx].reverse().find(i => i < cur);
  if (n === undefined) n = dir > 0 ? idx[0] : idx[idx.length - 1];
  sel = VIEW[n].id;
  document.querySelectorAll("#list .card.sel").forEach(el => el.classList.remove("sel"));
  const el = document.querySelector(`#list .card[data-id="${window.CSS && CSS.escape ? CSS.escape(sel) : sel}"]`);
  if (el) { el.classList.add("sel"); el.scrollIntoView({block: "nearest"}); }
  detail();
  toast(`動いた候補 ${idx.indexOf(n) + 1} / ${idx.length} 件目`);
}
/* 同じ企業の候補だけを巡回する (p / Shift+P)。企業は選択中の候補の
   最初の strong リンク (企業BANの banKeyOf と同じ定義) で判定する */
function peerEntity(p) {
  return p ? (p.entities || []).find(x => x.strength === "strong") : null;
}
function jumpPeer(dir) {
  const cur = P.find(x => x.id === sel);
  const e = peerEntity(cur);
  if (!cur || !e) { toast("企業リンクが無い候補です"); return; }
  const idx = [];
  VIEW.forEach((p, i) => { const pe = peerEntity(p); if (pe && pe.entity_id === e.entity_id) idx.push(i); });
  if (idx.length < 2) { toast(`「${e.name}」の候補はこの絞り込みでは1件だけです`); return; }
  const at = VIEW.findIndex(p => p.id === sel);
  let n = dir > 0 ? idx.find(i => i > at) : [...idx].reverse().find(i => i < at);
  if (n === undefined) n = dir > 0 ? idx[0] : idx[idx.length - 1];
  sel = VIEW[n].id;
  document.querySelectorAll("#list .card.sel").forEach(el => el.classList.remove("sel"));
  const el = document.querySelector(`#list .card[data-id="${window.CSS && CSS.escape ? CSS.escape(sel) : sel}"]`);
  if (el) { el.classList.add("sel"); el.scrollIntoView({block: "nearest"}); }
  detail();
  toast(`同「${e.name}」 ${idx.indexOf(n) + 1} / ${idx.length} 件目`);
}
/* 未読の候補だけを巡回する (Shift+N)。既読は trl-seen (詳細を開くと付く) */
function jumpUnread() {
  const idx = [];
  VIEW.forEach((p, i) => { if (!seen.has(p.id)) idx.push(i); });
  if (!idx.length) { toast("未読の候補はありません"); return; }
  const at = VIEW.findIndex(p => p.id === sel);
  let n = idx.find(i => i > at);
  if (n === undefined) n = idx[0];
  sel = VIEW[n].id;
  document.querySelectorAll("#list .card.sel").forEach(el => el.classList.remove("sel"));
  const el = document.querySelector(`#list .card[data-id="${window.CSS && CSS.escape ? CSS.escape(sel) : sel}"]`);
  if (el) { el.classList.add("sel"); el.scrollIntoView({block: "nearest"}); }
  detail();
  toast(`未読 ${idx.indexOf(n) + 1} / ${idx.length} 件目`);
}
/* 再評価の予約 (r キー): 監査ページと同じ端末内ストア (trl-review) に
   同じ形で書くことで、反映用 JSON の書き出しにそのまま乗る。予約の解除は
   DB へ伝わらない (監査ページと同じ非対称。あちらの mergeReeval の注記参照) */
let reviewCache = null;
function reviewStore() {
  if (reviewCache) return reviewCache;
  let base = {decisions: {}, reeval: {}, adjustments: {}};
  try { const r = JSON.parse(localStorage.getItem("trl-review") || "null");
    if (r && typeof r === "object") base = {decisions: r.decisions || {}, reeval: r.reeval || {}, adjustments: r.adjustments || {}};
  } catch (e) { /* 読めなければ空から */ }
  reviewCache = base;
  return base;
}
function newReviewId() {
  try { if (window.crypto && crypto.randomUUID) return crypto.randomUUID(); } catch (e) { /* 代替生成へ */ }
  return "r-" + Date.now().toString(36) + "-" + Math.random().toString(36).slice(2, 12);
}
function bookedState(p) {
  const l = reviewStore().reeval[p.id];
  if (l && l.origin !== "server") return !!l.on;
  return !!p.reeval_booked;
}
function toggleReevalKey() {
  const p = P.find(x => x.id === sel);
  if (!p) { toast("候補が選択されていません"); return; }
  const store = reviewStore();
  let r = store.reeval[p.id];
  if (!r) { r = {id: newReviewId(), candidate_id: p.id, on: bookedState(p)}; store.reeval[p.id] = r; }
  r.on = !r.on;
  try { localStorage.setItem("trl-review", JSON.stringify(store)); } catch (e) { /* 保存不可でも表示は続ける */ }
  toast(r.on ? "再評価を予約しました" : "再評価の予約を解除しました（この端末だけに効きます）");
  render();
}
function stepSel(dir) {
  if (!VIEW.length) return;
  let i = VIEW.findIndex(p => p.id === sel);
  if (i < 0) i = 0;
  const n = Math.min(VIEW.length - 1, Math.max(0, i + dir));
  if (VIEW[n].id === sel) return;
  sel = VIEW[n].id;
  document.querySelectorAll("#list .card.sel").forEach(el => el.classList.remove("sel"));
  const el = document.querySelector(`#list .card[data-id="${window.CSS && CSS.escape ? CSS.escape(sel) : sel}"]`);
  if (el) { el.classList.add("sel"); el.scrollIntoView({block: "nearest"}); }
  detail();
}
const flipSelect = id => { const s = $(id); s.value = s.value === s.options[0].value ? s.options[1].value : s.options[0].value; s.dispatchEvent(new Event("change")); };
const flipCheck = id => { const c = $(id); c.checked = !c.checked; c.dispatchEvent(new Event("change")); };
const ACTIONS = [
  {label: "次の候補へ移動", keys: "j", run: () => stepSel(1)},
  {label: "前の候補へ移動", keys: "k", run: () => stepSel(-1)},
  {label: "10件次の候補へ移動", keys: "J", run: () => stepSel(10)},
  {label: "10件前の候補へ移動", keys: "K", run: () => stepSel(-10)},
  {label: "次の「動いた」候補へ移動", keys: "u", run: () => jumpMoved(1)},
  {label: "前の「動いた」候補へ移動", keys: "U", run: () => jumpMoved(-1)},
  {label: "同じ企業の次の候補へ移動", keys: "p", run: () => jumpPeer(1)},
  {label: "同じ企業の前の候補へ移動", keys: "P", run: () => jumpPeer(-1)},
  {label: "次の未読候補へ移動", keys: "N", run: () => jumpUnread()},
  {label: "選択中の候補の詳細ページを開く", keys: "Enter", run: () => { if (VMODE) { toggleVSel(sel); return; } if (sel) location.href = "detail.html?id=" + encodeURIComponent(sel); }},
  {label: "選択中の候補をピン留め / 解除", keys: "f", run: () => togglePin(sel)},
  {label: "選択中の候補の再評価を予約 / 解除", keys: "r", run: () => toggleReevalKey()},
  {label: "選択中の候補のリンクをコピー", keys: "y", run: () => { const p = P.find(x => x.id === sel); if (p) { copyText(detailUrl(p)); toast("候補のリンクをコピーしました"); } else toast("候補が選択されていません"); }},
  {label: "滑らかスクロールを切り替え", keys: "S", state: () => { try { return localStorage.getItem("trl-smooth") === "0" ? "現在: 切" : "現在: 入"; } catch (e) { return ""; } }, run: () => toggleSmooth()},
  {label: "選択中の候補の企業をBAN / 解除", keys: "b", run: () => banKeyOf(sel, "company")},
  {label: "選択中の候補の市場をBAN / 解除", keys: "B", run: () => banKeyOf(sel, "market")},
  {label: "最後に光らせた群を全BAN / 全解除", keys: "Cmd+X", run: () => { if (BANMODE.size) allBanToggle(); else toast("先にチップ群のBANチップを押して光らせてください"); }},
  {label: "選択中の候補を比較バスケットに追加 / 解除", keys: "c", run: () => toggleBasket(sel)},
  {label: "選択中の候補の原文を新しいタブで開く", keys: "e", run: () => { const p = P.find(x => x.id === sel); if (p && p.url) window.open(p.url, "_blank", "noopener"); else toast("候補が選ばれていません"); }},
  {label: "選択中の候補の根拠 [関連材料の先頭] を開く", keys: "E", run: () => { const p = P.find(x => x.id === sel); const n = p && (p.related_news || [])[0]; if (n && n.url) window.open(n.url, "_blank", "noopener"); else toast("関連材料がありません"); }},
  {label: "調査メモ欄へ移動", keys: "m", run: () => { const t = $("#note"); if (t) { t.scrollIntoView({block: "center"}); t.focus(); } else toast("候補を選んでください"); }},
  {label: "複数選択モードの入 / 切", keys: "v", run: () => toggleVMode()},
  {label: "選択中の候補をあとで読むに追加 / 解除", keys: "w", run: () => { if (sel) toggleLater(sel); else toast("候補が選ばれていません"); }},
  {label: "あとで読むの帯へ移動", keys: "W", run: () => { const el = $("#laterband"); if (el && !el.hidden) { el.scrollIntoView({block: "start"}); } else toast("あとで読むは空です"); }},
  {label: "並び順を前へ切り替える", keys: ",", run: () => { const s = $("#fo"); s.selectedIndex = (s.selectedIndex + s.options.length - 1) % s.options.length; s.dispatchEvent(new Event("change")); }},
  {label: "並び順を次へ切り替える", keys: ".", run: () => { const s = $("#fo"); s.selectedIndex = (s.selectedIndex + 1) % s.options.length; s.dispatchEvent(new Event("change")); }},
  {label: "検索欄へ移動", keys: "/", run: () => $("#q").focus()},
  {label: "並び順を次へ切り替える", keys: "s", state: () => { const s = $("#fo"); return s && s.selectedOptions[0] ? `現在: ${s.selectedOptions[0].textContent}` : ""; }, run: () => { const s = $("#fo"); s.selectedIndex = (s.selectedIndex + 1) % s.options.length; s.dispatchEvent(new Event("change")); }},
  {label: "新着だけ表示を切り替える", keys: "n", state: () => { const b = document.querySelector('#fown [data-v="new"]'); return b && b.getAttribute("aria-pressed") === "true" ? "現在: 選択中" : ""; }, run: () => { const b = document.querySelector('#fown [data-v="new"]'); if (b) b.click(); }},
  {label: "世代表示を切り替える", keys: "d", state: () => showGen ? "現在: 表示" : "現在: 非表示", run: () => flipCheck("#swgen")},
  {label: "言語を切り替える [日本語訳 / 原文]", keys: "l", state: () => { const s = $("#fl"); return s ? `現在: ${s.value === "orig" ? "原文" : "日本語訳"}` : ""; }, run: () => flipSelect("#fl")},
  {label: "コンパクト表示を切り替える", keys: ";", state: () => compact ? "現在: コンパクト" : "現在: 標準", run: () => flipCheck("#swcompact")},
  {label: "テーマを切り替える [ダーク / ライト / 色覚配慮]", keys: "", state: () => `現在: ${{dark: "ダーク", light: "ライト", cb: "色覚配慮"}[document.documentElement.dataset.theme || "dark"]}`, run: () => flipSelect("#fth")},
  {label: "テーマをダークにする", keys: "D", state: () => document.documentElement.dataset.theme === "dark" ? "現在のテーマ" : "", run: () => setTheme("dark")},
  {label: "テーマをライトにする", keys: "L", state: () => document.documentElement.dataset.theme === "light" ? "現在のテーマ" : "", run: () => setTheme("light")},
  {label: "テーマを色覚配慮にする", keys: "C", state: () => document.documentElement.dataset.theme === "cb" ? "現在のテーマ" : "", run: () => setTheme("cb")},
  {label: "文字サイズを切り替える [標準 / 大 / 特大]", keys: "", state: () => { let z = ""; try { z = localStorage.getItem("trl-zoom") || ""; } catch (e) { /* 読めなければ標準 */ } return `現在: ${["標準", "大", "特大"][ZOOMS.indexOf(z)] || "標準"}`; }, run: () => cycleZoom()},
  {label: "条件をリセットする", keys: "a", run: () => $("#rs").click()},
  {label: "現在の条件に名前を付けて保存", keys: "", run: () => $("#fpsave").click()},
  {label: "現在の絞り込みを CSV で書き出す", keys: "", run: () => exportData("csv")},
  {label: "現在の絞り込みを JSON で書き出す", keys: "", run: () => exportData("json")},
  {label: "BANを書き出す [JSON]", keys: "", run: () => { const b = $("#banexp"); if (b) b.click(); }},
  {label: "注目・新着の共有リンクをコピー", keys: "", run: () => { const b = $("#cpypins"); if (b) b.click(); }},
  {label: "現在の絞り込みのリンクをコピー", keys: "", run: () => { const b = $("#fplink"); if (b) b.click(); }},
  {label: "data.json のスナップショットを保存", keys: "", run: () => saveSnapshot()},
  {label: "週次レポート [Markdown] を保存", keys: "", run: () => saveWeekly()},
  {label: "メインページへ移動", keys: "g i", seq: "i", run: () => { location.href = "index.html"; }},
  {label: "企業ページへ移動", keys: "", run: () => { location.href = "company.html"; }},
  {label: "採点基準ページへ移動", keys: "g c", seq: "c", run: () => { location.href = "criteria.html"; }},
  {label: "監査ページへ移動", keys: "g a", seq: "a", run: () => { location.href = "audit.html"; }},
  {label: "Look・See ページへ移動", keys: "g w", seq: "w", run: () => { location.href = "watch.html"; }},
  {label: "観測ページへ移動", keys: "g o", seq: "o", run: () => { location.href = "observe.html"; }},
  {label: "ブリーフィングページへ移動", keys: "g b", seq: "b", run: () => { location.href = "brief.html"; }},
  {label: "次回評価予定ページへ移動", keys: "g q", seq: "q", run: () => { location.href = "queue.html"; }},
  {label: "実行履歴ページへ移動", keys: "g r", seq: "r", run: () => { location.href = "runs.html"; }},
  {label: "項目別の不足ページへ移動", keys: "g d", seq: "d", run: () => { location.href = "deductions.html"; }},
  {label: "用語集ページへ移動", keys: "g l", seq: "l", run: () => { location.href = "glossary.html"; }},
  {label: "キー操作の説明書ページへ移動", keys: "g k", seq: "k", run: () => { location.href = "keys.html"; }},
  {label: "キー操作の一覧を開く / 閉じる", keys: "?", run: () => toggleHelp()},
];
/* ページ移動のキーシーケンス: g の後にホームポジション周辺のキーを押す
   (割り当ては ACTIONS の seq が正本。入力中・変換中・修飾キー併用時は効かない) */
let gAt = 0;
document.addEventListener("keydown", e => {
  if (e.metaKey || e.ctrlKey || e.altKey || e.isComposing) return;
  const t = e.target;
  if (t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.tagName === "SELECT")) return;
  if (gAt && Date.now() - gAt < 900) {
    const a = ACTIONS.find(x => x.seq === e.key);
    gAt = 0;
    if (a) { e.preventDefault(); a.run(); }
    return;
  }
  gAt = e.key === "g" ? Date.now() : 0;
});
let palIdx = 0, palList = [];
/* パレット専用の動的コマンド (ACTIONS には入れない):
   「候補を開く」= 入力した言葉に合う候補の詳細へ直接飛ぶ。
   「チップ切り替え」= 全チップを [群: チップ名] として検索・実行できる。
   どちらも検索語があるときだけ出す (空のパレットを埋めないため) */
function openCmds(q) {
  if (!q) return [];
  const byId = new Map();
  for (const p of P) {
    if (byId.has(p.id)) continue;
    const nm = (p.entities || []).find(e => e.strength === "strong" && e.name)?.name || p.title_ja || p.title;
    if (nm) byId.set(p.id, {nm, p});
  }
  const counts = {};
  for (const v of byId.values()) counts[v.nm] = (counts[v.nm] || 0) + 1;
  const starts = [], incl = [];
  for (const entry of byId) {
    const lv = entry[1].nm.toLowerCase();
    if (lv.startsWith(q)) starts.push(entry); else if (lv.includes(q)) incl.push(entry);
  }
  return starts.concat(incl).slice(0, 8).map(([id, v]) => {
    let suffix = "";
    if (counts[v.nm] > 1 && v.p.title) { const t = v.p.title; suffix = ` [${t.length > 42 ? t.slice(0, 42) + "…" : t}]`; }
    return {label: `候補を開く: ${v.nm}${suffix}`, run: () => { location.href = `detail.html?id=${encodeURIComponent(id)}`; }};
  });
}
function chipCmds(q) {
  if (!q) return [];
  const out = [];
  document.querySelectorAll(".chips").forEach(g => {
    const gl = BANGLABEL[g.id];
    if (!gl) return;
    g.querySelectorAll("button[data-v]").forEach(b => {
      const label = `${gl}: ${b.textContent.trim()}`;
      if (!label.toLowerCase().includes(q)) return;
      out.push({label,
        state: () => b.classList.contains("ban") ? "BAN中" : (b.getAttribute("aria-pressed") === "true" ? "選択中" : ""),
        run: () => b.click()});
    });
  });
  return out;
}
function palRender() {
  const q = $("#palq").value.trim().toLowerCase();
  palList = ACTIONS.filter(a => !q || a.label.toLowerCase().includes(q)).concat(openCmds(q), chipCmds(q));
  palIdx = Math.min(palIdx, Math.max(0, palList.length - 1));
  $("#pallist").innerHTML = palList.map((a, i) => {
    let st = "";
    try { st = a.state ? a.state() : ""; } catch (e) { st = ""; }
    return `<button type="button" class="pal-i ${i === palIdx ? "on" : ""}" data-i="${i}"><span>${esc(a.label)}${st ? ` [${esc(st)}]` : ""}</span>${a.keys ? `<kbd>${esc(a.keys)}</kbd>` : ""}</button>`;
  }).join("")
    || `<p class="hint" style="padding:10px 14px">一致するコマンドがありません。</p>`;
}
/* パレット/ヘルプ表示中は背面のページをスクロールさせない (スマホで
   キーボードを閉じたときにページが切れて見える問題の対処。枠の高さは
   100dvh 基準に CSS 側で揃えてある) */
function syncScrollLock() {
  document.body.style.overflow = (!$("#pal").hidden || !$("#khelp").hidden) ? "hidden" : "";
}
function openPal() { $("#pal").hidden = false; $("#palq").value = ""; palIdx = 0; palRender(); syncScrollLock(); $("#palq").focus(); }
function closePal() { $("#pal").hidden = true; syncScrollLock(); }
function runPal() { const a = palList[palIdx]; if (!a) return; closePal(); a.run(); }
function toggleHelp() {
  const h = $("#khelp");
  if (h.hidden) {
    // 一覧は全コマンドを出す: キーの付いたものを先に、キーなし
    // (コマンドパレット専用) を後に分けて並べる。「コマンドにはあるのに
    // キー操作には無い」という穴に見える状態をなくすため
    const row = (a, keyless) => `<div class="kh-r${keyless ? " keyless" : ""}" data-t="${esc(((a.keys || "") + " " + a.label).toLowerCase())}"><kbd>${esc(a.keys || "なし")}</kbd><span>${esc(a.label)}</span></div>`;
    $("#khlist").innerHTML = ACTIONS.filter(a => a.keys).map(a => row(a, false)).join("")
      + `<div class="kh-div">キーなし（コマンドパレット専用）</div>`
      + ACTIONS.filter(a => !a.keys).map(a => row(a, true)).join("");
    const hq = $("#helpq"); if (hq) hq.value = "";
    h.hidden = false;
  } else h.hidden = true;
  syncScrollLock();
}
$("#helpq").addEventListener("input", e => {
  const q = e.target.value.trim().toLowerCase();
  document.querySelectorAll("#khlist .kh-r").forEach(r => { r.style.display = !q || r.dataset.t.includes(q) ? "" : "none"; });
});
/* キー操作の練習: 指定のキーを順に押してもらうだけの小さな練習 */
const PRAC = ["j", "k", "f", "/", "n"];
let pracIdx = -1;
function pracMsg() {
  const m = $("#pracmsg");
  if (!m) return;
  if (pracIdx < 0) { m.hidden = true; return; }
  m.hidden = false;
  m.textContent = pracIdx < PRAC.length
    ? `練習中: 「${PRAC[pracIdx]}」キーを押してください (${pracIdx + 1}/${PRAC.length})`
    : "練習完了です。Esc で閉じて、一覧で試してみてください。";
}
document.addEventListener("click", e => {
  if (e.target.closest("#pracbtn")) { pracIdx = 0; pracMsg(); }
});
$("#palq").addEventListener("input", () => { palIdx = 0; palRender(); });
$("#palq").addEventListener("keydown", e => {
  if (e.isComposing) return;  // IME 変換中の Enter は確定に使う
  if (e.key === "ArrowDown") { e.preventDefault(); palIdx = Math.min(palList.length - 1, palIdx + 1); palRender(); }
  else if (e.key === "ArrowUp") { e.preventDefault(); palIdx = Math.max(0, palIdx - 1); palRender(); }
  else if (e.key === "Enter") { e.preventDefault(); runPal(); }
  else if (e.key === "Escape") closePal();
});
$("#pallist").addEventListener("pointerdown", e => { const b = e.target.closest("[data-i]"); if (b) { e.preventDefault(); palIdx = +b.dataset.i; runPal(); } });
$("#pal").addEventListener("pointerdown", e => { if (e.target === $("#pal")) closePal(); });
$("#khelp").addEventListener("pointerdown", e => { if (e.target === $("#khelp")) { $("#khelp").hidden = true; syncScrollLock(); } });
$("#palbtn").addEventListener("click", openPal);
$("#khbtn").addEventListener("click", toggleHelp);
document.addEventListener("keydown", e => {
  // コマンドパレット (Ctrl+K / Cmd+K) は入力中でも開閉できる
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") { e.preventDefault(); $("#pal").hidden ? openPal() : closePal(); return; }
  // 全BAN (Cmd+X / Ctrl+X): BANチップが光っている群だけを対象にし、入力中や画面が開いている間は本来の切り取りを邪魔しない
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "x" && BANMODE.size) {
    const tagX = (document.activeElement || {}).tagName || "";
    if (/INPUT|TEXTAREA|SELECT/.test(tagX) || e.isComposing || composing || !$("#pal").hidden || !$("#khelp").hidden) return;
    e.preventDefault(); allBanToggle(); return;
  }
  if (e.key === "Escape") {
    const hadOverlay = !$("#pal").hidden || !$("#khelp").hidden;
    closePal(); $("#khelp").hidden = true; syncScrollLock(); pracIdx = -1; pracMsg();
    // 画面が開いていなければ、Esc で BANモードを全部抜ける
    if (!hadOverlay && BANMODE.size) { for (const id of [...BANMODE]) { BANMODE.delete(id); syncBanBtn(id); } toast("BANモードを抜けました"); }
    return;
  }
  if (pracIdx >= 0 && pracIdx < PRAC.length && !$("#khelp").hidden) {
    if (e.key === PRAC[pracIdx]) { e.preventDefault(); pracIdx++; pracMsg(); }
    return;
  }
  if (!$("#pal").hidden || !$("#khelp").hidden) return;  // 画面が開いている間はそちらの操作を優先
  const tag = (document.activeElement || {}).tagName || "";
  if (/INPUT|TEXTAREA|SELECT/.test(tag) || e.isComposing || composing) return;  // 入力中と IME 変換中は邪魔しない
  if (e.ctrlKey || e.metaKey || e.altKey) return;
  if (e.key === "Enter" && /BUTTON|A/.test(tag)) return;  // ボタンやリンクにフォーカスがあるときは本来の動作を優先
  // 数字キーで TRL チップを直接切り替える (1〜9 は TRL、0 は未確定)
  if (/^[0-9]$/.test(e.key)) {
    e.preventDefault();
    const n = e.key === "0" ? "u" : Number(e.key);
    if (trlBan.has(n)) { toast(`TRL ${n === "u" ? "未確定" : n} はBAN中です (チップの長押し/右クリックで解除)`); return; }
    if (trlSel.has(n)) trlSel.delete(n); else trlSel.add(n);
    syncTrlUI(); render();
    return;
  }
  const a = ACTIONS.find(x => x.keys && x.keys === e.key);
  if (a) { e.preventDefault(); a.run(); }
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
/* 検索記法 (src: など) の補完: 最後の語が演算子名や演算子:値の形なら、
   その演算子で使える値を候補に出す */
function opCompletions() {
  const tok = ($("#q").value.split(/\s+/).pop() || "");
  const ops = {
    src: () => [...new Set(P.map(p => p.source).filter(Boolean))],
    status: () => ["final", "hold", "review", "pending", "rejected"],
    dir: () => ["positive", "negative", "mixed"],
    trl: () => ["1", "2", "3", "4", "5", "6", "7", "8", "9", "1-9"],
    tier: () => ["core", "conditional", "look", "see"],
    item: () => Object.keys(IL), missing: () => Object.keys(IL),
    model: () => [...new Set(P.map(p => p.long && p.long.llm_model).filter(Boolean))],
    tag: () => [...new Set(P.flatMap(p => p.fields || []))]};
  const m = tok.match(/^([a-z]+):(.*)$/);
  if (m && ops[m[1]]) {
    const part = m[2].toLowerCase();
    return ops[m[1]]().filter(v => v.toLowerCase().includes(part)).slice(0, 8).map(v => ({t: "記法", v: m[1] + ":" + v, tok: true}));
  }
  if (tok.length >= 2 && !tok.includes(":")) {
    const hit = Object.keys(ops).filter(n => n.startsWith(tok.toLowerCase()));
    if (hit.length) return hit.map(n => ({t: "記法", v: n + ":", tok: true}));
  }
  return null;
}
function showSug() {
  clearTimeout(sugHideTimer);
  if (!P.length) { hideSug(); return; }
  const opc = opCompletions();
  if (opc && opc.length) {
    sugItems = opc; sugIdx = -1;
    sugBox.innerHTML = sugItems.map((it, i) => `<button type="button" class="sug-i" data-i="${i}"><span class="sug-t">${esc(it.t)}</span><span class="sug-x">${esc(it.v)}</span></button>`).join("");
    sugBox.hidden = false;
    return;
  }
  const q = $("#q").value.trim().toLowerCase();
  if (q.length < 2) { hideSug(); return; }
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
  if (it.tok) {
    // 記法の補完は最後の語だけを置き換える (前にある検索語は残す)
    const parts = $("#q").value.split(/\s+/);
    parts[parts.length - 1] = it.v;
    $("#q").value = parts.join(" ") + (it.v.endsWith(":") ? "" : " ");
    if (it.v.endsWith(":")) { showSug(); } else { hideSug(); render(); }
    $("#q").focus();
    return;
  }
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
/* チップのBAN: 絞り込みチップを長押しすると、その値を除外 (BAN) する。
   選択 (含む) とは別に持ち、BAN中のチップには赤い斜線が入る。
   例: 保留をBANすると、保留以外 (と、もともと隠れている除外以外) が全部出る */
const BANKEYS = ["fsc", "fdc", "fmc", "fexc", "fcc", "fgc", "fstc", "ftierc", "fgenc", "fown", "fhcc", "fmvc", "fhistc", "fevc"];
const chipBans = {};
for (const k of BANKEYS) chipBans[k] = new Set();
const trlBan = new Set();
function saveChipBans() {
  try {
    const o = {};
    for (const k of BANKEYS) o[k] = [...chipBans[k]];
    o.ftc = [...trlBan];
    localStorage.setItem("trl-chipbans", JSON.stringify(o));
  } catch (e) { /* 保存できなくても動く */ }
}
function loadChipBans() {
  try {
    const o = JSON.parse(localStorage.getItem("trl-chipbans") || "{}");
    for (const k of BANKEYS) for (const v of o[k] || []) chipBans[k].add(v);
    for (const v of o.ftc || []) trlBan.add(v === "u" ? "u" : Number(v));
  } catch (e) { /* 壊れた保存は無視 */ }
}
loadChipBans();
/* BAN一式 (チップBAN・TRL BAN・指名BAN) の取り込み/戻し。プロファイルと
   BANの書き出し/読み込みで共用する */
function captureBanState() {
  const o = {};
  for (const k of BANKEYS) o[k] = [...chipBans[k]];
  o.ftc = [...trlBan];
  o.named = BANS.map(b => ({t: b.t, v: b.v, on: !!b.on}));
  return o;
}
/* BANと選択の矛盾 (同じ値が両方に入った状態) を解消する。保存した条件・
   プロファイル・BANファイルの取り込みで起こりうるので、取り込みの出口で通す */
function sanitizeSel() {
  for (const [id, set] of [...CHIPG, ["fgc", tagSel]]) { const bn = chipBans[id]; if (bn) for (const v of bn) set.delete(v); }
  for (const v of trlBan) trlSel.delete(v);
  if (evWin != null && chipBans.fevc.has(String(evWin))) evWin = null;
}
function restoreBanState(o) {
  if (!o || typeof o !== "object") return;
  for (const k of BANKEYS) { chipBans[k].clear(); if (Array.isArray(o[k])) for (const v of o[k]) chipBans[k].add(v); }
  trlBan.clear(); if (Array.isArray(o.ftc)) for (const v of o.ftc) trlBan.add(v === "u" ? "u" : Number(v));
  if (Array.isArray(o.named)) BANS = o.named.filter(b => b && typeof b.v === "string" && typeof b.t === "string").map(b => ({t: b.t, v: b.v, on: !!b.on}));
  sanitizeSel();
  saveChipBans(); saveBans();
  [...CHIPG, ["fgc", tagSel]].forEach(([id, set]) => syncChips(id, set));
  syncTrlUI(); syncEvUI(); buildBanUI();
}
function syncChips(id, set) {
  document.querySelectorAll("#" + id + " button:not([data-banmode])").forEach(b => {
    const on = set.has(b.dataset.v);
    b.classList.toggle("on", on); b.setAttribute("aria-pressed", on ? "true" : "false");
    const bn = chipBans[id];
    b.classList.toggle("ban", !!(bn && bn.has(b.dataset.v)));
  });
}
/* BANモード: 各チップ群の末尾にある BAN チップを押して赤く光っている間は、
   その群のチップはタップ/クリックが選択でなく BAN の切り替えになる。
   スマホで長押しを繰り返さずに済ませるためのモードで、端末には保存しない
   (開き直したら通常モードに戻る)。長押しと右クリックの BAN は従来どおり */
const BANMODE = new Set();
function banModeBtn(id) {
  const box = $("#" + id);
  if (!box || box.querySelector("[data-banmode]")) return;
  const b = document.createElement("button");
  b.type = "button"; b.className = "banmode"; b.dataset.banmode = id;
  b.textContent = "BAN"; b.setAttribute("aria-pressed", "false");
  b.title = "BANモード: 光っている間は、この群のチップのタップが選択でなくBANの切り替えになります";
  box.appendChild(b);
  syncBanBtn(id);
}
function syncBanBtn(id) {
  const b = document.querySelector("#" + id + " [data-banmode]");
  if (!b) return;
  const on = BANMODE.has(id);
  b.classList.toggle("on", on); b.setAttribute("aria-pressed", on ? "true" : "false");
}
function toggleBanMode(id) {
  if (BANMODE.has(id)) BANMODE.delete(id); else { BANMODE.add(id); buzz(); }
  syncBanBtn(id);
}
/* 全BAN (Cmd+X / Ctrl+X): 対象は最後に光らせた群だけ (LIFO)。複数の群が
   光っているときに全部へ同時に効くと事故になるため、1回で1群だけに効かせ、
   効いた群はモードを抜ける (スタックを pop する形)。その群が既に全部BANなら
   全解除になる (同じキーで行き来できる) */
const BANGLABEL = {fsc: "状態", fdc: "方向性", ftc: "TRL", fmc: "市場", fexc: "取引所", fcc: "分野", fgc: "タグ", fstc: "段階", ftierc: "層", fgenc: "世代", fown: "自分の表示", fhcc: "保留理由", fmvc: "動き", fhistc: "履歴", fevc: "評価日"};
function allBanToggle() {
  const id = [...BANMODE].pop();  // 最後に光らせた群 (Set は挿入順を保つ)
  if (!id) return;
  let unbanned = false;
  if (id === "ftc") {
    const targets = [1, 2, 3, 4, 5, 6, 7, 8, 9, "u"];
    unbanned = targets.every(v => trlBan.has(v));
    for (const v of targets) { if (unbanned) trlBan.delete(v); else { trlBan.add(v); trlSel.delete(v); } }
    if (!unbanned) buzz();
    syncTrlUI();
  } else if (id === "fevc") {
    const vals = [...document.querySelectorAll("#fevc button[data-v]")].map(b => b.dataset.v);
    unbanned = vals.length > 0 && vals.every(v => chipBans.fevc.has(v));
    for (const v of vals) { if (unbanned) chipBans.fevc.delete(v); else chipBans.fevc.add(v); }
    if (evWin != null && chipBans.fevc.has(String(evWin))) evWin = null;
    if (!unbanned) buzz();
    syncEvUI();
  } else {
    const set = (CHIPG.find(([g]) => g === id) || [])[1] || (id === "fgc" ? tagSel : null);
    if (!set) return;
    const vals = [...document.querySelectorAll(`#${id} button[data-v]`)].map(b => b.dataset.v);
    if (!vals.length) return;
    unbanned = vals.every(v => chipBans[id].has(v));
    for (const v of vals) { if (unbanned) chipBans[id].delete(v); else { chipBans[id].add(v); set.delete(v); } }
    if (!unbanned) buzz();
    syncChips(id, set); if (id === "fgc") syncTagN();
  }
  BANMODE.delete(id); syncBanBtn(id);  // 効いた群はモードを抜ける
  saveChipBans(); render();
  const rest = BANMODE.size ? ` (光っている群があと${BANMODE.size}つ)` : "";
  toast(unbanned ? `全BANを解除しました: ${BANGLABEL[id]}${rest}` : `全BANしました: ${BANGLABEL[id]}${rest}`);
}
function bindChips(id, set, after) {
  const box = $("#" + id);
  banModeBtn(id);
  const toggleBan = b => {
    const bs = chipBans[id];
    if (bs) { if (bs.has(b.dataset.v)) bs.delete(b.dataset.v); else { bs.add(b.dataset.v); set.delete(b.dataset.v); buzz(); } saveChipBans(); }
    syncChips(id, set); if (after) after(); render();
  };
  box.addEventListener("click", e => {
    const b = e.target.closest("button"); if (!b) return;
    if (b.dataset.banmode) { toggleBanMode(id); return; }
    if (box.dataset.lp === "1") { box.dataset.lp = ""; return; }  // 長押し直後のクリックは飲む
    if (BANMODE.has(id)) { toggleBan(b); return; }  // BANモード中はタップがBAN切り替え (BAN中のチップも解除できる)
    if (b.classList.contains("ban")) return;  // BAN中のチップは通常クリック/タップを通さない (解除は長押し/右クリック/BANモードだけ)
    if (set.has(b.dataset.v)) set.delete(b.dataset.v); else set.add(b.dataset.v);
    syncChips(id, set); if (after) after(); render();
  });
  let timer = null;
  box.addEventListener("pointerdown", e => {
    const b = e.target.closest("button"); if (!b || b.dataset.banmode) return;
    timer = setTimeout(() => { box.dataset.lp = "1"; toggleBan(b); }, 450);
  });
  const cancel = () => { if (timer) { clearTimeout(timer); timer = null; } };
  box.addEventListener("pointerup", cancel);
  box.addEventListener("pointerleave", cancel);
  box.addEventListener("pointercancel", cancel);
  // PC では右クリックでも BAN を切り替える (長押しと同じ操作)
  box.addEventListener("contextmenu", e => {
    const b = e.target.closest("button"); if (!b) return;
    e.preventDefault();
    if (b.dataset.banmode) return;
    toggleBan(b);
  });
}
const syncTagN = () => { $("#fgn").textContent = tagSel.size ? `（${tagSel.size}件選択中）` : ""; };
const CHIPG = [["fsc", stSel], ["fdc", dirSel], ["fmc", mkSel], ["fexc", excSel], ["fcc", scSel], ["fstc", stgSel], ["ftierc", tierSel], ["fgenc", genSel], ["fown", ownSel], ["fhcc", holdSel], ["fmvc", moveSel], ["fhistc", histSel]];
CHIPG.forEach(([id, set]) => bindChips(id, set));
bindChips("fgc", tagSel, syncTagN);
/* 「判定済み」の近道が相手にするのは BANされていない 1〜9 だけ (BAN中のレベルは
   どの経路からも選択できない、という不変条件をここで一元化する) */
const trlAvail = () => [1, 2, 3, 4, 5, 6, 7, 8, 9].filter(i => !trlBan.has(i));
const trlAllOn = () => { const a = trlAvail(); return a.length > 0 && a.every(i => trlSel.has(i)); };
const syncTrlUI = () => document.querySelectorAll("#ftc button:not([data-banmode])").forEach(b => {
  // 「判定済み」チップの選択表示は、BANされていない 1〜9 が全部そろっているかどうかで決まる (近道なので専用の状態は持たない)
  const on = b.dataset.l === "all" ? trlAllOn()
    : trlSel.has(b.dataset.l === "u" ? "u" : Number(b.dataset.l));
  b.classList.toggle("on", on); b.setAttribute("aria-pressed", on ? "true" : "false");
  const bn = b.dataset.l === "all" ? [1, 2, 3, 4, 5, 6, 7, 8, 9].every(i => trlBan.has(i))
    : trlBan.has(b.dataset.l === "u" ? "u" : Number(b.dataset.l));
  b.classList.toggle("ban", bn);
});
banModeBtn("ftc");
const trlToggleBan = b => {
  if (b.dataset.l === "all") {
    const all = [1, 2, 3, 4, 5, 6, 7, 8, 9].every(i => trlBan.has(i));
    for (let i = 1; i <= 9; i++) { if (all) trlBan.delete(i); else { trlBan.add(i); trlSel.delete(i); } }
  } else {
    const k = b.dataset.l === "u" ? "u" : Number(b.dataset.l);
    if (trlBan.has(k)) trlBan.delete(k); else { trlBan.add(k); trlSel.delete(k); buzz(); }
  }
  saveChipBans(); syncTrlUI(); render();
};
$("#ftc").addEventListener("click", e => {
  const b = e.target.closest("button"); if (!b) return;
  if (b.dataset.banmode) { toggleBanMode("ftc"); return; }
  if ($("#ftc").dataset.lp === "1") { $("#ftc").dataset.lp = ""; return; }  // 長押し直後のクリックは飲む
  if (BANMODE.has("ftc")) { trlToggleBan(b); return; }  // BANモード中はタップがBAN切り替え
  if (b.classList.contains("ban")) return;  // BAN中のチップは通常クリックを通さない
  if (b.dataset.l === "all") {
    // 判定済み = レベルが確定したもの全部への近道。BAN中のレベルは選択できないので
    // 対象から外す (以前は BAN中のレベルまで選択状態に入り、チップが選択とBANの
    // 両方を表示する矛盾が起きていた)。未確定チップはそのまま
    if (trlAllOn()) { for (let i = 1; i <= 9; i++) trlSel.delete(i); }
    else { for (const i of trlAvail()) trlSel.add(i); }
  } else {
    const k = b.dataset.l === "u" ? "u" : Number(b.dataset.l);
    if (trlSel.has(k)) trlSel.delete(k); else trlSel.add(k);
  }
  $("#ftn").value = ""; $("#ftm").value = "";  // 個別に触ったら範囲入力は空に戻す (表示と選択状態を一致させる)
  syncTrlUI(); render();
});
{  // TRLチップの長押し = BAN (判定済みチップは 1〜9 をまとめてBAN)
  const box = $("#ftc");
  let timer = null;
  box.addEventListener("pointerdown", e => {
    const b = e.target.closest("button"); if (!b || b.dataset.banmode) return;
    timer = setTimeout(() => { box.dataset.lp = "1"; trlToggleBan(b); }, 450);
  });
  const cancel = () => { if (timer) { clearTimeout(timer); timer = null; } };
  box.addEventListener("pointerup", cancel);
  box.addEventListener("pointerleave", cancel);
  box.addEventListener("pointercancel", cancel);
  box.addEventListener("contextmenu", e => {
    const b = e.target.closest("button"); if (!b) return;
    e.preventDefault();
    if (b.dataset.banmode) return;
    trlToggleBan(b);
  });
}
const applyRange = () => {  // n〜m を入れたら、その範囲のレベルだけを選択状態にする (未確定チップはそのまま)
  if ($("#ftn").value === "" || $("#ftm").value === "") return;
  const n = Number($("#ftn").value), m = Number($("#ftm").value);
  if (!Number.isInteger(n) || !Number.isInteger(m)) return;
  const lo = Math.min(9, Math.max(1, Math.min(n, m))), hi = Math.min(9, Math.max(1, Math.max(n, m)));
  for (let i = 1; i <= 9; i++) { if (i >= lo && i <= hi && !trlBan.has(i)) trlSel.add(i); else trlSel.delete(i); }
  syncTrlUI(); render();
};
["#ftn", "#ftm"].forEach(s => $(s).addEventListener("change", applyRange));
["#fsn", "#fsm"].forEach(s => $(s).addEventListener("change", render));
// 評価日の窓は単一選択 (押したものだけが残り、同じものをもう一度押すと解除)。
// 他のチップと同じく長押し/右クリックでBANでき、BANした窓に入る候補は除外する
const syncEvUI = () => document.querySelectorAll("#fevc button:not([data-banmode])").forEach(b => {
  const on = evWin != null && String(evWin) === b.dataset.v;
  b.classList.toggle("on", on); b.setAttribute("aria-pressed", on ? "true" : "false");
  b.classList.toggle("ban", chipBans.fevc.has(b.dataset.v));
});
const evBanToggle = b => {
  if (chipBans.fevc.has(b.dataset.v)) chipBans.fevc.delete(b.dataset.v);
  else {
    chipBans.fevc.add(b.dataset.v); buzz();
    const v = b.dataset.v === "latest" ? "latest" : Number(b.dataset.v);
    if (evWin === v) evWin = null;  // 選択中の窓をBANしたら選択も外す
  }
  saveChipBans(); syncEvUI(); render();
};
banModeBtn("fevc");
$("#fevc").addEventListener("click", e => {
  const b = e.target.closest("button"); if (!b) return;
  if (b.dataset.banmode) { toggleBanMode("fevc"); return; }
  if ($("#fevc").dataset.lp === "1") { $("#fevc").dataset.lp = ""; return; }  // 長押し直後のクリックは飲む
  if (BANMODE.has("fevc")) { evBanToggle(b); return; }  // BANモード中はタップがBAN切り替え
  if (b.classList.contains("ban")) return;  // BAN中のチップは通常クリックを通さない
  const v = b.dataset.v === "latest" ? "latest" : Number(b.dataset.v);
  evWin = evWin === v ? null : v;
  syncEvUI(); render();
});
{  // 評価日チップの長押し/右クリック = BAN
  const box = $("#fevc");
  let timer = null;
  box.addEventListener("pointerdown", e => {
    const b = e.target.closest("button"); if (!b || b.dataset.banmode) return;
    timer = setTimeout(() => { box.dataset.lp = "1"; evBanToggle(b); }, 450);
  });
  const cancel = () => { if (timer) { clearTimeout(timer); timer = null; } };
  box.addEventListener("pointerup", cancel);
  box.addEventListener("pointerleave", cancel);
  box.addEventListener("pointercancel", cancel);
  box.addEventListener("contextmenu", e => {
    const b = e.target.closest("button"); if (!b) return;
    e.preventDefault();
    if (b.dataset.banmode) return;
    evBanToggle(b);
  });
}
$("#fl").addEventListener("change", () => { LANG = $("#fl").value; try { localStorage.setItem("trl-lang", LANG); } catch (e) { /* 保存できなくても切替は効く */ } render(); });
const syncTh = () => { $("#fth").value = document.documentElement.dataset.theme || "dark"; };
/* テーマの直接指定 (ボタン・キー・パレットの共通処理)。保存と select の同期まで面倒を見る */
function setTheme(mode) {
  document.documentElement.dataset.theme = mode;
  try { localStorage.setItem("trl-theme", mode); } catch (e) { /* 保存できなくても切替は効く */ }
  const sel = $("#fth"); if (sel) sel.value = mode;
  toast(`テーマ: ${{dark: "ダーク", light: "ライト", cb: "色覚配慮"}[mode] || mode}`);
}
const themeBtn = $("#themebtn");
if (themeBtn) themeBtn.addEventListener("click", () => {
  const order = ["dark", "light", "cb"];
  const cur = document.documentElement.dataset.theme || "dark";
  setTheme(order[(order.indexOf(cur) + 1) % order.length]);
});
$("#fth").addEventListener("change", () => { document.documentElement.dataset.theme = $("#fth").value; try { localStorage.setItem("trl-theme", $("#fth").value); } catch (e) { /* 保存できなくても切替は効く */ } });
syncTh();
// 世代表示のスイッチ。状態は localStorage (trl-showgen) で詳細ページと共有する
function applyViewSwitches() {
  const sg = $("#swgen");
  if (sg) sg.checked = showGen;
}
$("#swgen").addEventListener("change", () => { showGen = $("#swgen").checked; try { localStorage.setItem("trl-showgen", showGen ? "1" : "0"); } catch (e) { /* 保存できなくても切替は効く */ } render(); });
applyViewSwitches();
// 表示密度 (コンパクト表示) のスイッチ。状態は localStorage (trl-density)。
// CSS だけで効くため再描画はしない
let compact = false;
try { compact = localStorage.getItem("trl-density") === "compact"; } catch (e) { /* 保存不可なら既定のまま */ }
function applyDensity() {
  document.body.classList.toggle("compact", compact);
  const sw = $("#swcompact"); if (sw) sw.checked = compact;
}
$("#swcompact").addEventListener("change", () => { compact = $("#swcompact").checked; try { localStorage.setItem("trl-density", compact ? "compact" : "normal"); } catch (e) { /* 保存できなくても切替は効く */ } applyDensity(); });
applyDensity();
$("#rs").addEventListener("click", () => { const snapF = captureFilterState(); const snapB = {}; for (const k of BANKEYS) snapB[k] = [...chipBans[k]]; const snapT = [...trlBan]; $("#q").value = ""; for (const k of BANKEYS) chipBans[k].clear(); trlBan.clear(); saveChipBans(); BANMODE.clear(); [...BANKEYS, "ftc"].forEach(k => syncBanBtn(k)); [...CHIPG, ["fgc", tagSel]].forEach(([id, set]) => { set.clear(); syncChips(id, set); }); syncTagN(); trlSel.clear(); syncTrlUI(); $("#ftn").value = ""; $("#ftm").value = ""; syncTrlUI(); $("#fsn").value = ""; $("#fsm").value = ""; evWin = null; syncEvUI(); $("#fo").value = "status"; sel = P[0] ? P[0].id : null; render(); toast("条件をリセットしました", () => { restoreFilterState(snapF); for (const k of BANKEYS) { chipBans[k].clear(); for (const v of snapB[k]) chipBans[k].add(v); } trlBan.clear(); for (const v of snapT) trlBan.add(v); saveChipBans(); [...BANKEYS, "ftc"].forEach(k => syncBanBtn(k)); syncTrlUI(); render(); }); });

/* 条件 (絞り込み一式) の保存と共有。端末には名前付きで保存し
   (localStorage trl-presets)、リンクでは URL パラメータに載せて持ち運べる。
   対象は絞り込みと並び順だけで、表示の好み (言語・テーマ・世代表示・
   密度) は端末ごとの設定として含めない */
function captureFilterState() {
  return {q: $("#q").value, sort: $("#fo").value,
    st: [...stSel], trl: [...trlSel], dir: [...dirSel], mk: [...mkSel], exc: [...excSel], sc: [...scSel],
    tag: [...tagSel], stg: [...stgSel], tier: [...tierSel], gen: [...genSel], own: [...ownSel],
    hold: [...holdSel], move: [...moveSel], hist: [...histSel], slo: $("#fsn").value, shi: $("#fsm").value,
    ev: evWin == null ? "" : String(evWin)};
}
function restoreFilterState(s) {
  if (!s || typeof s !== "object") return;
  const put = (set, arr) => { set.clear(); if (Array.isArray(arr)) for (const v of arr) set.add(v); };
  put(stSel, s.st); put(dirSel, s.dir); put(mkSel, s.mk); put(excSel, s.exc); put(scSel, s.sc);
  put(tagSel, s.tag); put(stgSel, s.stg); put(tierSel, s.tier); put(genSel, s.gen);
  put(ownSel, s.own); put(holdSel, s.hold); put(moveSel, s.move); put(histSel, s.hist);
  trlSel.clear(); if (Array.isArray(s.trl)) for (const v of s.trl) trlSel.add(v === "u" ? "u" : Number(v));
  $("#q").value = typeof s.q === "string" ? s.q : "";
  if (typeof s.sort === "string" && [...$("#fo").options].some(o => o.value === s.sort)) $("#fo").value = s.sort;
  $("#fsn").value = typeof s.slo === "string" ? s.slo : "";
  $("#fsm").value = typeof s.shi === "string" ? s.shi : "";
  evWin = s.ev === "latest" ? "latest" : (s.ev === "7" ? 7 : (s.ev === "30" ? 30 : null));
  sanitizeSel();
  [...CHIPG, ["fgc", tagSel]].forEach(([id, set]) => syncChips(id, set));
  syncTagN(); syncTrlUI(); syncEvUI();
}
const PRESET_KEY = "trl-presets";
function loadPresets() { try { const o = JSON.parse(localStorage.getItem(PRESET_KEY) || "{}"); return o && typeof o === "object" && !Array.isArray(o) ? o : {}; } catch (e) { return {}; } }
function savePresets(o) { try { localStorage.setItem(PRESET_KEY, JSON.stringify(o)); } catch (e) { /* 保存不可でも操作は続ける */ } }
function syncPresetSel() {
  const names = Object.keys(loadPresets()).sort((a, b) => a.localeCompare(b, "ja"));
  $("#fpsel").innerHTML = names.length ? names.map(n => `<option value="${esc(n)}">${esc(n)}</option>`).join("") : `<option value="">（保存した条件なし）</option>`;
}
function flashBtn(btn, msg) { const o = btn.textContent; btn.textContent = msg; setTimeout(() => { btn.textContent = o; }, 1600); }
$("#fpsave").addEventListener("click", e => {
  const name = $("#fpname").value.trim();
  if (!name) { flashBtn(e.currentTarget, "条件名を入れてください"); return; }
  const all = loadPresets(); all[name] = captureFilterState(); savePresets(all);
  syncPresetSel(); $("#fpsel").value = name; $("#fpname").value = "";
  flashBtn(e.currentTarget, "保存しました");
});
$("#fpapply").addEventListener("click", () => { const name = $("#fpsel").value; const all = loadPresets(); if (name && all[name]) { restoreFilterState(all[name]); render(); } });
$("#fpdel").addEventListener("click", () => { const name = $("#fpsel").value; if (!name) return; const all = loadPresets(); delete all[name]; savePresets(all); syncPresetSel(); });
syncPresetSel();
function filterLinkUrl() {
  const s = captureFilterState();
  const u = new URL(location.origin + location.pathname);
  const setP = (k, v) => { if (v) u.searchParams.set(k, v); };
  setP("q", s.q); if (s.sort !== "status") setP("sort", s.sort);
  for (const k of ["st", "trl", "dir", "mk", "sc", "tag", "stg", "tier", "gen", "own", "hold", "move"]) setP("f_" + k, s[k].join(","));
  setP("f_slo", s.slo); setP("f_shi", s.shi); setP("f_ev", s.ev);
  return u.toString();
}
$("#fplink").addEventListener("click", e => copyText(filterLinkUrl(), e.currentTarget));
/* プロファイル: 条件 (絞り込み一式) + BAN一式を「視点」として名前付き保存する。
   条件だけの保存 (trl-presets) とは別に、BANまで含めた束を扱う */
const PROF_KEY = "trl-profiles";
function loadProfiles() { try { const o = JSON.parse(localStorage.getItem(PROF_KEY) || "{}"); return o && typeof o === "object" && !Array.isArray(o) ? o : {}; } catch (e) { return {}; } }
function saveProfiles(o) { try { localStorage.setItem(PROF_KEY, JSON.stringify(o)); } catch (e) { /* 保存不可でも操作は続ける */ } }
function syncProfileSel() {
  const names = Object.keys(loadProfiles()).sort((a, b) => a.localeCompare(b, "ja"));
  $("#pfsel").innerHTML = names.length ? names.map(n => `<option value="${esc(n)}">${esc(n)}</option>`).join("") : `<option value="">（プロファイルなし）</option>`;
}
$("#pfsave").addEventListener("click", e => {
  const name = $("#pfname").value.trim();
  if (!name) { flashBtn(e.currentTarget, "名前を入れてください"); return; }
  const all = loadProfiles();
  all[name] = {filter: captureFilterState(), bans: captureBanState()};
  saveProfiles(all); syncProfileSel(); $("#pfsel").value = name; $("#pfname").value = "";
  flashBtn(e.currentTarget, "保存しました");
});
$("#pfapply").addEventListener("click", () => {
  const name = $("#pfsel").value; const all = loadProfiles();
  if (name && all[name]) { restoreFilterState(all[name].filter); restoreBanState(all[name].bans); render(); toast(`プロファイルを適用: ${name}`); }
});
$("#pfdel").addEventListener("click", () => {
  const name = $("#pfsel").value; if (!name) return;
  const all = loadProfiles(); delete all[name]; saveProfiles(all); syncProfileSel();
});
syncProfileSel();
/* 保存した正規表現: /パターン/ 形式の検索に名前を付けて端末に保存する */
const RE_KEY = "trl-resaved";
function loadResaved() { try { const o = JSON.parse(localStorage.getItem(RE_KEY) || "{}"); return o && typeof o === "object" && !Array.isArray(o) ? o : {}; } catch (e) { return {}; } }
function saveResaved(o) { try { localStorage.setItem(RE_KEY, JSON.stringify(o)); } catch (e) { /* 保存不可でも操作は続ける */ } }
function syncReSel() {
  const names = Object.keys(loadResaved()).sort((a, b) => a.localeCompare(b, "ja"));
  $("#resel").innerHTML = names.length ? names.map(n => `<option value="${esc(n)}">${esc(n)}</option>`).join("") : `<option value="">（保存した正規表現なし）</option>`;
}
$("#resave").addEventListener("click", e => {
  const name = $("#rename").value.trim();
  const q = $("#q").value.trim();
  if (!name) { flashBtn(e.currentTarget, "名前を入れてください"); return; }
  if (!regexOf(q)) { flashBtn(e.currentTarget, "/パターン/ の形で検索欄に入れてください"); return; }
  const all = loadResaved(); all[name] = q; saveResaved(all);
  syncReSel(); $("#resel").value = name; $("#rename").value = "";
  flashBtn(e.currentTarget, "保存しました");
});
$("#reapply").addEventListener("click", () => {
  const name = $("#resel").value; const all = loadResaved();
  if (name && all[name]) { $("#q").value = all[name]; render(); }
});
$("#redel").addEventListener("click", () => {
  const name = $("#resel").value; if (!name) return;
  const all = loadResaved(); delete all[name]; saveResaved(all); syncReSel();
});
syncReSel();
function importFiltersFromUrl(prm) {
  if (!prm || ![...prm.keys()].some(k => k === "q" || k.startsWith("f_"))) return;
  const csv = k => { const v = prm.get("f_" + k); return v ? v.split(",").filter(Boolean) : []; };
  restoreFilterState({q: prm.get("q") || "", sort: prm.get("sort") || "status",
    st: csv("st"), trl: csv("trl"), dir: csv("dir"), mk: csv("mk"), sc: csv("sc"),
    tag: csv("tag"), stg: csv("stg"), tier: csv("tier"), gen: csv("gen"), own: csv("own"), hold: csv("hold"), move: csv("move"),
    slo: prm.get("f_slo") || "", shi: prm.get("f_shi") || "", ev: prm.get("f_ev") || ""});
}
/* 現在の絞り込み結果の書き出し。ブラウザ内だけで完結し、値は data.json の
   直読み、並びは画面の表示順そのまま (画面側で並べ替えや換算はしない) */
function exportData(fmt) {
  const rows = filtered().map(p => ({
    id: p.id, title: dispT(p), title_orig: p.title,
    companies: p.entities.filter(e => e.strength === "strong").map(e => e.name).join(" / "),
    source: p.source, published: p.published || "", status: p.display_status,
    total: p.long && typeof p.long.total === "number" ? p.long.total : null,
    trl: lvl(p), direction: dirOf(p) || "",
    evaluated_at: (p.long && p.long.evaluated_at) || (p.event && p.event.evaluated_at) || "",
    url: p.url}));
  let blob, name;
  if (fmt === "json") {
    blob = new Blob([JSON.stringify(rows, null, 1)], {type: "application/json"});
    name = "trl-results.json";
  } else {
    const head = ["id", "title", "companies", "source", "published", "status", "total", "trl", "direction", "evaluated_at", "url"];
    const q = v => { const s = String(v ?? ""); return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s; };
    blob = new Blob(["\ufeff" + [head.join(","), ...rows.map(r => head.map(k => q(r[k])).join(","))].join("\r\n")], {type: "text/csv"});
    name = "trl-results.csv";
  }
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob); a.download = name; a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 4000);
}
$("#expcsv").addEventListener("click", () => exportData("csv"));
$("#expjson").addEventListener("click", () => exportData("json"));
function downloadBlob(blob, name) {
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob); a.download = name; a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 4000);
}
/* data.json のスナップショット保存 (今見ているデータそのもの) */
function saveSnapshot() {
  const day = (META.generated_at || "").slice(0, 10) || "unknown";
  downloadBlob(new Blob([JSON.stringify(META)], {type: "application/json"}), `trl-data-${day}.json`);
}
/* 週次レポート (Markdown)。最新ランの digest と直近7日の評価件数から
   決定的に組む (新しい集計は履歴の件数だけ) */
function weeklyMd() {
  const lines = ["# TRL Explorer 週次レポート", "", `生成: ${META.generated_at || ""}`, ""];
  const dg = META.digest || [];
  const fin = dg.filter(x => x.status === "final").length, hold = dg.filter(x => x.status === "hold").length;
  lines.push(`## 最新ランの評価 (${dg.length}件)`, "", `確定 ${fin}件 / 保留 ${hold}件`, "");
  for (const x of dg) {
    const p = P.find(y => y.id === x.candidate_id);
    const nm = p ? candName(p) : x.candidate_id;
    const pv = x.prev ? ` (前回 総合 ${x.prev.total ?? "—"} / TRL ${x.prev.trl_level ?? "未確定"})` : " (初回)";
    lines.push(`- ${nm}: ${SL[x.status] || x.status} 総合 ${x.total ?? "—"} / TRL ${x.trl_level ?? "未確定"}${pv}`);
  }
  const genMs = Date.parse(META.generated_at || "");
  let n7 = 0;
  for (const p of P) for (const pt of evalPoints(p)) {
    const t = Date.parse(pt.evaluated_at || "");
    if (!Number.isNaN(t) && !Number.isNaN(genMs) && t <= genMs && genMs - t <= 7 * 86400000) n7++;
  }
  lines.push("", "## 直近7日", "", `評価 ${n7}件`, "", `出典: TRL Explorer (data.json ${META.generated_at || ""})`);
  return lines.join("\n");
}
function saveWeekly() { downloadBlob(new Blob([weeklyMd()], {type: "text/markdown"}), "trl-weekly.md"); }
/* 引用の BibTeX 風形式 */
function bibtexOf(p) {
  const year = (p.published || "").slice(0, 4) || "n.d.";
  const key = (p.id || "trl").replace(/[^a-zA-Z0-9]/g, "");
  return `@misc{${key},\n  title = {${p.title}},\n  year = {${year}},\n  howpublished = {${p.url || ""}},\n  note = {TRL Explorer: ${detailUrl(p)}}\n}`;
}
/* サマリーカード画像 (1200x630)。値は候補の直読みを描くだけ */
function saveCardImage(p) {
  const cv = document.createElement("canvas");
  cv.width = 1200; cv.height = 630;
  const g = cv.getContext("2d");
  if (!g) return;
  g.fillStyle = "#0c171d"; g.fillRect(0, 0, 1200, 630);
  g.fillStyle = "#4a6cf7"; g.fillRect(0, 0, 1200, 10);
  const cut = (s, n) => s.length > n ? s.slice(0, n) + "…" : s;
  g.fillStyle = "#e3eef3"; g.font = "700 44px sans-serif";
  g.fillText(cut(candName(p), 34), 64, 130);
  g.font = "28px sans-serif"; g.fillStyle = "#a5bac6";
  g.fillText(cut(dispT(p), 58), 64, 190);
  const L = p.long;
  g.font = "700 120px sans-serif"; g.fillStyle = "#e3eef3";
  g.fillText(L && L.total != null ? String(L.total) : "—", 64, 370);
  g.font = "30px sans-serif"; g.fillStyle = "#a5bac6";
  g.fillText(`総合点 / TRL ${lvl(p) ?? "未確定"}${L && L.band ? " / " + L.band : ""}`, 64, 430);
  g.fillText(`${SL[p.display_status] || p.display_status} ・ 評価 ${L && L.evaluated_at ? L.evaluated_at.slice(0, 10) : "—"}`, 64, 490);
  g.font = "24px sans-serif";
  g.fillText("TRL Explorer", 64, 575);
  cv.toBlob(b => { if (b) downloadBlob(b, `trl-card-${p.id}.png`); }, "image/png");
}
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
  const c = e.target.closest("[data-id]"); if (c) { if (VMODE) { toggleVSel(c.dataset.id); return; } sel = c.dataset.id; render(); }
});
$("#cpypins").addEventListener("click", e => {
  const btn = e.currentTarget;
  // 共有リンクには注目ピンと新着の基準時刻 (?seen=) を含める。基準は
  // 現在の新着判定に使っている値そのもので、送り先が取り込めば送り主と
  // 同じ新着の見え方になる。基準は新しい評価が来るまで変わらないので、
  // コピーのたびに進めたりはしない
  const parts = [];
  if (pins.size) parts.push(`pins=${[...pins].map(encodeURIComponent).join(",")}`);
  if (newBase) parts.push(`seen=${encodeURIComponent(newBase)}`);
  copyText(`${location.origin}${location.pathname}?${parts.join("&")}`, btn);
});
$("#det").addEventListener("input", e => { if (e.target.id === "note" && sel) noteDrafts[sel] = e.target.value; });
$("#det").addEventListener("click", e => {
  if (e.target.closest("#save")) {
    const saved = setNote(sel, $("#note").value); if (saved) delete noteDrafts[sel];
    $("#msg").textContent = saved ? "この端末のブラウザに保存しました。" : "保存できませんでした(ブラウザの保存機能が無効の可能性があります)。";
  }
  if (e.target.closest("#pint")) { togglePin(sel); return; }
  if (e.target.closest("#absboth")) {
    const box = $("#absbothbox");
    if (box) { box.hidden = !box.hidden; box.style.display = box.hidden ? "none" : "grid"; }
    return;
  }
  const co = e.target.closest("#cpyo"), cj = e.target.closest("#cpyj"), cs = e.target.closest("#cpys"), ce = e.target.closest("#cpye"), cm = e.target.closest("#cpym");
  const cb = e.target.closest("#cpyb"), ci = e.target.closest("#cardimg");
  if (cb || ci) {
    const p = P.find(x => x.id === sel);
    if (!p) return;
    if (cb) copyBuilt(() => bibtexOf(p), cb);
    else saveCardImage(p);
    return;
  }
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
/* BAN の操作 (追加・スイッチ・取り消し) */
{
  const banbox = $("#banbox");
  if (banbox) {
    try { if (localStorage.getItem("trl-banfold") === "1") banbox.open = false; } catch (e) { /* 保存不可なら開いたまま */ }
    banbox.addEventListener("toggle", () => { try { localStorage.setItem("trl-banfold", banbox.open ? "0" : "1"); } catch (e) { /* 保存できなくても畳める */ } });
  }
  const addBtn = $("#banadd"), typeSel = $("#bantype");
  if (addBtn) addBtn.addEventListener("click", () => {
    const t = $("#bantype").value;
    let v = $("#banval").value.trim();
    if (!v) return;
    // 候補に完全一致が無く、含む候補が1つだけならそちらに寄せる
    // (「Genmab」と打って「Genmab A/S」を指す、のを取りこぼさないため)
    const opts = banOptions(t);
    if (!opts.includes(v)) {
      const hits = opts.filter(o => o.toLowerCase().includes(v.toLowerCase()));
      if (hits.length === 1) v = hits[0];
    }
    if (!BANS.some(b => b.t === t && b.v === v)) { BANS.push({t, v, on: true}); saveBans(); buzz(); }
    $("#banval").value = "";
    buildBanUI(); render();
  });
  if (typeSel) typeSel.addEventListener("change", buildBanUI);
  document.addEventListener("change", e => {
    const sw = e.target.closest && e.target.closest("[data-ban]");
    if (sw) { const b = BANS[+sw.dataset.ban]; if (b) { b.on = sw.checked; saveBans(); buildBanUI(); render(); } }
  });
  document.addEventListener("click", e => {
    const x = e.target.closest && e.target.closest("[data-banx]");
    if (x) { BANS.splice(+x.dataset.banx, 1); saveBans(); buildBanUI(); render(); }
  });
  /* BAN一式の書き出し/読み込み (視点の共有・バックアップ用) */
  const expBtn = $("#banexp"), impBtn = $("#banimpb"), impFile = $("#banimp");
  if (expBtn) expBtn.addEventListener("click", () => {
    downloadBlob(new Blob([JSON.stringify({version: 1, bans: captureBanState()}, null, 1)], {type: "application/json"}), "trl-bans.json");
    toast("BANを書き出しました");
  });
  if (impBtn && impFile) {
    impBtn.addEventListener("click", () => impFile.click());
    impFile.addEventListener("change", () => {
      const f = impFile.files && impFile.files[0];
      if (!f) return;
      f.text().then(tx => {
        try {
          const o = JSON.parse(tx);
          restoreBanState(o.bans || o);
          render(); toast("BANを読み込みました");
        } catch (e) { toast("BANファイルを読み込めませんでした"); }
        impFile.value = "";
      });
    });
  }
}
load();
