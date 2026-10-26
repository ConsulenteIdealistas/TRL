# TRL投資分析システム v0.1.0

技術・企業の**長期成長評価**と、1〜6か月の**イベント評価**を分離して扱う分析基盤です。
根拠・出典・不足情報を保存し、情報不足の候補は**保留(hold)**してレビューキューに入れます。
Python標準ライブラリのみ。スマホのブラウザだけで運用できます。結果は GitHub Pages の画面(`web/`)で見られます。

> スコアは評価尺度です。株価上昇確率・成功確率・投資利益率ではなく、売買の自動判定にも使えません。

## ファイル構成
`trl.py` 本体 / `test_trl.py` `test_entities.py` `test_site.py` テスト / `scoring.json` 配点・設定 / `sources.json` 収集設定(キーワード集合・トピック) / `entities.json` 企業辞書 /
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
- 任意: Variable `TRL_LLM_FALLBACK_MODEL`(代替モデル名)。主モデルが 503/429 等の一時的失敗を返したときだけ、同じプロバイダの代替モデルで1回だけ再試行する。404/401等の恒常的な失敗では切り替えない。未設定なら無効。
- モデルが廃止・変更されたときは、コードを触らず Variable `TRL_LLM_MODEL` を書き換えるだけで切り替えられます。
- 一時的失敗への再試行は `llm.retries`(既定3回、1回の呼び出しで最大4試行)。待機は指数バックオフ(5秒→15秒→30秒→60秒上限、ジッター付き)で、`Retry-After` があればそちらを優先する。1回の呼び出しのタイムアウトは `llm.timeout_sec`(既定120秒)。過負荷時は応答自体が遅くなるため、60秒では正常な応答も切り捨てやすい。
- **コードを修正した後は、Actions の「Re-run jobs」ではなく「Run workflow」から新規に実行すること。** Re-run は元の実行時点のコミットのまま動くため、修正が反映されず、設定ハッシュも古いままになる。新規実行では `[run]` 行の設定ハッシュが最新の設定と一致するかで反映を確認できる。

Gemini 3.8 Flash は従量課金の料金表がある一方、無料枠の有無・上限は未確認です(Google AI Studioで確認してください)。Anthropic APIは従量課金です。1回の評価件数は `max_eval`(既定3)で制限されます。収集はソース単位で並列取得し、評価は候補を3件ずつ並列に処理します(結果の保存と、API失敗が続いた場合の打ち切り判定は候補順に行い、無駄な呼び出しはバッチの残り分までに抑えます)。評価フェーズには時間予算(既定600秒)も設けて長時間化を抑えます。サイトの一覧で「詳細を見る」を押すと候補ごとの詳細ページ(`detail.html`)に移動し、根拠の全文・イベント評価・同梱した関連ニュースを確認できます。

## 設計メモ
- 総合点は6項目すべてが非nullのときのみ確定。1つでもnullなら hold(重みの再配分はしない)。
- 長期とイベントは別々に評価・保存し、平均しない。
- 市場予想が確認できないイベントは、分類を確定せず乖離・織り込み度を保留にする。
- 重複排除は DOI / PMID / arXiv ID / URL のみ。タイトル類似では統合しない。
- 基準日(`--as-of`)より後の資料は評価対象外。
- プロトタイプの間は版番号(`config_version`・プロンプトの `v1`・本体のバージョン)を上げない。設定とプロンプトの内容ハッシュは評価ごとに記録されるので、変更の追跡はハッシュで行う。
- `data` ブランチは実行ごとに通常のコミットで追記される(DBの過去版が残る。取得は `--depth 1` で、履歴が増えても実行時間は伸びない)。
- LLM APIの失敗(429・5xx・タイムアウト・認証/モデル名の誤り)は評価結果として保存せず、候補を未評価のまま残す。連続して失敗したら評価フェーズを打ち切る(レポートと画面に表示)。内容起因の失敗(不正なJSONなど)は従来どおり error として保存する。旧版が API失敗を評価エラーとして保存したレビューキューは、画面・レポートのどちらでも表示しない。
- 抽出結果は同じ入力(`input_hash`=基準日を含む)なら再利用する。採点だけが失敗した場合に、同じ基準日で再実行すると抽出をやり直さない。
- 画面の状態: 選定可能=final / 要追加調査=必須項目は揃い他が不足 / 保留=必須項目が不足 / 未評価 / エラー / 除外。画面側で点数の再計算・換算・平均はしない。
- `trl_level`(1〜9またはnull)は `long.v1.md` で別項目として出力させる。`technology_readiness`(0〜100)からは換算しない。
- arXiv は所属情報を取得しないため、企業との `strong` 一致は成立しない(現時点では対象外として扱う)。
- 情報源は PubMed / arXiv(論文)に加え、Google News RSS(`gnews`、企業名+ビジネス語クエリ)、ClinicalTrials.gov(`trials`、スポンサー名クエリ。published は最終更新日)、EDINET(`edinet`、証券コードクエリ。開示書類のメタデータ)を利用できる。論文以外の短文ソース(news / disclosure)は `screening.min_abstract_chars_by_info_type` で最低文字数を別に定める。
- ニュースは評価単位にしない。企業に紐付かないニュースは収集段階で捨て、転載・定型ページ・値動きまとめは媒体/タイトルフィルタで落とす。残ったニュースは「関連事業ニュース」として、同一企業(製品リンク経由を含む)の論文・治験・開示を評価するときにプロンプトへ同梱する補助根拠であり、サイトの候補一覧にも表示しない。
- ニュースプールは報道(`gnews`)と企業発表(`prtimes`、PR TIMES の公式発表を site: 指定で収集)の二系統。同梱時に origin(企業発表/報道)を付け、同じ事実が両方で確認できる場合を信頼度の高い根拠として扱う擦り合わせを行う。
- 企業クエリは sources.json には書かず、entities.json の `sector` と `watch`(news_name / prtimes_terms / sponsor / extra_keywords)から起動時に自動生成する(gnews=代表名+セクター別キーワード集合、trials=sponsor がある企業のみ、edinet=ticker がある企業のみ、prtimes=英日表記)。sources.json にはキーワード集合・生成パラメータ(entity_watch)とトピッククエリ(topics)だけを置く。生成結果は `trl.py queries` で確認できる。企業追加は entities.json の1エントリで完結する。
- EDINET は Secret `EDINET_API_KEY` が必要。未設定のときは取得を自動でスキップする(エラーにしない)。現行はメタデータのみで、XBRL の財務数値抽出は未実装。
- 失敗した情報源はログの `::warning::` とレポートに表示。全情報源が失敗したときだけジョブが失敗。
- 評価基準を大きく変えたときは `trl.py purge --evaluations` で評価とレビューキューを全削除し、候補を未評価に戻せる(候補・収集物・実行履歴は残る)。ワークフローの手動実行で purge_evaluations=yes を選ぶと、削除→再評価→サイト再出力が一回の実行で完結する。

## 実装状況
**テスト済み(自動テスト `test_trl` 52件 + `test_entities` 10件 + `test_site`)**: 設定検証、採点・hold・スコア帯、日付/鮮度、正規化、重複排除、保存/再読込、レビューキュー、プロンプト差し込み、不正AI応答の検出、HTTPリトライと秘密情報マスク、PubMed/arXivのXML解析(固定サンプル)、収集→評価→保存の統合(モックAI)。

**未検証**: 実際のPubMed/arXiv/AI API、GitHub Actions上の実行、`data`ブランチへの保存。

**未実装**: 特許・IR資料の本文解析・市場データ、EDINETのXBRL財務数値抽出、需給分析、TRL9相当からの成長余地探索、過去イベントの事後検証、AI採点の妥当性検証。

## CLI
```
python -m unittest test_trl test_entities test_site -v
python trl.py run --data-dir data --sources arxiv --llm gemini
python trl.py export --data-dir data --out _site   # data.json と web/ を _site に出力
python trl.py queue --data-dir data
```
