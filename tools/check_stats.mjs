// web/app.js の統計関数を実物のまま抜き出し、R robustbase が生成した
// golden ベクトル (tools/stats-vectors.json) と照合する。
// 使い方 (リポジトリのルートから): node tools/check_stats.mjs
// 許容差: 平均・SD・中央値・四分位数・Sn は相対 1e-9、Qn は相対 3e-6
// (Qn の残差は robustbase 側の定数切捨て分のみ。stats_crosscheck.R 参照)
import fs from "fs";
import { fileURLToPath } from "url";
import path from "path";

const root = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const src = fs.readFileSync(path.join(root, "web", "app.js"), "utf8");
function grabFn(name) {
  const i = src.indexOf("function " + name);
  if (i < 0) throw new Error("not found in app.js: " + name);
  let d = 0, j = src.indexOf("{", i);
  for (; j < src.length; j++) { if (src[j] === "{") d++; else if (src[j] === "}") { d--; if (!d) break; } }
  return src.slice(i, j + 1);
}
function grabConst(name) {
  const i = src.indexOf("const " + name);
  if (i < 0) throw new Error("not found in app.js: const " + name);
  return src.slice(i, src.indexOf(";", i) + 1);
}
const code = [grabConst("SN_FINITE ="), grabConst("QN_FINITE ="), grabConst("snCorr ="), grabConst("qnCorr ="),
  grabConst("meanOf ="), grabFn("pct7"), grabFn("sdOf"), grabFn("snOf"), grabFn("qnFiniteC"), grabFn("qnOf"),
  "export { pct7, sdOf, snOf, qnOf, meanOf };"].join("\n");
const tmp = path.join(root, "tools", ".stats-fns.tmp.mjs");
fs.writeFileSync(tmp, code);
const { pct7, sdOf, snOf, qnOf, meanOf } = await import(tmp);
fs.unlinkSync(tmp);

const ref = JSON.parse(fs.readFileSync(path.join(root, "tools", "stats-vectors.json"), "utf8"));
const worst = {mean: 0, sd: 0, median: 0, q1: 0, q3: 0, sn: 0, qn: 0};
const rel = (a, b) => b === 0 ? Math.abs(a - b) : Math.abs(a - b) / Math.abs(b);
for (const r of ref) {
  const c = {mean: meanOf(r.x), sd: sdOf(r.x), median: pct7(r.x, 0.5), q1: pct7(r.x, 0.25), q3: pct7(r.x, 0.75), sn: snOf(r.x), qn: qnOf(r.x)};
  for (const k of Object.keys(worst)) worst[k] = Math.max(worst[k], rel(c[k], r[k]));
}
console.log("max rel diff vs R robustbase:", Object.entries(worst).map(([k, v]) => `${k}=${v.toExponential(2)}`).join(" "));
const ok = worst.mean < 1e-9 && worst.sd < 1e-9 && worst.median < 1e-9 && worst.q1 < 1e-9 && worst.q3 < 1e-9 && worst.sn < 1e-9 && worst.qn < 3e-6;
console.log(ok ? "WITHIN TOLERANCE" : "OUT OF TOLERANCE");
process.exit(ok ? 0 : 1);
