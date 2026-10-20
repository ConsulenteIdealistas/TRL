# Academic & Tech Digest

PubMed と arXiv の新着から TRL と投資妙味を評価し、スマホで読めるダイジェストを生成する。

本システムは公開情報の整理であり、投資助言ではない。

## 仕組み

- 毎日 JST 07:00 に GitHub Actions が実行される
- PubMed（バイオ）と arXiv（宇宙）の新着を収集する
- 分野別に上位N件（バイオ3・宇宙2）を選定する
- AI 入力ファイルを生成し、GitHub Pages で公開する
- `config/evaluator.yaml` の設定に応じて次のいずれかで動く
  - gemini: Gemini API 無料枠で自動評価し、評価済みダイジェストまで生成する
  - manual: 入力ファイルのみ生成し、AI 評価は Issue 貼り付けで手動実行する

GitHub Models は 2026-07-30 に完全廃止されたため使用しない。

## セットアップ

1. Public リポジトリを作成する
2. 全ファイルを GitHub Web UI で作成する
3. Settings → Pages → Source: Deploy from a branch → main / docs
4. Settings → Actions → General → Workflow permissions: Read and write permissions
5. Gemini を使う場合: Settings → Secrets → Actions → GEMINI_API_KEY を登録する
6. Actions → daily-collect → Run workflow で初回実行する

## AI 評価方式の切り替え

`config/evaluator.yaml` の `provider` を変更する。

- gemini: 自動評価。`GEMINI_API_KEY` が必要
- manual: 手動貼り付け。追加設定不要

`fallback_to_manual: true` の場合、Gemini が失敗しても入力ファイルは生成され、手動 Issue で補完できる。

## 日次運用

### gemini の場合

Pages で `YYYY-MM-DD.md` を開くだけでよい。

### manual の場合

1. Pages で `YYYY-MM-DD-input.md` を開き、全文をコピーする
2. Claude または ChatGPT に貼り付けて評価する
3. Issues → New issue → AI評価結果 を選ぶ
4. タイトルに `YYYY-MM-DD` を含める（例: `ai-eval 2026-10-20`）
5. 本文に AI の出力を貼り付けて投稿する
6. 数分後、`YYYY-MM-DD.md` が生成される

## ファイル構成

- README.md
- requirements.txt
- config/sources.yaml
- config/scoring.yaml
- config/evaluator.yaml
- prompts/eval_v1.md
- src/digest/__init__.py
- src/digest/sanitize.py
- src/digest/collectors.py
- src/digest/evaluator.py
- src/digest/pipeline.py
- src/digest/main.py
- .github/ISSUE_TEMPLATE/ai-eval-result.md
- .github/workflows/daily-collect.yml
- .github/workflows/issue-ingest.yml
- docs/index.md
- docs/.nojekyll
- docs/digests/.gitkeep

## 設定

- 情報源: `config/sources.yaml`
- スコア重み・キーワード・上位N: `config/scoring.yaml`
- AI 評価: `config/evaluator.yaml`
- プロンプト: `prompts/eval_v1.md`

## トラブルシューティング

- Actions が push できない: Workflow permissions を Read and write にする
- Pages が真っ白: `docs/.nojekyll` があるか確認する
- Gemini 評価が空になる: `GEMINI_API_KEY` と無料枠レート制限を確認する
- PubMed が 0 件: `config/sources.yaml` の `term` を緩和する
- arXiv が 0 件: `categories` を増やす
- Issue 取り込みが動かない: `ai-eval` ラベル、タイトルの `YYYY-MM-DD`、本文の有無を確認する

## セキュリティ

- Secrets に置くのは `GEMINI_API_KEY` のみ
- 個人の投資仮説・ウォッチリストは含めない（public リポジトリのため）
- AI 出力はサニタイズされる（生HTML禁止、リンクに rel 付与）

## 免責

本システムが生成するダイジェストは公開情報の整理であり、投資助言・勧誘を目的としない。投資判断は自己責任で行う。


