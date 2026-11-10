"""フェーズ導出レイヤー (phases/v1)。

最新 long 評価の代表 TRL (整数 1〜9) から、その技術がイノベーションの
3障壁 (魔の川・死の谷・ダーウィンの海) に対してどこにいるか (現在地) を
決定的に導く。stage・tiers と同じ表示専用の層で、評価・選定・tier 導出
には一切使わない。導出は export 時に行い、DB には保存しない (TRL が
変われば自動で追随する)。

定義と境界の根拠 (設計書 ~/workspace/trl-phases/DESIGN.md §1・§3):
- 3障壁の定義は NEDO「NEDO によるプロジェクトマネジメントの進化に向けて」
- 境界は NEDO の TRL 文言の切れ目に置く: TRL3「技術コンセプトの確認、
  要素技術の構想」→ TRL4「各開発要素の製作と性能確認」、TRL6「実証
  システムの導入環境に近い環境での実証」→ TRL7「製品候補の製作」。
  障壁区間には「移行の手前側の最終レベル」を割り当て、9レベル全てが
  ちょうど1つの区間に属して現在地が一意に決まるようにする (P-1)
- ダーウィンの海に「定着期」は定義しない (P-2)。市場での生存は技術の
  成熟では決まらず、TRL 9 から assert することは採点基準の原則1
  (商用展開と TRL の分離) と原則3 (TRL の高低から市場での成否を
  推論しない) に反するため。海については「移行期」までが TRL から
  決定的に言える範囲とする
- 状態語彙は 前駆期 (before) / 移行期 (in) / 定着期 (crossed) とする
  (泰崇の案。区間名が主表示で、状態は詳細ページに障壁ごとに併記する)

表示側 (web/app.js・web/detail.html・web/criteria.html) のラベルと定義文は
このモジュールの定義の転記であり、境界を変えるときはルール版を上げて
表示側を同期させる (規程の criteria.html 転記と同じ運用)。
"""

PHASE_RULES_VERSION = "phases/v1"

# 区間の並び (TRL の昇順)。トラック・フィルタ・凡例はこの順で出す
ZONE_ORDER = ("research", "devil_river", "development",
              "valley_of_death", "commercialization", "darwinian_sea")

ZONE_LABELS = {"research": "研究期", "devil_river": "魔の川",
               "development": "開発期", "valley_of_death": "死の谷",
               "commercialization": "事業化期", "darwinian_sea": "ダーウィンの海"}

# 区間が占める TRL レベル数 (トラックの幅はこの比に比例させる)
ZONE_SPANS = {"research": 2, "devil_river": 1, "development": 2,
              "valley_of_death": 1, "commercialization": 2, "darwinian_sea": 1}

ZONE_DEFS = {
    "research": "科学的な基本原理・現象の発見・確認と、その定式化・応用可能性の確認の段階（NEDO の TRL 1〜2）。製作はまだ始まらない。",
    "devil_river": "技術コンセプトの確認・要素技術の構想の段階（NEDO の TRL 3）。研究側でできる最終段で、次段から開発要素の製作が始まるため、魔の川の移行期にある。",
    "development": "各開発要素の製作と性能確認から、全てを統合した実証システム（試作品）の製作まで（NEDO の TRL 4〜5）。",
    "valley_of_death": "実証システムを導入環境に近い環境で実証する段階（NEDO の TRL 6）。開発側の最終段で、次段の製品候補に向けて、死の谷の移行期にある。",
    "commercialization": "製品候補の製作と導入環境での実証（TRL 7）、製品の製作と販売（パイロットライン、TRL 8）の段階。",
    "darwinian_sea": "商品化・大量生産の段階（NEDO の TRL 9）。市場に晒され、ダーウィンの海の最中にある。市場で生き残るかどうかは TRL からは判定しない。",
}

# TRL → 区間 (境界の置き方は冒頭のとおり)
_ZONE_BY_TRL = {1: "research", 2: "research", 3: "devil_river",
                4: "development", 5: "development", 6: "valley_of_death",
                7: "commercialization", 8: "commercialization",
                9: "darwinian_sea"}

# 障壁の並びと、その障壁の「移行期」にあたる TRL (手前側の最終レベル)
BARRIER_ORDER = ("devil_river", "valley_of_death", "darwinian_sea")
_BARRIER_AT = {"devil_river": 3, "valley_of_death": 6, "darwinian_sea": 9}

BARRIER_LABELS = {"devil_river": "魔の川", "valley_of_death": "死の谷",
                  "darwinian_sea": "ダーウィンの海"}

# NEDO の定義 (§1-1) に基づく障壁の説明
BARRIER_DEFS = {
    "devil_river": "「魔の川」と呼ばれる研究開発段階の不確実性（NEDO）。研究から開発へ進めるかどうかの関門。",
    "valley_of_death": "「死の谷」と呼ばれる、研究開発後の事業化に要する調達、生産、流通の体制構築や資金調達の困難（NEDO）。",
    "darwinian_sea": "「ダーウィンの海」と呼ばれる市場獲得における生存競争の困難（NEDO）。",
}

STATE_LABELS = {"before": "前駆期", "in": "移行期", "crossed": "定着期"}
STATE_DEFS = {"before": "その障壁に差し掛かる前の段階",
              "in": "その障壁の移行の最中にある段階",
              "crossed": "その障壁を越え、次の段階に定着した段階"}


def phase_for_trl(level):
    """代表 TRL (整数 1〜9) から現在地を導く純粋関数。

    戻り値は export にそのまま載せる形:
      {"v": ルール版, "trl": level, "zone": 区間キー,
       "barriers": {障壁キー: "before" / "in" / "crossed"}}
    ダーウィンの海の "crossed" は定義しないため現れない (TRL 9 が最上)。
    TRL が整数 1〜9 でなければ None (フェーズ無しとして表示しない)。
    """
    if not isinstance(level, int) or isinstance(level, bool):
        return None
    zone = _ZONE_BY_TRL.get(level)
    if zone is None:
        return None
    barriers = {}
    for key in BARRIER_ORDER:
        at = _BARRIER_AT[key]
        if level < at:
            barriers[key] = "before"
        elif level == at:
            barriers[key] = "in"
        elif key == "darwinian_sea":
            barriers[key] = "in"  # 到達しない分岐 (at=9 が上限)。海に定着期は無い
        else:
            barriers[key] = "crossed"
    return {"v": PHASE_RULES_VERSION, "trl": level, "zone": zone,
            "barriers": barriers}
