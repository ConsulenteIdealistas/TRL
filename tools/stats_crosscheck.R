# 統計量の golden ベクトル生成 (参照実装は R robustbase)。
#
# web/app.js の統計関数 (平均・中央値・標本SD・四分位数 type 7・Sn・Qn) は
# 表示層の決定的導出であり、その数値が参照実装と一致することをここで
# 固定する。使い方 (リポジトリのルートから):
#   Rscript tools/stats_crosscheck.R   # tools/stats-vectors.json を再生成
#   node tools/check_stats.mjs         # app.js の実物と照合する
#
# Qn の残差について: robustbase の Qn は定数を 2.21914 に切り捨てるため、
# 精密な定数 2.21914446598508 を使う JS 側とは相対差 2.1e-6 程度が残る。
# これは係数や順序統計量の差ではない (2026-11-24 の掃引照合で確認)。
suppressMessages(library(robustbase))
set.seed(20261124)
out <- list()
add <- function(x) {
  out[[length(out) + 1]] <<- list(x = x, mean = mean(x), sd = sd(x),
    median = median(x), q1 = unname(quantile(x, 0.25)), q3 = unname(quantile(x, 0.75)),
    sn = Sn(x), qn = Qn(x))
}
for (n in c(2:20, 25, 30, 40, 50, 60)) add(round(rnorm(n, mean = 55, sd = 18), 1))
add(1:5)
add(c(2, 3, 3, 5, 7, 7, 7, 11, 13, 101))
add(rep(7, 9))
add(c(10, 10, 20, 20, 30, 40, 40, 40, 90, 90, 90, 90))
fmt <- function(v) paste(format(v, digits = 12, scientific = FALSE, trim = TRUE), collapse = ",")
lines <- sapply(out, function(r) sprintf('{"x":[%s],"mean":%s,"sd":%s,"median":%s,"q1":%s,"q3":%s,"sn":%s,"qn":%s}',
  fmt(r$x), fmt(r$mean), fmt(r$sd), fmt(r$median), fmt(r$q1), fmt(r$q3), fmt(r$sn), fmt(r$qn)))
cat("[", paste(lines, collapse = ",\n"), "]\n", file = "tools/stats-vectors.json")
cat("wrote", length(out), "vectors to tools/stats-vectors.json\n")
