
"use strict";

const demoPapers = [
  {
    id: "paper-001",
    title: "Solid-state electrolytes for next-generation lithium batteries",
    source: "arXiv",
    date: "2026-10-18",
    url: "https://arxiv.org/",
    trl: 5,
    status: "final",
    tags: ["固体電池", "エネルギー", "材料科学"],
    abstract: "固体電解質を用いた次世代リチウムイオン電池に関する研究。イオン伝導性と安定性の両立を目指し、実用化に向けた技術課題を検討する。",
    summary: "固体電解質のイオン伝導性と安定性を評価する研究。技術面の進展だけでなく、量産性・コスト・顧客導入の検証が事業化判断の鍵となる。",
    citations: 42,
    companies: ["Panasonic", "Toyota"],
    strengths: [
      "イオン伝導性の改善を検討している",
      "安定性とサイクル特性を評価している"
    ],
    missing: [
      "収益化の見通し（monetization）",
      "市場成長性（market_growth）",
      "財務の持続可能性（financial_sustainability）",
      "競合優位性の持続性（moat_durability）"
    ],
    note: ""
  },
  {
    id: "paper-002",
    title: "Quantum error correction with surface codes",
    source: "arXiv",
    date: "2026-10-16",
    url: "https://arxiv.org/",
    trl: 4,
    status: "review",
    tags: ["量子コンピューティング", "情報科学", "物理"],
    abstract: "表面符号を用いた量子誤り訂正の手法を検討。実験条件や計算規模に応じた実用化上の課題を分析する。",
    summary: "量子誤り訂正の技術的な進展を調べる研究。実機での再現性、計算資源、運用コストなどの追加確認が必要。",
    citations: 28,
    companies: ["IBM", "Google"],
    strengths: [
      "誤り訂正手法を具体的に検討している",
      "実用化に必要な技術課題を整理できる"
    ],
    missing: [
      "商用環境での再現性",
      "導入コストと収益化の見通し",
      "競合方式との比較"
    ],
    note: ""
  },
  {
    id: "paper-003",
    title: "CRISPR-based gene therapy for rare diseases",
    source: "PubMed",
    date: "2026-10-14",
    url: "https://pubmed.ncbi.nlm.nih.gov/",
    trl: 3,
    status: "hold",
    tags: ["遺伝子治療", "バイオテクノロジー", "医療"],
    abstract: "希少疾患を対象とする遺伝子治療の研究。前臨床段階における治療効果と安全性の検証を扱う。",
    summary: "臨床応用には有望な技術的要素があるものの、前臨床データだけで製品化や収益化を判断することはできない。",
    citations: 16,
    companies: ["研究関連企業未確認"],
    strengths: [
      "対象疾患と技術的な目的が明確"
    ],
    missing: [
      "臨床試験での有効性と安全性",
      "規制当局の承認見通し",
      "収益化と市場規模"
    ],
    note: ""
  },
  {
    id: "paper-004",
    title: "Sustainable catalysts for CO₂ conversion",
    source: "arXiv",
    date: "2026-10-12",
    url: "https://arxiv.org/",
    trl: 2,
    status: "rejected",
    tags: ["カーボンニュートラル", "触媒", "環境"],
    abstract: "二酸化炭素の変換に向けた触媒技術を検討。材料の特性と反応条件を中心に研究している。",
    summary: "基礎研究段階の成果。実用条件での性能、耐久性、製造コストに関する情報が不足している。",
    citations: 9,
    companies: [],
    strengths: [
      "研究対象となる化学反応が明確"
    ],
    missing: [
      "実用条件での性能",
      "製造コストと耐久性",
      "事業化の具体的な経路"
    ],
    note: ""
  },
  {
    id: "paper-005",
    title: "Scalable Exact Non-Pauli Noise Simulation for Fault-Tolerant Quantum Circuits",
    source: "arXiv",
    date: "2026-10-10",
    url: "https://arxiv.org/",
    trl: null,
    status: "error",
    tags: ["量子コンピューティング", "シミュレーション"],
    abstract: "耐故障量子回路におけるノイズシミュレーション手法を扱う研究。",
    summary: "評価処理でエラーが発生したため、TRLや事業化に関する結論は未確定。論文情報と評価ログを確認する必要がある。",
    citations: null,
    companies: [],
    strengths: [],
    missing: [
      "TRL評価が未確定",
      "収益化の見通し",
      "市場成長性",
      "財務の持続可能性",
      "競合優位性の持続性"
    ],
    note: "",
    error: "デモ用の評価エラー。実際のAPIエラーを表すものではありません。"
  }
];

const statusLabels = {
  final: "選定可能",
  review: "要追加調査",
  hold: "保留",
  error: "エラー",
  rejected: "除外"
};

const statusClasses = {
  final: "status-badge",
  review: "status-badge status-review",
  hold: "status-badge status-hold",
  error: "status-badge status-error",
  rejected: "status-badge status-rejected"
};

const elements = {
  searchForm: document.querySelector("#searchForm"),
  searchInput: document.querySelector("#searchInput"),
  statusFilter: document.querySelector("#statusFilter"),
  trlFilter: document.querySelector("#trlFilter"),
  sortFilter: document.querySelector("#sortFilter"),
  resetButton: document.querySelector("#resetButton"),
  resultsList: document.querySelector("#resultsList"),
  detailPanel: document.querySelector("#detailPanel"),
  emptyState: document.querySelector("#emptyState"),
  resultCount: document.querySelector("#resultCount"),
  totalCount: document.querySelector("#totalCount"),
  finalCount: document.querySelector("#finalCount"),
  reviewCount: document.querySelector("#reviewCount"),
  holdCount: document.querySelector("#holdCount")
};

let selectedId = demoPapers[0].id;
let filteredPapers = [];
const papers = demoPapers.map(paper => ({ ...paper }));

function escapeHTML(value) {
  return String(value ?? "").replace(/[&<>"']/g, char => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;"
  })[char]);
}

function formatNumber(value) {
  return value == null ? "—" : Number(value).toLocaleString("ja-JP");
}

function getPaper(id) {
  return papers.find(paper => paper.id === id);
}

function sourceClass(source) {
  if (source === "arXiv") return "source-badge source-arxiv";
  if (source === "PubMed") return "source-badge source-pubmed";
  return "source-badge source-other";
}

function statusBadge(status) {
  return `
    <span class="${statusClasses[status] || "status-badge"}">
      ${escapeHTML(statusLabels[status] || status)}
    </span>`;
}

function trlMarkup(trl, detail = false) {
  const known = Number.isInteger(trl) && trl >= 1 && trl <= 9;
  const segments = Array.from({ length: 9 }, (_, index) => `
    <span class="trl-segment ${known && index < trl ? "is-filled" : ""}"></span>
  `).join("");

  return `
    <div class="${detail ? "detail-tracks" : "trl-display"}">
      <div class="trl-number ${known ? "" : "is-unknown"}">
        <span>TRL</span>
        <strong>${known ? trl : "未確定"}</strong>
      </div>
      <div class="trl-track" role="img"
        aria-label="${known ? `TRL ${trl} / 9` : "TRL未確定"}">
        ${segments}
      </div>
    </div>`;
}

function matchesTRL(paper, filter) {
  const trl = paper.trl;

  switch (filter) {
    case "known":
      return Number.isInteger(trl) && trl >= 1 && trl <= 9;
    case "unknown":
      return !Number.isInteger(trl) || trl < 1 || trl > 9;
    case "early":
      return Number.isInteger(trl) && trl >= 1 && trl <= 3;
    case "middle":
      return Number.isInteger(trl) && trl >= 4 && trl <= 6;
    case "late":
      return Number.isInteger(trl) && trl >= 7 && trl <= 9;
    default:
      return true;
  }
}

function matchesSearch(paper, query) {
  if (!query) return true;

  const searchable = [
    paper.title,
    paper.source,
    paper.abstract,
    paper.summary,
    ...(paper.tags || []),
    ...(paper.companies || []),
    ...(paper.missing || [])
  ].join(" ").toLocaleLowerCase("ja");

  return searchable.includes(query.toLocaleLowerCase("ja"));
}

function getFilteredPapers() {
  const query = elements.searchInput.value.trim();
  const status = elements.statusFilter.value;
  const trl = elements.trlFilter.value;
  const sort = elements.sortFilter.value;

  const result = papers.filter(paper =>
    matchesSearch(paper, query) &&
    (status === "all" || paper.status === status) &&
    matchesTRL(paper, trl)
  );

  result.sort((a, b) => {
    if (sort === "trl") {
      const aTRL = Number.isInteger(a.trl) ? a.trl : -1;
      const bTRL = Number.isInteger(b.trl) ? b.trl : -1;
      return bTRL - aTRL;
    }

    if (sort === "title") {
      return a.title.localeCompare(b.title, "ja");
    }

    return b.date.localeCompare(a.date);
  });

  return result;
}

function renderStats() {
  elements.totalCount.textContent = formatNumber(papers.length);
  elements.finalCount.textContent =
    formatNumber(papers.filter(p => p.status === "final").length);
  elements.reviewCount.textContent =
    formatNumber(papers.filter(p => p.status === "review").length);
  elements.holdCount.textContent =
    formatNumber(papers.filter(p =>
      p.status === "hold" || p.status === "error"
    ).length);
}

function renderPaperCard(paper) {
  const selected = paper.id === selectedId;
  const tags = (paper.tags || []).map(tag =>
    `<span class="tag">${escapeHTML(tag)}</span>`
  ).join("");

  return `
    <article class="paper-card ${selected ? "is-selected" : ""}"
      data-paper-id="${escapeHTML(paper.id)}">
      <div class="paper-card-main">
        <div class="paper-card-content">
          <div class="paper-topline">
            <span class="${sourceClass(paper.source)}">
              ${escapeHTML(paper.source)}
            </span>
            <span class="paper-date">${escapeHTML(paper.date)}</span>
          </div>

          <h3 class="paper-title">${escapeHTML(paper.title)}</h3>
          <p class="paper-abstract">${escapeHTML(paper.abstract)}</p>

          <div class="paper-tags">${tags}</div>

          <div class="paper-meta">
            <span class="paper-meta-item">
              引用数 <strong>${formatNumber(paper.citations)}</strong>
            </span>
            <span class="paper-meta-item">
              関連企業 <strong>${(paper.companies || []).length ? "情報あり" : "未確認"}</strong>
            </span>
          </div>
        </div>

        <div class="paper-side">
          ${statusBadge(paper.status)}
          ${trlMarkup(paper.trl)}
        </div>
      </div>

      <div class="paper-footer">
        <div class="paper-footer-info">
          <span>${paper.missing?.length
            ? `確認項目 ${paper.missing.length}件`
            : "不足項目なし"}</span>
        </div>
        <button class="detail-toggle" type="button"
          data-select-paper="${escapeHTML(paper.id)}"
          aria-pressed="${selected}">
          ${selected ? "選択中 ✓" : "詳細を見る →"}
        </button>
      </div>
    </article>`;
}

function renderResults() {
  filteredPapers = getFilteredPapers();

  elements.resultCount.textContent = String(filteredPapers.length);
  elements.resultsList.innerHTML =
    filteredPapers.map(renderPaperCard).join("");

  elements.emptyState.hidden = filteredPapers.length !== 0;

  if (!filteredPapers.some(paper => paper.id === selectedId)) {
    selectedId = filteredPapers[0]?.id ?? null;
    elements.resultsList.innerHTML =
      filteredPapers.map(renderPaperCard).join("");
  }

  renderDetail();
}

function renderList(items, type) {
  if (!items || items.length === 0) {
    return `<p class="detail-hint">登録された情報はありません。</p>`;
  }

  return `
    <ul class="point-list">
      ${items.map(item => `
        <li>
          <span class="point-icon ${type === "warning" ? "warning" : ""}">
            ${type === "warning" ? "!" : "✓"}
          </span>
          <span>${escapeHTML(item)}</span>
        </li>
      `).join("")}
    </ul>`;
}

function renderDetail() {
  const paper = getPaper(selectedId);

  if (!paper) {
    elements.detailPanel.innerHTML = `
      <div class="detail-placeholder">
        <span class="placeholder-icon">⌕</span>
        <h3>論文が選択されていません</h3>
        <p>検索条件を変更するか、一覧から論文を選択してください。</p>
      </div>`;
    return;
  }

  const companies = (paper.companies || []).map(company =>
    `<span class="company-chip">${escapeHTML(company)}</span>`
  ).join("");

  elements.detailPanel.innerHTML = `
    <div class="detail-content">
      <header class="detail-header">
        <div class="detail-header-top">
          <span class="${sourceClass(paper.source)}">
            ${escapeHTML(paper.source)}
          </span>
          ${statusBadge(paper.status)}
        </div>

        <h3 class="detail-title">${escapeHTML(paper.title)}</h3>

        <div class="detail-meta">
          <span>公開日 ${escapeHTML(paper.date)}</span>
          <span>引用数 ${formatNumber(paper.citations)}</span>
        </div>
      </header>

      <section class="detail-section">
        <h4>研究概要</h4>
        <p>${escapeHTML(paper.summary)}</p>
      </section>

      <section class="detail-section">
        <h4>技術成熟度（TRL）</h4>
        ${trlMarkup(paper.trl, true)}
        <p class="detail-hint">
          TRLは技術成熟度の目安です。収益性や市場性を直接示すものではありません。
        </p>
      </section>

      <section class="detail-section">
        <h4>評価できる点</h4>
        ${renderList(paper.strengths, "strength")}
      </section>

      <section class="detail-section">
        <h4>不足している観点</h4>
        ${renderList(paper.missing, "warning")}
      </section>

      ${paper.error ? `
        <section class="detail-section">
          <h4>処理上の注意</h4>
          <p>${escapeHTML(paper.error)}</p>
        </section>` : ""}

      <section class="detail-section">
        <h4>関連企業</h4>
        <div class="company-list">
          ${companies || '<span class="detail-hint">関連企業は未確認です。</span>'}
        </div>
      </section>

      <section class="detail-section">
        <h4>調査メモ</h4>
        <textarea class="detail-note" id="paperNote"
          aria-label="調査メモ"
          placeholder="仮説、確認したい点、次の調査などを記録..."
        >${escapeHTML(paper.note || "")}</textarea>

        <div class="detail-actions">
          <button class="button button-secondary" type="button"
            id="saveNoteButton">メモを保存</button>
          <a class="button button-source"
            href="${escapeHTML(paper.url)}"
            target="_blank" rel="noopener noreferrer">
            原文サイトを開く ↗
          </a>
        </div>
        <p id="noteMessage" class="detail-hint" aria-live="polite"></p>
      </section>
    </div>`;
}

function selectPaper(id) {
  if (!getPaper(id)) return;

  selectedId = id;
  renderResults();
}

function resetFilters() {
  elements.searchInput.value = "";
  elements.statusFilter.value = "all";
  elements.trlFilter.value = "all";
  elements.sortFilter.value = "date";
  selectedId = papers[0]?.id ?? null;
  renderResults();
}

function saveNote() {
  const paper = getPaper(selectedId);
  const noteField = document.querySelector("#paperNote");
  const message = document.querySelector("#noteMessage");

  if (!paper || !noteField || !message) return;

  paper.note = noteField.value;
  message.textContent =
    "この画面内に保存しました。ページ再読み込み後も保持する機能は未実装です。";
}

elements.searchForm.addEventListener("submit", event => {
  event.preventDefault();
  renderResults();
});

elements.searchInput.addEventListener("input", renderResults);
elements.statusFilter.addEventListener("change", renderResults);
elements.trlFilter.addEventListener("change", renderResults);
elements.sortFilter.addEventListener("change", renderResults);
elements.resetButton.addEventListener("click", resetFilters);

elements.resultsList.addEventListener("click", event => {
  const button = event.target.closest("[data-select-paper]");
  const card = event.target.closest("[data-paper-id]");
  const id = button?.dataset.selectPaper || card?.dataset.paperId;

  if (id) selectPaper(id);
});

elements.detailPanel.addEventListener("click", event => {
  if (event.target.closest("#saveNoteButton")) {
    saveNote();
  }
});

renderStats();
renderResults();
