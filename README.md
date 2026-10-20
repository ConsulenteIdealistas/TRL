# TRL投資分析システム v0.1.0

技術・企業の**長期成長評価**と、1〜6か月の**イベント評価**を分離して扱う分析基盤です。
根拠・出典・不足情報を保存し、情報不足の候補は**保留(hold)**してレビューキューに入れます。
Python標準ライブラリのみ。スマホのブラウザだけで運用できます。

> スコアは評価尺度です。株価上昇確率・成功確率・投資利益率ではなく、売買の自動判定にも使えません。

## ファイル構成(すべてリポジトリの直下・フォルダなし)
`trl.py` 本体 / `test_trl.py` テスト / `scoring.json` 配点・設定 / `sources.json` 検索クエリ /
`extract.v1.md` `long.v1.md` `event.v1.md` プロンプト / `README.md` / `.github/workflows/main.yml`

## スマホ(Safari)でのセットアップ
1. GitHubで新規リポジトリを作成(Private推奨)。
2. Settings → Actions → General → Workflow permissions を **Read and write** にして保存。
3. Add file → Create new file で、上記ファイルを1つずつ作成(ファイル名を入力し、中身を貼り付けて Commit)。
   `main.yml` だけはファイル名欄に `.github/workflows/main.yml` と入力。
4. Actions タブ → TRL pipeline → Run workflow。

結果は実行ページの Summary と、`data` ブランチの `reports/latest.md` で読めます。評価結果とレビューキューは `data` ブランチの `trl.sqlite` に保存され、次回実行に引き継がれます。

## AI評価(任意・既定はオフ)
LLM未設定のときは収集・重複排除・スクリーニングのみ行い、評価はスキップします(課金回避)。
Settings → Secrets and variables → Actions で設定:
- Variable `TRL_LLM_PROVIDER` = `gemini` または `anthropic`
- Secret `GEMINI_API_KEY` または `ANTHROPIC_API_KEY`
- 任意: Variable `TRL_LLM_MODEL`、Secret `NCBI_API_KEY`

Geminiの無料枠の条件は未確認です。Anthropic APIは従量課金です。1回の評価件数は `max_eval`(既定10)で制限されます。

## 設計メモ
- 総合点は6項目すべてが非nullのときのみ確定。1つでもnullなら hold(重みの再配分はしない)。
- 長期とイベントは別々に評価・保存し、平均しない。
- 市場予想が確認できないイベントは、分類を確定せず乖離・織り込み度を保留にする。
- 重複排除は DOI / PMID / arXiv ID / URL のみ。タイトル類似では統合しない。
- 基準日(`--as-of`)より後の資料は評価対象外。
- `scoring.json` を変えたら `config_version` を更新(実行・評価ごとに版とハッシュを記録)。プロンプトは新しい版ファイル(`long.v2.md`)を作り `scoring.json` の `prompts` を切り替える。
- `data` ブランチは単一コミットで強制上書き(履歴肥大を避ける代わりにDBの過去版は残らない)。
- 失敗した情報源はログの `::warning::` とレポートに表示。全情報源が失敗したときだけジョブが失敗。

## 実装状況
**テスト済み(自動テスト45件)**: 設定検証、採点・hold・スコア帯、日付/鮮度、正規化、重複排除、保存/再読込、レビューキュー、プロンプト差し込み、不正AI応答の検出、HTTPリトライと秘密情報マスク、PubMed/arXivのXML解析(固定サンプル)、収集→評価→保存の統合(モックAI)。

**未検証**: 実際のPubMed/arXiv/AI API、GitHub Actions上の実行、`data`ブランチへの保存。

**未実装**: 臨床試験・特許・IR・市場データ、需給分析、TRL9相当からの成長余地探索、過去イベントの事後検証、AI採点の妥当性検証。

## CLI
```
python -m unittest test_trl -v
python trl.py run --data-dir data --sources arxiv --llm gemini
python trl.py queue --data-dir data
```