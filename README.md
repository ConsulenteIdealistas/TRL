# Academic & Tech Digest（最小構成）

PubMed / arXiv の新着情報から、技術成熟度（TRL）と投資妙味を評価し、
スマホで読めるダイジェストを生成するシステムです。

**Claude Code 不要・PC 不要**。GitHub Web UI だけで構築・運用できます。
AI 評価は **Gemini API 無料枠で自動化**でき、**手動貼り付け方式への切り替え**も可能です。

> 本システムは公開情報の整理であり、投資助言ではありません。

---

## 1. これは何か

- 毎日 JST 07:00 に GitHub Actions が PubMed（バイオ）と arXiv（宇宙）の新着を収集
- 機械的スコアで分野別に上位5件（バイオ3・宇宙2）を選定
- AI 評価用の入力ファイルを自動生成し、GitHub Pages で公開
- `config/evaluator.yaml` の設定に応じて：
  - `gemini`: Gemini API 無料枠で自動評価し、評価済みダイジェストまで生成
  - `manual`: 入力ファイルのみ生成。AI 評価は Issue 貼り付けで手動実行

## 2. 特徴

| 特徴 | 内容 |
|---|---|
| ゼロコスト | 無料 API + GitHub Actions 無料枠 + Pages |
| スマホ完結 | 構築・運用・閲覧すべてブラウザで可能 |
| 自動/手動切替 | `config/evaluator.yaml` の `provider` で切替 |
| 著作権配慮 | 有料誌本文は取得せず、公開メタデータ・要旨のみ |
| 部分失敗許容 | 1ソース停止でも他ソースで継続。全停止時はスタブページ |
| 監査可能 | Git 履歴がそのまま監査ログ・バックアップ |

> **GitHub Models は 2026-07-30 に完全廃止されました**。`provider` には指定できません。
> 将来の代替は Microsoft Foundry Models（Azure 課金）等を検討してください。

## 3. 全体フロー

```text
[GitHub Actions: 毎日 JST 07:00]
   │
   ├─ PubMed 収集（バイオ）
   ├─ arXiv 収集（宇宙）
   ├─ 名寄せ（DOI + タイトル類似度）
   ├─ スコアリング → 分野別上位N件を選定
   ├─ AI 入力ファイル生成
   ├─ [provider=gemini の場合] Gemini で自動評価
   └─ docs/digests/YYYY-MM-DD-input.md と YYYY-MM-DD.md を commit
         │
         ▼
[provider=manual の場合のみ]
   スマホで入力ファイルをコピー → Claude/ChatGPT → Issue 貼り付け
   → issue-ingest が YYYY-MM-DD.md を生成

## 4. ファイル構成
.
├── README.md
├── requirements.txt
├── config/
│   ├── sources.yaml
│   ├── scoring.yaml
│   └── evaluator.yaml          # AI評価の切替設定
├── prompts/
│   └── eval_v1.md
├── src/digest/
│   ├── __init__.py
│   ├── sanitize.py
│   ├── collectors.py
│   ├── evaluator.py            # Gemini 自動評価
│   ├── pipeline.py
│   └── main.py
├── .github/
│   ├── ISSUE_TEMPLATE/
│   │   └── ai-eval-result.md
│   └── workflows/
│       ├── daily-collect.yml
│       └── issue-ingest.yml
└── docs/
    ├── index.md
    └── digests/
        └── .gitkeep

生成物:
パス 内容
docs/digests/YYYY-MM-DD-input.md AI 入力ファイル（毎日）
docs/digests/YYYY-MM-DD.md 評価済みダイジェスト（自動 or Issue 取り込み後）
docs/metrics.json ソース別件数・成功率・evaluated フラグ


