# TRL投資分析システム

技術・企業の**長期成長評価（long）**と、1〜6か月の**イベント評価（event）**を分離して扱う分析基盤です。
公開情報（論文・治験・ニュース・開示・財務）を収集し、LLM が根拠を抽出・採点して SQLite に保存、GitHub Pages で表示します。
根拠・出典・不足情報を必ず保存し、情報不足の候補は**保留（hold）**にして点数を出しません。
Python 標準ライブラリのみで動作し、運用はスマホのブラウザだけで完結します。

> スコアは評価尺度です。株価上昇確率・成功確率・投資利益率ではなく、売買の自動判定にも使えません。投資助言ではありません。

公開サイト: https://consulenteidealistas.github.io/TRL/ （画面と `data.json` は公開されます）

## 現在の規模（2026-10-29 時点）

- 対象企業: **300社**（コア 180 / ウォッチ 120）、16市場、生成クエリ 867件
- 分野別: バイオ 128 / 宇宙 29 / 半導体 40 / AI 30 / ロボティクス 31 / エネルギー・蓄電池 30 / 量子技術 12（ほかに分野を跨ぐタグ: フォトニクス 14 / 医療機器 25）
- 市場別: US 33 / JP 22 / GB 13 / FR 12 / DE 10 / AU 10 / CA 8 / CH 6 / DK 6 / ES 5 / SE 5 / NL 5 / IT 4 / FI 4 / NO 4 / BE 3
- 設定バージョン: `2026.10.0`（設定とプロンプトの内容ハッシュを評価ごとに記録し、変更の追跡はハッシュで行う）
- テスト: 174件（`test_trl.py` / `test_entities.py` / `test_site.py`、CI でも毎回実行）

## 全体像

```
収集 (ソース別に並列) → 正規化・重複排除 → 企業リンク付け → スクリーニング
  → 選定 (コア企業リンク・材料の強さ順, 週6件まで)
  → LLM抽出 (根拠・事実) → LLM評価 (long / event)
  → SQLite 保存 (dataブランチ) → サイト書き出し (data.json + web/) → 表示対象のみ翻訳
```

実行は GitHub Actions のワークフロー `.github/workflows/main.yaml` が担います（後述「実行と運用」）。

## 企業辞書（entities/）

企業は市場別ファイル（`entities/jp.json`, `us.json`, ... 全16ファイル）に1エントリずつ定義し、起動時に合体します。企業追加は該当ファイルへの1エントリ追加で完結します。

エントリの主な項目:

| 項目 | 内容 |
|---|---|
| `id` / `name` / `aliases` | 一意なID、正式名、別名（日本語名含む）。別名は3文字未満・他企業との衝突を検証で拒否 |
| `ticker` / `exchange` / `market` | 証券コード（接尾辞なし）、取引所、市場コード（US/GB/...、JP は省略可）。ticker の形式も検証する |
| `sector` | `bio` / `space` / `semiconductor` / `ai` / `robotics` / `energy` / `quantum`（主分類は1社1つ） |
| `tags` | 分野を跨ぐ第二の分類（複数可）。現在は `photonics`（半導体内の光通信）と `medtech`（バイオ内の医療機器）、および他分野の sector キー（例: 半導体寄りのロボティクス企業への `semiconductor`）を使う |
| `tier` | `core` / `watch`（後述） |
| `watch.news_name` | ニュース検索に使う代表名 |
| `watch.sponsor` | ClinicalTrials.gov のスポンサー名（ある企業のみ治験クエリを生成） |
| `watch.pubmed` | PubMed 所属検索文字列（ある企業のみ） |
| `watch.openalex` | OpenAlex 機関ID（登録済み14社のみ） |
| `watch.extra_keywords` | 製品名・技術名などの追加キーワード |
| `products` | 製品リンク用の製品名・別名（任意） |

**tier の意味**: 評価の対象は**コア企業にリンクする候補のみ**です。ウォッチ企業は収集と根拠プールには参加しますが、その企業だけに紐付く材料は評価されません。層の移動は `tier` の書き換えだけです。

**クエリの自動生成**: 企業クエリは `sources.json` には書かず、辞書の `sector` と `watch` から起動時に自動生成します（`trl.py queries` で確認可能）。

- `gnews`: 代表名 + セクター別キーワード集合（市場言語別: 英/独/伊/スウェーデン語の集合あり、他は英語集合）
- `prtimes`: 英日表記の引用符付き企業名（**JP のみ**）
- `trials`: `sponsor` がある企業のみ
- `yahoo`: `ticker` + 市場別接尾辞（GB=.L / FR=.PA / DE=.DE / CH=.SW / SE=.ST / DK=.CO / ES=.MC / NL=.AS / FI=.HE / NO=.OL / BE=.BR / IT=.MI / CA=.TO / AU=.AX、US・JP はなし）
- `edinet`: 証券コード（**JP のみ**）
- `pubmed`: `watch.pubmed` がある企業のみ所属検索。その他の論文はトピッククエリ（`sources.json` の `topics`、3件）で収集
- `openalex`: 機関IDがある企業のみ

市場別ルール: EDINET・PR TIMES は JP のみ、EDGAR は US のみ。財務数値の金額単位は報告通貨で表示します。

## 情報源と役割

| ソース | 内容 | 役割 |
|---|---|---|
| `pubmed` | 論文（所属検索＋トピック） | 評価対象 |
| `arxiv` | 論文（トピック） | 評価対象 |
| `openalex` | 企業所属の論文（抄録を復元） | 評価対象 |
| `trials` | ClinicalTrials.gov の治験（スポンサー検索） | 評価対象 |
| `gnews` | Google News RSS（企業名＋ビジネス語） | **プール専用**（評価対象にしない） |
| `prtimes` | PR TIMES の企業発表（JP のみ） | **プール専用** |
| `yahoo` | Yahoo Finance の財務（四半期ごとに1件） | **プール専用** |
| `edinet` | EDINET 開示・財務数値（JP のみ） | **プール専用** |
| `edgar` | SEC EDGAR の財務数値（XBRL、US のみ） | **プール専用** |

ソース別の取得期間（lookback）: trials 180日 / yahoo・edgar 400日 / prtimes 90日 / pubmed・openalex 365日 / edinet 120日。その他は既定30日・最大20件。

### 収集と候補管理

- **重複排除**は安定IDのみで行い、タイトル類似では統合しません。キーは DOI / PMID / arXiv ID / URL。例外として Yahoo・EDGAR は URL が銘柄・企業ページ（四半期で不変）のため、四半期ごとの `source_id` をキーにします（Yahoo は市場接尾辞つきシンボル-期間。接尾辞なしだと市場違いの同名ティッカーで衝突するため）。
- ニュースは企業に紐付かないものを収集段階で捨て、転載・定型ページ・値動きまとめを媒体/タイトルフィルタで落とします。
- 候補は抄録の最低文字数（通常200字、news/disclosure は30字）と最大経過日数（365日）でスクリーニングします。
- 基準日（`--as-of`）より後の資料は評価対象外です。

### 根拠プール（評価への同梱）

ニュースと財務は単体では評価せず、同一企業の論文・治験を評価するときに根拠としてプロンプトへ同梱します。

- **関連事業ニュース**: 同一企業（製品リンク経由を含む）の直近90日・最大8件。出典の種別（origin=企業発表 `prtimes` / 報道 `gnews`）を付け、両方で確認できる事実は信頼度を高く、企業発表のみの事実は割り引くようプロンプトで指示します。
- **関連財務根拠**: 財務数値を含む開示のみ、直近400日・最大2件。論文・治験の抄録に存在しない売上・利益・現預金などの公表数値が `financial_sustainability` / `monetization` の根拠になります。実働ソースは Yahoo Finance・EDINET・SEC EDGAR です。
- Google News の記事 URL は直接開けない仕様のため、サイトでは記事タイトルをタイトル検索リンクに、媒体名を媒体サイトへのリンクにして表示します。

## 評価仕様

### long（長期成長評価）: 6項目・100点

| 項目 | 配点 | 軸 |
|---|---|---|
| technology_readiness | 25 | 技術軸 |
| technological_advantage | 15 | 技術軸 |
| monetization | 25 | 事業軸 |
| market_growth | 15 | 事業軸 |
| financial_sustainability | 10 | 事業軸 |
| moat_durability | 10 | 事業軸 |

必須項目は technology_readiness と monetization です。

- **総合点は6項目すべてが非nullのときのみ確定**します。1つでも null なら hold で、重みの再配分はしません。
- **例外（二軸化）**: `trl_level` が確定し **3以下**のとき、事業軸は「段階未達（pre_stage）」として不足に数えず、技術軸の2項目が揃えば技術軸の総合点（`total_scope=technology`）で final にします。前商業段階では事業項目の「存在しない」を「不足」と同一視しないための仕様です。
- 各項目には根拠（evidence）・理由が必須で、不足がある場合は不足情報（missing_info）の記録が必須です。

### trl_level と採点基準の3原則

`trl_level`（1〜9 または未確定）は technology_readiness の点数から換算せず、専用項目として判定させます。セクター別（バイオ/AI/半導体/宇宙）の詳細基準をプロンプト `long.v1.md` に埋め込んでいます。

1. **商用展開と TRL の分離**: TRL は「実環境で意図通り動作するかの実証段階」のみで判定します。販売・課金・顧客数・売上・量産規模は TRL の根拠にせず、事業軸（monetization）で扱います。
2. **「複数」「継続」の扱い**: 件数・期間は資料に明記された分のみを使い、推測で補いません。TRL 9 には繰り返し・継続の証拠が必須です。
3. **TRL と成長余地の分離**: TRL の高低から収益化・市場成長・競争優位性を推論しません（事業軸で別途評価します）。

### 材料の方向性（direction）

long 評価では、点数・TRL とは独立に「その資料が示す出来事の方向」を positive / negative / mixed で理由つきに判定します。治験中止のように全項目に点が付いても逆風の材料を誤読しないためで、サイトのカードにバッジ表示し、一覧で絞り込みできます。

### 不足情報の扱い

- 資料群に原理的に存在しないもの（第三者の市場統計・競合の定量比較・特許明細・将来ガイダンス）は不足として数えません。market_growth は患者規模などの代理指標で、財務は既存数値で評価します。
- 時間待ちの不足（治験結果の公表待ちなど）は接頭辞「結果待ち：」で区別し、サイトでは別掲します。
- 治験レジストリ側で要旨が更新された候補は `material_updated_at` を記録し、再評価の間隔を待たずに優先再評価します。

### event（イベント評価）: 6項目・100点

| 項目 | 配点 |
|---|---|
| catalyst_quality | 25 |
| expectation_gap | 25 |
| not_priced_in | 20 |
| timing_feasibility | 10 |
| supply_demand | 10 |
| downside_resilience | 10 |

長期とイベントは別々に評価・保存し、平均しません。市場予想が確認できないイベントは分類を確定せず、expectation_gap / not_priced_in を null に戻して保留にします。現在の材料には株価・出来高データが含まれないため、event は final が出にくく、主に短期視点のメモとして機能します。

### スコアのバンド

| 点数 | バンド | 意味 |
|---|---|---|
| 80–100 | very_strong | 非常に強い |
| 60–79 | strong | 強い |
| 40–59 | moderate_uncertain | 中程度・不確実 |
| 20–39 | weak | 弱い |
| 0–19 | extremely_weak | 極めて弱い |

### 選定と再評価

- 選定の優先順位: 企業リンクの強さ（strong > weak）> 実証シグナル > 新しさ。企業あたり上限10件、1回の評価は最大6件、うち論文枠2件を確保します。
- 評価は3件並列（結果の保存と打ち切り判定は候補順）。評価フェーズには時間予算600秒があります。
- hold の再評価は14日後から。ただし材料の更新が前回評価より新しい候補は間隔をバイパスします。再試行は最大3回まで。
- **未評価のみモード**（`--only-new`）: 一度でも評価された候補を除外し、未評価の候補だけを対象にします。
- LLM API の失敗（429・5xx・タイムアウト・認証エラー等）は評価結果として保存せず、候補を未評価のまま残します。連続2回失敗したら評価フェーズを打ち切ります。内容起因の失敗（不正な JSON 等）は error として保存します。旧版が API 失敗をエラーとして保存した候補は、画面・レポートのどちらにも表示しません。
- 抽出結果は入力ハッシュ（基準日を含む）が同じなら再利用します。

### LLM 設定

- プロバイダは `gemini` / `anthropic` / 未設定（収集のみ）。呼び出しはタイムアウト120秒、再試行3回（指数バックオフ 5→15→30→60秒、Retry-After 優先）。
- 主モデルが一時的な失敗（503/429 等）を返したときだけ、代替モデル（`TRL_LLM_FALLBACK_MODEL`）で1回だけ再試行します。モデルが廃止・変更されたときは、コードを触らず Variable の書き換えだけで切り替えられます。
- プロンプト: `extract.v1.md`（抽出）/ `long.v1.md`（長期）/ `event.v1.md`（イベント）。

## サイト（web/）

- 書き出し対象は「評価済み全件＋評価キュー上位10件」。データ形式は `data.json`（schema_version 3）。
- 一覧: 検索（2文字以上で企業名・キーワード・タイトルの**サジェスト**、日本語入力・スマホ対応）、国・市場と分野とタグの絞り込み、方向性フィルタ、並び替え（おすすめ順 / TRL 高い順・低い順 / 国・市場別 / 分野別 / 日付）。
- 表示状態: 選定可能（final）/ 要追加調査（review）/ 保留（hold）/ 評価待ち（pending）/ エラー / 除外。画面側で点数の再計算・換算・平均はしません。
- 詳細ページ（`detail.html`）: 根拠の全文、反証、不足情報と「結果待ち」の別掲、イベント評価、同梱した関連ニュース・財務根拠を表示します。
- 外部由来のテキストは全てエスケープして描画し、リンクは http/https のみ許可します。スコアは数値以外を表示しません。

### 翻訳

タイトル・抄録の日本語訳は表示対象のみ作成して DB に保存し（1回訳せば再利用）、画面で日本語訳/原文を切り替えます（未訳のものは原文のまま表示）。翻訳は専用 API のみで行い、評価用 LLM には任せません。

1. **Azure Translator**（本命: 無料枠 F0 が月200万字・毎月リセット）
2. **DeepL API**（合計100万字の一回限りの枠のため温存）
3. **Google Cloud Translation**（月50万字まで無料・毎月リセット、二重障害時のみ）

キーが無い段は自動で飛ばします。どの経路で訳したかは候補ごとに記録され、画面にも「訳: Azure」のように表示されます。

翻訳に使った文字数は段ごとに数えて run 記録に残します（`translation_chars`）。累計（Azure / Google は月次、DeepL は一回限り枠のため通算）が 50万 / 100万 / 125万 / 150万 / 175万 / 200万字のしきい値を跨ぐと、run 記録に `translation_alerts` として残し、実行ログにも警告を出します。

## 実行と運用

### ワークフロー（.github/workflows/main.yaml）

- 定期実行: **毎週火曜 06:30 JST**（cron `30 21 * * 1` UTC）。手動実行も可。同時実行は1本のみ、タイムアウト30分。
- 処理の流れ: テスト → data ブランチから DB 復元（`--depth 1` で取得）→（任意で purge）→ パイプライン実行 → サイト書き出し → DB を data ブランチへ保存 → GitHub Pages へデプロイ。
- DB は `data` ブランチの `trl.sqlite`（ルート直下）に保存され、実行ごとに通常のコミットで履歴が残ります。レポートは `data` ブランチの `reports/latest.md` と Actions の Summary でも読めます。

手動実行の入力:

| 入力 | 既定 | 内容 |
|---|---|---|
| sources | 全ソース | 収集するソースの組み合わせ |
| kinds | long | 評価種別（long / event / long,event） |
| max_eval | 6 | 1回の評価上限 |
| retry_failed | no | 失敗した評価の再試行（no/yes/force） |
| purge_evaluations | no | yes で評価とキューを全削除し候補を未評価に戻す（削除→再評価→再出力が1回の実行で完結） |
| only_new | no | yes で未評価の候補のみ評価 |

> **コードを修正した後は「Re-run jobs」ではなく「Run workflow」から新規に実行すること。** Re-run は元の実行時点のコミットのまま動くため、修正が反映されず、設定ハッシュも古いままになります。新規実行では `[run]` 行の設定ハッシュが最新の設定と一致するかで反映を確認できます。

### Secrets / Variables（Settings → Secrets and variables → Actions）

| 名前 | 種別 | 用途 |
|---|---|---|
| TRL_LLM_PROVIDER | Variable | `gemini` または `anthropic` |
| TRL_LLM_MODEL | Variable | モデル名（未設定時のコード既定は Gemini `gemini-3.8-flash`） |
| TRL_LLM_FALLBACK_MODEL | Variable | 代替モデル名（任意） |
| GEMINI_API_KEY / ANTHROPIC_API_KEY | Secret | LLM のキー |
| OPENALEX_API_KEY | Secret | OpenAlex（推奨） |
| NCBI_API_KEY | Secret | PubMed（任意、レート上限が上がる） |
| AZURE_TRANSLATOR_KEY | Secret | 翻訳・第1段 |
| AZURE_TRANSLATOR_REGION | Variable | 例: `japanwest` |
| DEEPL_API_KEY | Secret | 翻訳・第2段 |
| GOOGLE_TRANSLATE_API_KEY | Secret | 翻訳・第3段 |
| EDINET_API_KEY | Secret | EDINET（設定済み・運用中） |
| TRL_EDGAR_CONTACT | Variable | SEC EDGAR の User-Agent に使う連絡先メール（任意。未設定でも SEC の例示形式で動作する） |

### セットアップ（初回のみ）

1. リポジトリを作成し、Settings → Actions → General → Workflow permissions を **Read and write** にする。
2. Settings → Pages → Source を **GitHub Actions** にする。
3. 上記の Secrets / Variables を設定する。
4. Actions タブ → TRL pipeline → Run workflow。

## CLI

```
python3 trl.py run           # 収集+評価+保存 (--sources/--kinds/--max-eval/--retry-failed/--only-new/--skip-collect/--as-of/--llm/--model)
python3 trl.py queue         # レビューキューの表示
python3 trl.py export        # サイト書き出し (data.json + web/, --out で出力先変更)
python3 trl.py queries       # 生成クエリの一覧
python3 trl.py check-config  # 設定の検証とハッシュ表示
python3 trl.py purge --evaluations  # 評価とキューを全削除し候補を未評価に戻す (候補・収集物・実行履歴は残る)
python3 -m unittest test_trl test_entities test_site   # テスト
```

## ファイル構成

```
trl.py                  CLI 入口
trlcore/                本体パッケージ
  config.py             設定読み込み・検証 (scoring.json / sources.json / entities)
  entities.py           企業辞書の検証・企業リンク付け
  pipeline.py           収集→選定→評価→書き出しの統括
  store.py              SQLite (候補・評価・識別子・実行記録)
  normalize.py          正規化と重複排除キー
  sources/              各ソースのフェッチャ (pubmed/arxiv/gnews/prtimes/trials/yahoo/openalex/edinet)
  evaluate.py           LLM 応答の検証・評価の実行
  scoring.py            スコア計算 (二軸化・バンド)
  llm.py                LLM プロバイダ (gemini/anthropic, 代替モデル)
  translate.py          翻訳 (Azure→DeepL→Google)
  site.py               data.json 書き出し
  report.py             実行レポート
  http.py               HTTP 取得 (リトライ・秘密情報マスク・応答32MB上限)
entities/               市場別企業辞書 (16ファイル)
scoring.json            配点・必須項目・バンド・スクリーニング・LLM 設定
sources.json            キーワード集合・生成パラメータ・トピック
extract.v1.md / long.v1.md / event.v1.md   プロンプト
web/                    画面 (index.html / detail.html / app.js / style.css / schema.json)
test_trl.py / test_entities.py / test_site.py   テスト
.github/workflows/main.yaml   ワークフロー
```

## 既知の制約・設計判断

- **EDINET は運用中**: JP 企業の開示を取得し、財務書類（有報・四半期・半期とその訂正）からは各社直近2件まで数値を抽出して財務プールに入れます（type=5 の CSV を優先し、取れなければ type=1 の XBRL）。キー未設定の環境では取得を自動でスキップし、エラーにしません。
- **Yahoo Finance は非公式 API**（キー・登録は不要）。bot 判定を避けるため User-Agent はセッションごとに実在ブラウザの候補から選びます。銘柄自体が無い企業（例: QPS研究所）は取得できません。取得失敗はログに残りますが、その企業の財務プールは空になります。
- **SEC EDGAR は公式 API**（キー・登録は不要、US 上場のみ）。company facts（XBRL）から売上・利益・現預金・短期有価証券などを抽出し、Yahoo と同じ財務プールに入れます。2026-10-29 の実測では US 94社中87社で取得でき、残り（上場直後で未提出・IFRS の集計概念が無い等）は Yahoo 側でカバーします。SEC の fair access 方針に従い User-Agent に運用者名と連絡先を含め、リクエストを間引きします。
- **arXiv は所属情報を取得しない**ため、企業との strong 一致は成立しません（現時点では対象外として扱います）。
- **OpenAlex は機関ID登録済みの14社のみ**が対象です。機関レコードが無い企業は PubMed/arXiv 側で拾います。
- **event 評価の限界**: 材料に株価・出来高が無いため、織り込み度の判定ができず final は出にくい状態です。本格化には株価履歴の材料追加が必要です。
- **TDnet は見送り済み**: 公式 API は索引のみで高額、本文は PDF 中心で現在の構成（標準ライブラリのみ）では抽出できないため採用していません。
- **未実装**: 特許・IR 資料の本文解析、市場データの取り込み、過去イベントの事後検証、AI 採点の妥当性検証。
- 失敗した情報源は Actions ログの `::warning::` とレポートに表示されます。全情報源が失敗したときだけジョブが失敗します。
