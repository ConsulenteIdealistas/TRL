"use strict";
const P=[
{id:"1",title:"Solid-state electrolytes for next-generation lithium batteries",source:"arXiv",date:"2026-10-18",url:"https://arxiv.org/",trl:5,status:"final",tags:["固体電池","エネルギー","材料科学"],abstract:"固体電解質を用いた次世代リチウムイオン電池に関する研究。イオン伝導性と安定性の両立を目指し、実用化に向けた技術課題を検討する。",summary:"固体電解質のイオン伝導性と安定性を評価する研究。技術面の進展だけでなく、量産性・コスト・顧客導入の検証が事業化判断の鍵となる。",citations:42,companies:["Panasonic","Toyota"],strengths:["イオン伝導性の改善を検討している","安定性とサイクル特性を評価している"],missing:["収益化の見通し（monetization）","市場成長性（market_growth）","財務の持続可能性（financial_sustainability）","競合優位性の持続性（moat_durability）"],note:""},
{id:"2",title:"Quantum error correction with surface codes",source:"arXiv",date:"2026-10-16",url:"https://arxiv.org/",trl:4,status:"review",tags:["量子コンピューティング","情報科学","物理"],abstract:"表面符号を用いた量子誤り訂正の手法を検討。実験条件や計算規模に応じた実用化上の課題を分析する。",summary:"量子誤り訂正の技術的な進展を調べる研究。実機での再現性、計算資源、運用コストなどの追加確認が必要。",citations:28,companies:["IBM","Google"],strengths:["誤り訂正手法を具体的に検討している","実用化に必要な技術課題を整理できる"],missing:["商用環境での再現性","導入コストと収益化の見通し","競合方式との比較"],note:""},
{id:"3",title:"CRISPR-based gene therapy for rare diseases",source:"PubMed",date:"2026-10-14",url:"https://pubmed.ncbi.nlm.nih.gov/",trl:3,status:"hold",tags:["遺伝子治療","バイオテクノロジー","医療"],abstract:"希少疾患を対象とする遺伝子治療の研究。前臨床段階における治療効果と安全性の検証を扱う。",summary:"臨床応用には有望な技術的要素があるものの、前臨床データだけで製品化や収益化を判断することはできない。",citations:16,companies:["研究関連企業未確認"],strengths:["対象疾患と技術的な目的が明確"],missing:["臨床試験での有効性と安全性","規制当局の承認見通し","収益化と市場規模"],note:""},
{id:"4",title:"Sustainable catalysts for CO₂ conversion",source:"arXiv",date:"2026-10-12",url:"https://arxiv.org/",trl:2,status:"rejected",tags:["カーボンニュートラル","触媒","環境"],abstract:"二酸化炭素の変換に向けた触媒技術を検討。材料の特性と反応条件を中心に研究している。",summary:"基礎研究段階の成果。実用条件での性能、耐久性、製造コストに関する情報が不足している。",citations:9,companies:[],strengths:["研究対象となる化学反応が明確"],missing:["実用条件での性能","製造コストと耐久性","事業化の具体的な経路"],note:""},
{id:"5",title:"Scalable Exact Non-Pauli Noise Simulation for Fault-Tolerant Quantum Circuits",source:"arXiv",date:"2026-10-10",url:"https://arxiv.org/",trl:null,status:"error",tags:["量子コンピューティング","シミュレーション"],abstract:"耐故障量子回路におけるノイズシミュレーション手法を扱う研究。",summary:"評価処理でエラーが発生したため、TRLや事業化に関する結論は未確定。論文情報と評価ログを確認する必要がある。",citations:null,companies:[],strengths:[],missing:["TRL評価が未確定","収益化の見通し","市場成長性","財務の持続可能性","競合優位性の持続性"],note:"",error:"デモ用の評価エラー。実際のAPIエラーを表すものではありません。"}];
const SL={final:"選定可能",review:"要追加調査",hold:"保留",error:"エラー",rejected:"除外"};
const $=s=>document.querySelector(s);
let sel="1";
const esc=v=>String(v??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const fmt=v=>v==null?"—":Number(v).toLocaleString("ja-JP");
const ok=t=>Number.isInteger(t)&&t>=1&&t<=9;
const src=s=>"badge "+(s==="arXiv"?"s-arxiv":s==="PubMed"?"s-pubmed":"");
const stB=s=>`<span class="st ${s==="final"?"":"st-"+s}">${SL[s]}</span>`;
function trl(t,d){const k=ok(t);return `<div class="${d?"det":"trl"}"><div class="num ${k?"":"u"}"><span>TRL</span><strong>${k?t:"未確定"}</strong></div><div class="track">${Array.from({length:9},(_,i)=>`<span class="seg ${k&&i<t?"f":""}"></span>`).join("")}</div></div>`}
function inR(t,f){return f==="known"?ok(t):f==="unknown"?!ok(t):f==="early"?ok(t)&&t<=3:f==="middle"?ok(t)&&t>=4&&t<=6:f==="late"?ok(t)&&t>=7:true}
function filtered(){const q=$("#q").value.trim().toLowerCase(),s=$("#fs").value,t=$("#ft").value,o=$("#fo").value;
const r=P.filter(p=>(!q||[p.title,p.source,p.abstract,p.summary,...p.tags,...p.companies,...p.missing].join(" ").toLowerCase().includes(q))&&(s==="all"||p.status===s)&&inR(p.trl,t));
r.sort((a,b)=>o==="trl"?(ok(b.trl)?b.trl:-1)-(ok(a.trl)?a.trl:-1):o==="title"?a.title.localeCompare(b.title,"ja"):b.date.localeCompare(a.date));return r}
function card(p){const s=p.id===sel;return `<article class="card ${s?"sel":""}" data-id="${p.id}"><div class="main"><div style="min-width:0"><div class="top"><span class="${src(p.source)}">${esc(p.source)}</span><span class="date">${p.date}</span></div><h3 class="title">${esc(p.title)}</h3><p class="abs">${esc(p.abstract)}</p><div class="tags">${p.tags.map(t=>`<span class="tag">${esc(t)}</span>`).join("")}</div><div class="mi"><span>引用数 <strong>${fmt(p.citations)}</strong></span><span>関連企業 <strong>${p.companies.length?"情報あり":"未確認"}</strong></span></div></div><div class="side">${stB(p.status)}${trl(p.trl)}</div></div><div class="foot"><span>${p.missing.length?`確認項目 ${p.missing.length}件`:"不足項目なし"}</span><button class="tg" type="button">${s?"選択中 ✓":"詳細を見る →"}</button></div></article>`}
const pts=(a,w)=>a.length?`<ul class="pts">${a.map(x=>`<li><b class="${w?"w":""}">${w?"!":"✓"}</b><span>${esc(x)}</span></li>`).join("")}</ul>`:`<p class="hint">登録された情報はありません。</p>`;
function detail(){const p=P.find(x=>x.id===sel);
if(!p){$("#det").innerHTML=`<div class="ph"><i>⌕</i><h3>論文が選択されていません</h3><p class="hint">検索条件を変更するか、一覧から論文を選択してください。</p></div>`;return}
$("#det").innerHTML=`<div class="dc"><header class="dh"><div class="top"><span class="${src(p.source)}">${esc(p.source)}</span>${stB(p.status)}</div><h3>${esc(p.title)}</h3><div class="mi"><span>公開日 ${p.date}</span><span>引用数 ${fmt(p.citations)}</span></div></header>
<section class="ds"><h4>研究概要</h4><p>${esc(p.summary)}</p></section>
<section class="ds"><h4>技術成熟度（TRL）</h4>${trl(p.trl,1)}<p class="hint">TRLは技術成熟度の目安です。収益性や市場性を直接示すものではありません。</p></section>
<section class="ds"><h4>評価できる点</h4>${pts(p.strengths)}</section>
<section class="ds"><h4>不足している観点</h4>${pts(p.missing,1)}</section>
${p.error?`<section class="ds"><h4>処理上の注意</h4><p>${esc(p.error)}</p></section>`:""}
<section class="ds"><h4>関連企業</h4>${p.companies.map(c=>`<span class="chip">${esc(c)}</span>`).join("")||'<span class="hint">関連企業は未確認です。</span>'}</section>
<section class="ds"><h4>調査メモ</h4><textarea id="note" aria-label="調査メモ" placeholder="仮説、確認したい点、次の調査などを記録...">${esc(p.note)}</textarea><div class="acts"><button class="btn sec" type="button" id="save">メモを保存</button><a class="btn src" href="${esc(p.url)}" target="_blank" rel="noopener noreferrer">原文サイトを開く ↗</a></div><p id="msg" class="hint" aria-live="polite"></p></section></div>`}
function render(){const r=filtered();$("#rc").textContent=r.length;$("#empty").hidden=r.length!==0;
if(!r.some(p=>p.id===sel))sel=r[0]?.id??null;
$("#list").innerHTML=r.map(card).join("");detail()}
function stats(){$("#c0").textContent=P.length;$("#c1").textContent=P.filter(p=>p.status==="final").length;$("#c2").textContent=P.filter(p=>p.status==="review").length;$("#c3").textContent=P.filter(p=>p.status==="hold"||p.status==="error").length}
$("#sf").addEventListener("submit",e=>{e.preventDefault();render()});
["#q"].forEach(s=>$(s).addEventListener("input",render));
["#fs","#ft","#fo"].forEach(s=>$(s).addEventListener("change",render));
$("#rs").addEventListener("click",()=>{$("#q").value="";$("#fs").value="all";$("#ft").value="all";$("#fo").value="date";sel=P[0].id;render()});
$("#list").addEventListener("click",e=>{const c=e.target.closest("[data-id]");if(c){sel=c.dataset.id;render()}});
$("#det").addEventListener("click",e=>{if(e.target.closest("#save")){const p=P.find(x=>x.id===sel);p.note=$("#note").value;$("#msg").textContent="この画面内に保存しました。ページ再読み込み後も保持する機能は未実装です。"}});
stats();render();
