/* 滑らかスクロール (氷の摩擦モデル + 指のノイズ補正) — 単体移植版
   使い方: このファイル全体を <script> として読み込む (または貼る) だけ。
   依存は無し。要素取得 ($) と通知 (toast) は干渉しないよう局所版を内蔵し、
   全体を即時関数で包んでいるので、既存ページの同名の関数や変数と衝突しない。
   入/切: window.trlSmooth.toggle() (状態は localStorage "trl-smooth"、既定は入)。
   本体 (一覧ページ) との唯一の違い: パレット等の表示中判定を、本体専用の
   #pal / #khelp ではなく、汎用の .palov (表示中のもの) で行う。 */

(() => {
"use strict";
const $ = s => document.querySelector(s);
let toastTimer = null;
function toast(msg) {
  let t = document.getElementById("trl-toast");
  if (!t) {
    t = document.createElement("div");
    t.id = "trl-toast";
    t.style.cssText = "position:fixed;left:50%;bottom:18px;transform:translateX(-50%);"
      + "background:var(--surface,#fff);border:1px solid var(--line,#ccc);color:var(--ink,#222);"
      + "padding:8px 14px;border-radius:10px;font-size:13px;z-index:60;box-shadow:0 4px 18px rgba(0,0,0,.35)";
    document.body.appendChild(t);
  }
  t.textContent = msg;
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.hidden = true; }, 2400);
}

/* 滑らかスクロール: 氷の上を滑るような摩擦モデル。ホイールの刻みは
   「目標位置」を積み上げるだけにし、実際の移動は毎フレーム
   「残りの距離 × (1 - e^(-dt/τ))」で近づける指数減衰で決める。
   速度が残距離に比例するので、刻みは連続につながり、止まり際は
   限りなく滑らかに減速する。τ (時定数、ms) が摩擦の強さで、
   大きいほど氷のように滑る。総移動量は刻みの合計と一致する。
   トラックパッドの細かいピクセル入力は素の動きが既に滑らかなので触らない。
   内側にスクロール領域がある場所 (パレット等) と、パレット/ヘルプ表示中は
   触らない。Shift+S キーで入/切 (端末に保存) */
const SM_TAU = 170;
let smoothOn = true;
try { smoothOn = localStorage.getItem("trl-smooth") !== "0"; } catch (e) { /* 読めなければ入 */ }
let smAnim = null, smCur = 0, smLast = 0;
/* 人間の指のノイズ補正 (目標側を整える):
   1. ノッチ正規化: 刻みの大きさの直近中央値を「そのホイールの標準ノッチ」
      とし、入力をその整数倍に丸める。端数は捨てずに残りとして次へ繰り越す
   3. 単発反転の保留: 進行方向と逆の刻みは 200ms 保留し、元の方向に戻れば
      ノイズとして捨て、逆向きが続けば本物の方向転換としてまとめて反映する
   5. 微小量の不感帯: 滑走中とその直後に届く小さな入力は即座に動かさず、
      貯めて 40px を超えたらまとめて反映する (指の揺れ対策) */
let smNotches = [], smResid = 0, smMicro = 0, smDir = 0, smRev = null, smGlideEnd = 0;
function smApply(d, now) {
  const maxY = Math.max(0, document.documentElement.scrollHeight - window.innerHeight);
  const base = smAnim ? smAnim.to : window.scrollY;
  const to = Math.min(maxY, Math.max(0, base + d));
  if (!smAnim) {
    if (to === window.scrollY) return;
    smCur = window.scrollY;
    smAnim = {to};
    smLast = now;
    requestAnimationFrame(smTick);
  } else smAnim.to = to;
  if (d) smDir = Math.sign(d);
}
function smTick(now) {
  if (smRev && now - smRev.t > 200) { const d = smRev.d; smRev = null; smApply(d, now); }
  if (!smAnim) { smGlideEnd = now; return; }
  const dt = Math.min(50, Math.max(1, now - (smLast || now))); smLast = now;
  const rem = smAnim.to - smCur;
  if (Math.abs(rem) < 0.6) { window.scrollTo(0, smAnim.to); smAnim = null; smGlideEnd = now; return; }
  let step = rem * (1 - Math.exp(-dt / SM_TAU));
  step = Math.max(-240, Math.min(240, step));
  smCur += step;
  window.scrollTo(0, smCur);
  requestAnimationFrame(smTick);
}
document.addEventListener("wheel", e => {
  if (!smoothOn || e.ctrlKey) return;  // ctrl+ホイールはブラウザズーム
  if (document.querySelector(".palov:not([hidden])")) return;
  let el = e.target;
  while (el && el !== document.body) {
    if (el instanceof Element) {
      const st = getComputedStyle(el);
      if ((st.overflowY === "auto" || st.overflowY === "scroll") && el.scrollHeight > el.clientHeight) return;
    }
    el = el.parentElement;
  }
  const unit = e.deltaMode === 1 ? 33 : e.deltaMode === 2 ? window.innerHeight : 1;
  const now = performance.now();
  let d = e.deltaY * unit;
  if (e.deltaMode === 0 && Math.abs(d) < 40) {
    // 細かい入力: 滑走中とその直後 (500ms) だけ不感帯に貯める。それ以外
    // (トラックパッド等) は今までどおり素通し
    if (!smAnim && now - smGlideEnd > 500) return;
    e.preventDefault();
    smMicro += d;
    if (Math.abs(smMicro) >= 40) { const m = smMicro; smMicro = 0; smApply(m, now); }
    return;
  }
  e.preventDefault();
  if (smMicro) { const m = smMicro; smMicro = 0; smApply(m, now); }
  if (e.deltaMode !== 2) {
    // ノッチ正規化 (ページ単位の入力は対象外)
    smNotches.push(Math.abs(d));
    if (smNotches.length > 9) smNotches.shift();
    if (smNotches.length >= 3) {
      const med = [...smNotches].sort((a, b) => a - b)[smNotches.length >> 1];
      if (med > 0) {
        const total = d + smResid;
        const q = Math.sign(total) * med * Math.max(1, Math.round(Math.abs(total) / med));
        smResid = total - q;
        d = q;
      }
    }
  }
  // 単発反転の保留 (滑走中とその直後だけ。止まってから十分経った後の
  // 反転は新しい意図として即座に反映する。保留の失効はタイマーでも担保)
  if (smDir && Math.sign(d) !== smDir && (smAnim || now - smGlideEnd <= 500)) {
    if (smRev && Math.sign(smRev.d) === Math.sign(d)) {
      const held = smRev.d; smRev = null;
      smApply(held + d, now);
    } else {
      smRev = {d, t: now};
      setTimeout(() => { if (smRev && performance.now() - smRev.t > 200) { const hd = smRev.d; smRev = null; smApply(hd, performance.now()); } }, 220);
    }
    return;
  }
  if (smRev) smRev = null;  // 元の方向に戻った: 保留分はノイズとして捨てる
  smApply(d, now);
}, {passive: false});
document.addEventListener("keydown", () => { smAnim = null; }, true);
function toggleSmooth() {
  smoothOn = !smoothOn;
  smAnim = null;
  try { localStorage.setItem("trl-smooth", smoothOn ? "1" : "0"); } catch (e) { /* 保存不可でも切り替えは効く */ }
  toast(smoothOn ? "滑らかスクロール: 入" : "滑らかスクロール: 切");
}

window.trlSmooth = { toggle: toggleSmooth };
})();
