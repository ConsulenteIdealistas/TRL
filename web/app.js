
const demoPapers = [
  {
    id: "demo-1",
    title: "Scalable Exact Non-Pauli Noise Simulation for Fault-Tolerant Quantum Circuits",
    source: "arXiv",
    date: "サンプル",
    company: "関連企業：未確認",
    status: "hold",
    trl: null,
    topics: ["量子コンピューティング", "ノイズシミュレーション"],
    abstract: "耐故障性量子回路における非パウリ型ノイズのシミュレーションに関する研究。技術的な成果と、商業化に必要なコスト・需要の評価は分けて確認する必要がある。",
    missing: ["収益化", "市場成長性", "財務的持続可能性", "競争優位性"],
    url: ""
  },
  {
    id: "demo-2",
    title: "Gene Therapy: Translational Research Overview",
    source: "PubMed",
    date: "サンプル",
    company: "関連企業：未確認",
    status: "error",
    trl: null,
    topics: ["遺伝子治療", "バイオテクノロジー"],
    abstract: "遺伝子治療分野の研究を想定した表示用サンプル。APIエラーと技術評価の不足を区別して表示するための例。",
    missing: [],
    url: ""
  },
  {
    id: "demo-3",
    title: "Advanced Materials for Next-Generation Batteries",
    source: "arXiv",
    date: "サンプル",
    company: "関連企業：未確認",
    status: "final",
    trl: 7,
    topics: ["全固体電池", "材料科学"],
    abstract: "次世代電池材料を想定した表示用サンプル。TRLは技術実証の段階を表す指標であり、商業的な成功を直接保証するものではない。",
    missing: [],
    url: ""
  }
];

const statusLabels = {
  final: "評価確定",
  hold: "保留",
  error: "エラー"
};

const searchInput = document.getElementById("searchInput");
const statusFilter = document.getElementById("statusFilter");
const trlFilter = document.getElementById("trlFilter");
const resetButton = document.getElementById("resetButton");
const resultsList = document.getElementById("resultsList");
const emptyState = document.getElementById("emptyState");

function escapeHTML(value) {
  return String(value ?? "").replace(/[&<>"']/g, char => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;"
  })[char]);
}

function matchesTRL(paper, filter) {
  if (filter === "all") return true;
  if (paper.trl == null) return filter === "unknown";
  if (filter === "low") return paper.trl >= 1 && paper.trl <= 3;
  if (filter === "mid") return paper.trl >= 4 && paper.trl <= 6;
  if (filter === "high") return paper.trl >= 7 && paper.trl <= 9;
  if (filter === "unknown") return false;
  return true;
}

function matchesSearch(paper, query) {
  if (!query) return true;

  const text = [
    paper.title,
    paper.source,
    paper.company,
    paper.abstract,
    ...paper.topics,
    ...paper.missing
  ].join(" ").toLocaleLowerCase("ja");

  return text.includes(query.toLocaleLowerCase("ja"));
}

function paperCard(paper) {
  const status = statusLabels[paper.status] || "未評価";
  const statusClass = ["final", "hold", "error"].includes(paper.status)
    ? paper.status
    : "unknown";

  const trlText = paper.trl == null
    ? "TRL：未確定"
    : `TRL：${paper.trl}/9`;

  const missingHTML = paper.missing.length
    ? `<span class="missing-note">不足情報：${paper.missing.map(escapeHTML).join("・")}</span>`
    : `<span class="missing-note">不足情報：なし</span>`;

  const linkHTML = paper.url
    ? `<a class="paper-link" href="${escapeHTML(paper.url)}" target="_blank" rel="noopener noreferrer">原文を開く ↗</a>`
    : `<span class="paper-link">原文リンクはデータ連携後に表示</span>`;

  return `
    <article class="paper-card">
      <div class="paper-topline">
        <div>
          <h3 class="paper-title">${escapeHTML(paper.title)}</h3>
          <div class="paper-meta">
            <span>${escapeHTML(paper.source)}</span>
            <span>${escapeHTML(paper.date)}</span>
            <span>${escapeHTML(paper.company)}</span>
          </div>
        </div>
        <span class="status status-${statusClass}">${status}</span>
      </div>

      <p class="paper-abstract">${escapeHTML(paper.abstract)}</p>

      <div class="tag-list">
        <span class="tag">${escapeHTML(trlText)}</span>
        ${paper.topics.map(topic =>
          `<span class="tag tag-cyan">${escapeHTML(topic)}</span>`
        ).join("")}
        ${paper.missing.length
          ? `<span class="tag tag-neutral">要追加調査</span>`
          : ""}
      </div>

      <div class="paper-footer">
        ${missingHTML}
        ${linkHTML}
      </div>
    </article>
  `;
}

function render() {
  const query = searchInput.value.trim();
  const status = statusFilter.value;
  const trl = trlFilter.value;

  const filtered = demoPapers.filter(paper =>
    matchesSearch(paper, query) &&
    (status === "all" || paper.status === status) &&
    matchesTRL(paper, trl)
  );

  resultsList.innerHTML = filtered.map(paperCard).join("");
  emptyState.hidden = filtered.length !== 0;
  document.getElementById("resultCount").textContent =
    `${filtered.length}件`;

  document.getElementById("totalCount").textContent = demoPapers.length;
  document.getElementById("finalCount").textContent =
    demoPapers.filter(p => p.status === "final").length;
  document.getElementById("holdCount").textContent =
    demoPapers.filter(p => p.status === "hold").length;
  document.getElementById("errorCount").textContent =
    demoPapers.filter(p => p.status === "error").length;
}

searchInput.addEventListener("input", render);
statusFilter.addEventListener("change", render);
trlFilter.addEventListener("change", render);

resetButton.addEventListener("click", () => {
  searchInput.value = "";
  statusFilter.value = "all";
  trlFilter.value = "all";
  render();
});

document.addEventListener("keydown", event => {
  const target = event.target;
  const isTyping = target instanceof HTMLInputElement ||
    target instanceof HTMLTextAreaElement ||
    target instanceof HTMLSelectElement;

  if (event.key === "/" && !isTyping) {
    event.preventDefault();
    searchInput.focus();
  }
});

render();
