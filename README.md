# TRL投資分析システム v0.1.0

技術・企業の**長期成長評価**と、1〜6か月の**イベント評価**を分離して扱う分析基盤です。
根拠・出典・不足情報を保存し、情報不足の候補は**保留(hold)**してレビューキューに入れます。
Python標準ライブラリのみ。スマホのブラウザだけで運用できます。結果は GitHub Pages の画面(`web/`)で見られます。

> スコアは評価尺度です。株価上昇確率・成功確率・投資利益率ではなく、売買の自動判定にも使えません。

## ファイル構成(すべてリポジトリの直下・フォルダなし)
`trl.py` 本体 / `test_trl.py` `test_entities.py` `test_site.py` テスト / `scoring.json` 配点・設定 / `sources.json` 検索クエリ / `entities.json` 企業辞書 /
`extract.v1.md` `long.v1.md` `event.v1.md` プロンプト / `README.md` / `.github/workflows/main.yml`
`web/`(例外: フォルダあり)= 画面。`index.html` `app.js` `style.css` と、Python⇔Web の契約 `schema.json`

## スマホ(Safari)でのセットアップ
1. GitHubで新規リポジトリを作成(Private推奨)。
2. Settings → Actions → General → Workflow permissions を **Read and write** にして保存。
3. Add file → Create new file で、上記ファイルを1つずつ作成(ファイル名を入力し、中身を貼り付けて Commit)。
   `main.yaml` だけはファイル名欄に `.github/workflows/main.yaml` と入力。
4. Settings → Pages → Source を **GitHub Actions** にする(初回のみ)。
5. Actions タブ → TRL pipeline → Run workflow。

実行が成功すると、画面が GitHub Pages に配備されます(`https://<ユーザー名>.github.io/<リポジトリ名>/`)。結果は画面のほか、実行ページの Summary と `data` ブランチの `reports/latest.md` でも読めます。**Pages の画面と `data.json` は公開されます**(評価内容も公開される前提です)。評価結果とレビューキューは `data` ブランチの `trl.sqlite` に保存され、次回実行に引き継がれます。

## AI評価(任意・既定はオフ)
LLM未設定のときは収集・重複排除・スクリーニングのみ行い、評価はスキップします(課金回避)。
Settings → Secrets and variables → Actions で設定:
- Variable `TRL_LLM_PROVIDER` = `gemini` または `anthropic`
- Secret `GEMINI_API_KEY` または `ANTHROPIC_API_KEY`
- LLM呼び出しの設定は `scoring.json` の `llm`(`timeout_sec` `retries` `max_consecutive_failures` `phase_budget_sec`)。
- 任意: Variable `TRL_LLM_MODEL`(モデル名。未設定なら Gemini は `gemini-3.8-flash`)、Secret `NCBI_API_KEY`
- モデルが廃止・変更されたときは、コードを触らず Variable `TRL_LLM_MODEL` を書き換えるだけで切り替えられます。

Gemini 3.8 Flash は従量課金の料金表がある一方、無料枠の有無・上限は未確認です(Google AI Studioで確認してください)。Anthropic APIは従量課金です。1回の評価件数は `max_eval`(既定10)で制限されます。

## 設計メモ
- 総合点は6項目すべてが非nullのときのみ確定。1つでもnullなら hold(重みの再配分はしない)。
- 長期とイベントは別々に評価・保存し、平均しない。
- 市場予想が確認できないイベントは、分類を確定せず乖離・織り込み度を保留にする。
- 重複排除は DOI / PMID / arXiv ID / URL のみ。タイトル類似では統合しない。
- 基準日(`--as-of`)より後の資料は評価対象外。
- プロトタイプの間は版番号(`config_version`・プロンプトの `v1`・本体のバージョン)を上げない。設定とプロンプトの内容ハッシュは評価ごとに記録されるので、変更の追跡はハッシュで行う。
- `data` ブランチは実行ごとに通常のコミットで追記される(DBの過去版が残る。取得は `--depth 1` で、履歴が増えても実行時間は伸びない)。
- LLM APIの失敗(429・5xx・タイムアウト・認証/モデル名の誤り)は評価結果として保存せず、候補を未評価のまま残す。連続して失敗したら評価フェーズを打ち切る(レポートと画面に表示)。内容起因の失敗(不正なJSONなど)は従来どおり error として保存する。
- 抽出結果は同じ入力(`input_hash`=基準日を含む)なら再利用する。採点だけが失敗した場合に、同じ基準日で再実行すると抽出をやり直さない。
- 画面の状態: 選定可能=final / 要追加調査=必須項目は揃い他が不足 / 保留=必須項目が不足 / 未評価 / エラー / 除外。画面側で点数の再計算・換算・平均はしない。
- `trl_level`(1〜9またはnull)は `long.v1.md` で別項目として出力させる。`technology_readiness`(0〜100)からは換算しない。
- arXiv は所属情報を取得しないため、企業との `strong` 一致は成立しない(現時点では対象外として扱う)。
- 失敗した情報源はログの `::warning::` とレポートに表示。全情報源が失敗したときだけジョブが失敗。

## 実装状況
**テスト済み(自動テスト `test_trl` 52件 + `test_entities` 10件 + `test_site`)**: 設定検証、採点・hold・スコア帯、日付/鮮度、正規化、重複排除、保存/再読込、レビューキュー、プロンプト差し込み、不正AI応答の検出、HTTPリトライと秘密情報マスク、PubMed/arXivのXML解析(固定サンプル)、収集→評価→保存の統合(モックAI)。

**未検証**: 実際のPubMed/arXiv/AI API、GitHub Actions上の実行、`data`ブランチへの保存。

**未実装**: 臨床試験・特許・IR・市場データ、需給分析、TRL9相当からの成長余地探索、過去イベントの事後検証、AI採点の妥当性検証。

## CLI
```
python -m unittest test_trl test_entities test_site -v
python trl.py run --data-dir data --sources arxiv --llm gemini
python trl.py export --data-dir data --out _site   # data.json と web/ を _site に出力
python trl.py queue --data-dir data
```
