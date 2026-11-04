"""SQLite による永続化。重複排除は identifiers テーブルの安定IDで行う。"""


def _filing_period_key(title):
    """EDINET 表題の「第N期(開始－終了)」を取り出す。訂正版と元書類の対応付け用。"""
    m = re.search(r"第\d+期\([^)]*\)", title or "")
    return m.group(0) if m else None

import hashlib
import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .dates import parse_date
from .normalize import gnews_title_norm


class _DeferrableConnection(sqlite3.Connection):
    """commit() を保留できる接続。評価結果の保存 (評価本体・キュー・候補の
    状態) を defer_commits() で1トランザクションに束ね、途中終了で
    「評価はあるが状態が古い」中途半端な書込みが残らないようにする。"""

    _defer = 0

    def commit(self):
        if self._defer:
            return
        super().commit()

    def commit_now(self):
        super().commit()


SCHEMA = """
CREATE TABLE IF NOT EXISTS candidates(
  id TEXT PRIMARY KEY, title TEXT, abstract TEXT, url TEXT, source TEXT, source_id TEXT,
  doi TEXT, published TEXT, retrieved_at TEXT, fields_json TEXT, info_type TEXT,
  status TEXT NOT NULL DEFAULT 'new', screen_reason TEXT, last_eval_at TEXT,
  retry_count INTEGER NOT NULL DEFAULT 0, first_seen_run TEXT,
  title_ja TEXT, abstract_ja TEXT, translation_src TEXT, media_url TEXT);
CREATE TABLE IF NOT EXISTS identifiers(key TEXT PRIMARY KEY, candidate_id TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS candidate_versions(
  id INTEGER PRIMARY KEY AUTOINCREMENT, candidate_id TEXT, title TEXT, abstract TEXT,
  published TEXT, source TEXT, superseded_at TEXT, run_id TEXT);
CREATE TABLE IF NOT EXISTS evaluations(
  id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, candidate_id TEXT, kind TEXT, as_of TEXT,
  evaluated_at TEXT, status TEXT, total REAL, band TEXT, items_json TEXT, extraction_json TEXT,
  event_json TEXT, prompt_versions_json TEXT, config_version TEXT, config_hash TEXT,
  llm_provider TEXT, llm_model TEXT, input_hash TEXT, errors_json TEXT);
CREATE TABLE IF NOT EXISTS extraction_cache(
  input_hash TEXT, prompt_sha TEXT, model TEXT, extraction_json TEXT, meta_json TEXT, created_at TEXT,
  PRIMARY KEY(input_hash, prompt_sha, model));
CREATE TABLE IF NOT EXISTS invalid_items(
  key TEXT NOT NULL, published TEXT NOT NULL DEFAULT '', config_hash TEXT NOT NULL,
  first_seen TEXT, last_seen TEXT, PRIMARY KEY(key, published, config_hash));
CREATE TABLE IF NOT EXISTS llm_calls(
  id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, candidate_id TEXT, purpose TEXT,
  provider TEXT, model TEXT, seconds REAL, ok INTEGER, error TEXT,
  chars_in INTEGER, chars_out INTEGER, tokens_in INTEGER, tokens_out INTEGER, created_at TEXT);
CREATE TABLE IF NOT EXISTS compare_results(
  id INTEGER PRIMARY KEY AUTOINCREMENT, batch TEXT, candidate_id TEXT, label TEXT,
  status TEXT, total REAL, band TEXT, trl_level INTEGER, items_json TEXT,
  seconds REAL, error TEXT, baseline_status TEXT, baseline_total REAL,
  baseline_trl INTEGER, baseline_model TEXT, actual_model TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS review_queue(
  candidate_id TEXT, kind TEXT, reason TEXT, missing_json TEXT, next_sources_json TEXT,
  reeval_conditions_json TEXT, last_eval_at TEXT, versions_json TEXT, created_at TEXT,
  updated_at TEXT, resolved_at TEXT, PRIMARY KEY(candidate_id, kind));
CREATE TABLE IF NOT EXISTS runs(
  run_id TEXT PRIMARY KEY, started_at TEXT, finished_at TEXT, config_version TEXT, config_hash TEXT,
  as_of TEXT, params_json TEXT, counts_json TEXT, status TEXT, duration_sec REAL);
CREATE TABLE IF NOT EXISTS run_sources(
  run_id TEXT, source TEXT, query TEXT, fetched INTEGER, new INTEGER, duplicates INTEGER,
  invalid INTEGER, ok INTEGER, error TEXT, seconds REAL);
CREATE TABLE IF NOT EXISTS candidate_entities (
    candidate_id TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    strength TEXT NOT NULL,
    matched_by TEXT NOT NULL,
    matched_alias TEXT NOT NULL,
    link_type TEXT NOT NULL DEFAULT 'entity',
    product_id TEXT NOT NULL DEFAULT '',
    product_name TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (
        candidate_id,
        entity_id,
        link_type,
        product_id,
        matched_by,
        matched_alias
    ),
    FOREIGN KEY (candidate_id) REFERENCES candidates(id)
);
-- related_news / related_financials の企業経由ジョイン用。主キーは candidate_id
-- 先頭のため entity_id 単独の探索が全走査になり、候補の増加に比例して重かった
CREATE INDEX IF NOT EXISTS idx_candidate_entities_entity ON candidate_entities(entity_id, candidate_id);
"""


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _j(x):
    return json.dumps(x, ensure_ascii=False, sort_keys=True)


class Store:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), factory=_DeferrableConnection)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=DELETE")  # -wal/-shm を作らない(コミット対象を1ファイルに)
        self.conn.executescript(SCHEMA)
        self._migrate()

    def _migrate(self):
        """既存DBに後から追加した列を足す(CREATE TABLE IF NOT EXISTS では増えないため)。"""
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(evaluations)")}
        if "extra_json" not in cols:
            self.conn.execute("ALTER TABLE evaluations ADD COLUMN extra_json TEXT")
        ccols = {r[1] for r in self.conn.execute("PRAGMA table_info(candidates)")}
        for col in ("title_ja", "abstract_ja", "translation_src", "material_updated_at", "media_url", "links_hash"):
            if col not in ccols:
                self.conn.execute(f"ALTER TABLE candidates ADD COLUMN {col} TEXT")
        cmpcols = {r[1] for r in self.conn.execute("PRAGMA table_info(compare_results)")}
        if cmpcols and "actual_model" not in cmpcols:
            self.conn.execute("ALTER TABLE compare_results ADD COLUMN actual_model TEXT")
        self.conn.commit()

    def set_translation(self, cid, title_ja, abstract_ja, title_by=None, abstract_by=None):
        """訳文と、その出典(どの経路で訳したか)を保存する。出典は項目別に記録する。"""
        row = self.conn.execute("SELECT translation_src FROM candidates WHERE id=?", (cid,)).fetchone()
        try:
            src = json.loads(row[0]) if row and row[0] else {}
        except ValueError:
            src = {}
        if title_by:
            src["title"] = title_by
        if abstract_by:
            src["abstract"] = abstract_by
        self.conn.execute("UPDATE candidates SET title_ja=?, abstract_ja=?, translation_src=? WHERE id=?",
                          (title_ja, abstract_ja, _j(src) if src else None, cid))
        self.conn.commit()

    def close(self):
        self.conn.commit()
        self.conn.close()

    @contextmanager
    def defer_commits(self):
        """ブロック内の commit() を保留し、正常終了時にまとめて確定する。
        例外時はロールバックし、部分的な書込みを残さない。"""
        conn = self.conn
        conn._defer += 1
        try:
            yield
        except Exception:
            conn._defer -= 1
            if conn._defer == 0:
                conn.rollback()
            raise
        conn._defer -= 1
        if conn._defer == 0:
            conn.commit_now()

    def orphan_report(self):
        """参照先の候補・実行が存在しない行 (孤児) の件数をテーブル別に返す。
        スキーマに外部キーがあるのは candidate_entities だけで、強制も既定で
        無効なため、定期的な検査で静かな腐敗を検出する。削除はしない。"""
        targets = {
            "identifiers": ("candidate_id", "candidates", "id"),
            "candidate_entities": ("candidate_id", "candidates", "id"),
            "evaluations": ("candidate_id", "candidates", "id"),
            "review_queue": ("candidate_id", "candidates", "id"),
            "llm_calls": ("candidate_id", "candidates", "id"),
            "run_sources": ("run_id", "runs", "run_id"),
        }
        out = {}
        for table, (col, ref_table, ref_col) in targets.items():
            n = self.conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE {col} IS NOT NULL "
                f"AND {col} NOT IN (SELECT {ref_col} FROM {ref_table})").fetchone()[0]
            if n:
                out[table] = n
        return out

    def eval_counts_long(self):
        """候補ごとの long 評価 (採点が完了した final/hold) の累計回数。
        サイトの「評価○回目」表示用で、観測の厚みを示す。"""
        return {cid: n for cid, n in self.conn.execute(
            "SELECT candidate_id, COUNT(*) FROM evaluations "
            "WHERE kind = 'long' AND status IN ('final', 'hold') GROUP BY candidate_id")}

    # --- candidates ---
    def _window_owner(self, rec, window_keys, entity_ids, window_days):
        """窓つき統合の所有者探し。window_keys の所有者のうち、企業リンク
        集合が entity_ids と一致し、published の日付差が window_days 以内で、
        rejected でない候補があればその id を返す。published がどちらか
        空・解釈不能なら窓判定はせず None (従来のキー照合に任せる)。"""
        rec_date = parse_date((rec.get("published") or "")[:10])
        if rec_date is None:
            return None
        want = set(entity_ids)
        for wk in window_keys:
            if not wk:
                continue
            row = self.conn.execute(
                "SELECT candidate_id FROM identifiers WHERE key=?", (wk,)).fetchone()
            if not row:
                continue
            owner = self.get_candidate(row[0])
            if owner is None or owner.get("status") == "rejected":
                continue
            if {l["entity_id"] for l in owner["entities"]} != want:
                continue
            own_date = parse_date((owner.get("published") or "")[:10])
            if own_date is None:
                continue
            if abs((rec_date - own_date).days) <= window_days:
                return owner["id"]
        return None

    def upsert_candidate(self, rec, run_id=None, window_keys=None, entity_ids=None, window_days=3):
        """-> (candidate_id, is_new)。既存IDに一致すれば統合し、欠けた項目だけ補う。

        window_keys と entity_ids を渡すと、通常のキー照合の前に窓つき統合を
        試す (gnews の日付ゆれ対策。pipeline の GNEWS_MERGE_WINDOW_DAYS 参照)。
        窓で統合された場合も、通常の統合と同じく rec のキーは所有者へ登録され、
        欠けた項目だけ補われる。"""
        c = self.conn
        existing = None
        if window_keys and entity_ids:
            existing = self._window_owner(rec, window_keys, entity_ids, window_days)
        if existing is None:
            for k in rec["keys"]:
                row = c.execute("SELECT candidate_id FROM identifiers WHERE key=?", (k,)).fetchone()
                if row:
                    existing = row[0]
                    break
        if existing:
            cur = self.get_candidate(existing)
            if cur is None:
                existing = None  # identifiers だけが残った孤立参照は新規として扱う
        if existing:
            for k in rec["keys"]:
                c.execute("INSERT OR IGNORE INTO identifiers VALUES(?,?)", (k, existing))
            if not cur["abstract"] and rec["abstract"]:
                c.execute("UPDATE candidates SET abstract=? WHERE id=?", (rec["abstract"], existing))
            if not cur["doi"] and rec.get("doi"):
                c.execute("UPDATE candidates SET doi=? WHERE id=?", (rec["doi"], existing))
            if not cur.get("media_url") and rec.get("media_url"):
                c.execute("UPDATE candidates SET media_url=? WHERE id=?", (rec["media_url"], existing))
            # 治験登録はレジストリ側で内容が更新される(結果公表・ステータス変更)。
            # 要旨が変わったら取り込み、材料更新の印を付ける(優先再評価の対象になる)。
            # 非破壊のため、上書きする前の版は candidate_versions に残す
            # (旧版が無いと比較ができない。2026-11-04 の非破壊原則)
            if rec.get("source") == "trials" and rec.get("abstract") and cur["abstract"] != rec["abstract"]:
                self._save_candidate_version(cur, run_id)
                c.execute("UPDATE candidates SET title=?, abstract=?, published=?, material_updated_at=? WHERE id=?",
                          (rec["title"], rec["abstract"], rec.get("published"), now_iso(), existing))
            # 財務開示は出典側で数値が訂正され得る (I-2)。開示の要旨は財務数値
            # テキストそのものなので、再取得で変わっていたら取り込む (旧版は
            # candidate_versions に残る)。材料更新の印を付ける (治験と同じ扱い)。
            # 表題が違うものは別書類とみなし、キーが衝突しても上書きしない。
            if (rec.get("info_type") == "disclosure" and rec.get("abstract")
                    and cur["title"] == rec.get("title") and cur["abstract"] != rec["abstract"]):
                self._save_candidate_version(cur, run_id)
                c.execute("UPDATE candidates SET title=?, abstract=?, published=?, material_updated_at=? WHERE id=?",
                          (rec["title"], rec["abstract"], rec.get("published"), now_iso(), existing))
            c.commit()
            return existing, False
        cid = "c_" + hashlib.sha1(rec["keys"][0].encode()).hexdigest()[:12]
        c.execute(
            "INSERT INTO candidates(id,title,abstract,url,source,source_id,doi,published,retrieved_at,"
            "fields_json,info_type,status,first_seen_run,media_url) VALUES(?,?,?,?,?,?,?,?,?,?,?,'new',?,?)",
            (cid, rec["title"], rec["abstract"], rec["url"], rec["source"], rec["source_id"], rec.get("doi"),
             rec["published"], rec["retrieved_at"], _j(rec["fields"]), rec["info_type"], run_id,
             rec.get("media_url") or ""))
        for k in rec["keys"]:
            c.execute("INSERT OR IGNORE INTO identifiers VALUES(?,?)", (k, cid))
        c.commit()
        return cid, True

    def dedupe_gnews(self, window_days=3):
        """gnews の重複を統合する保守パス。統合した行数を返す (冪等)。

        gtitle キー導入前の旧行は安定IDを持たず、再配信のたびに双子が
        生まれていた。また gtitle の日付成分のため、日付をずらした再配信は
        キー付き同士でも割れる。ここでは (企業リンク集合, 正規化タイトル)
        でグループ化し、published の広がりが window_days 以内のグループは
        最古行 (published → retrieved_at → id の順で最小) を survivor と
        して残し、他の行を rejected にして識別子を survivor へ付け替える。
        行自体は消さない (非破壊・可逆)。評価が付いた行は、統合される側
        にも survivor にもしない (対象から外す)。published が解釈できない
        行も、窓の中にあると確認できないため対象外にする。

        あわせて、生存する全ての gnews 行へ補助キー (gaux) を付与する。
        これで旧行も含め、将来の再配信が upsert_candidate の窓つき統合で
        survivor へ吸収されるようになる。"""
        c = self.conn
        rows = c.execute(
            "SELECT id, title, published, retrieved_at, status FROM candidates "
            "WHERE source='gnews'").fetchall()
        ent = {}
        for r in c.execute("SELECT candidate_id, entity_id FROM candidate_entities"):
            ent.setdefault(r["candidate_id"], set()).add(r["entity_id"])
        evaluated = {r[0] for r in c.execute(
            "SELECT DISTINCT candidate_id FROM evaluations")}
        groups = {}
        for r in rows:
            if r["status"] == "rejected" or r["id"] in evaluated:
                continue
            es = frozenset(ent.get(r["id"], ()))
            nt = gnews_title_norm(r["title"])
            # 企業リンクが無い行と短いタイトルは、gtitle/gaux のキー方針と
            # 同じく誤統合を避けて対象にしない
            if not es or len(nt) < 12:
                continue
            groups.setdefault((es, nt), []).append(r)
        merged = 0
        merged_ids = set()
        for members in groups.values():
            if len(members) < 2:
                continue
            dated = []
            for r in members:
                d = parse_date((r["published"] or "")[:10])
                if d is not None:
                    dated.append((d, r))
            if len(dated) < 2:
                continue
            dates = [d for d, _ in dated]
            if (max(dates) - min(dates)).days > window_days:
                continue
            dated.sort(key=lambda x: (x[0], x[1]["retrieved_at"] or "", x[1]["id"]))
            survivor = dated[0][1]
            for _, r in dated[1:]:
                # 識別子を survivor へ付け替える。identifiers.key は主キーで
                # 所有者は一意なので、他の候補が所有しているキーはあり得ないが、
                # 念のため survivor 以外が所有者の場合だけ付け替える
                for (k,) in c.execute(
                        "SELECT key FROM identifiers WHERE candidate_id=?",
                        (r["id"],)).fetchall():
                    row = c.execute(
                        "SELECT candidate_id FROM identifiers WHERE key=?", (k,)).fetchone()
                    if row and row[0] != survivor["id"]:
                        c.execute("UPDATE identifiers SET candidate_id=? WHERE key=?",
                                  (survivor["id"], k))
                c.execute(
                    "UPDATE candidates SET status='rejected', screen_reason=? WHERE id=?",
                    (f"duplicate_of:{survivor['id']}", r["id"]))
                merged += 1
                merged_ids.add(r["id"])
        # 生存する全 gnews 行へ gaux を付与 (企業ごとに1キー)。今回の統合で
        # rejected になった行は除く (スナップショットの status は古いため)
        for r in rows:
            if r["status"] == "rejected" or r["id"] in merged_ids:
                continue
            nt = gnews_title_norm(r["title"])
            if len(nt) < 12:
                continue
            for eid in ent.get(r["id"], ()):
                c.execute("INSERT OR IGNORE INTO identifiers VALUES(?,?)",
                          (f"gaux:{eid}:{nt}", r["id"]))
        c.commit()
        return merged

    def _save_candidate_version(self, cur, run_id=None):
        """上書きされる直前の材料 (題名・要旨・公開日) を版として残す。
        材料の版が残っていれば、評価時点との比較や変更内容の追跡ができる。"""
        self.conn.execute(
            "INSERT INTO candidate_versions(candidate_id,title,abstract,published,source,"
            "superseded_at,run_id) VALUES(?,?,?,?,?,?,?)",
            (cur["id"], cur["title"], cur["abstract"], cur["published"], cur["source"],
             now_iso(), run_id))

    def candidate_versions(self, cid):
        """候補の旧版 (古い順)。材料がどう変わったかを後から比較するための記録。"""
        rows = self.conn.execute(
            "SELECT title, abstract, published, source, superseded_at, run_id"
            " FROM candidate_versions WHERE candidate_id=? ORDER BY id", (cid,)).fetchall()
        return [dict(r) for r in rows]

    def get_candidate(self, cid):
        row = self.conn.execute("SELECT * FROM candidates WHERE id=?", (cid,)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["fields"] = json.loads(d.pop("fields_json") or "[]")
        d["entities"] = self.get_entities(cid)
        return d
        
    def set_entities(self, cid, links):
        """企業・製品リンクを保存し、既存の強いリンクを維持する。"""
        c = self.conn

        for link in links:
            entity_id = link["entity_id"]
            strength = link["strength"]
            matched_by = link.get("matched_by", link.get("field", ""))
            matched_alias = link.get("matched_alias", link.get("alias", ""))
            link_type = link.get("link_type", "entity")
            product_id = link.get("product_id") or ""
            product_name = link.get("product_name") or ""

            existing = c.execute(
                "SELECT strength FROM candidate_entities "
                "WHERE candidate_id=? AND entity_id=? AND link_type=? "
                "AND product_id=? AND matched_by=? AND matched_alias=?",
                (
                    cid, entity_id, link_type, product_id,
                    matched_by, matched_alias,
                ),
            ).fetchone()

            if existing:
                if existing["strength"] != "strong" and strength == "strong":
                    c.execute(
                        "UPDATE candidate_entities SET strength=? "
                        "WHERE candidate_id=? AND entity_id=? AND link_type=? "
                        "AND product_id=? AND matched_by=? AND matched_alias=?",
                        (
                            "strong", cid, entity_id, link_type,
                            product_id, matched_by, matched_alias,
                        ),
                    )
                continue

            c.execute(
                "INSERT INTO candidate_entities "
                "(candidate_id, entity_id, strength, matched_by, matched_alias, "
                "link_type, product_id, product_name) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    cid, entity_id, strength, matched_by, matched_alias,
                    link_type, product_id, product_name,
                ),
            )

        c.commit()


    def get_entities(self, cid):
        """候補論文に紐づく企業・製品リンクを取得する。"""
        rows = self.conn.execute(
            "SELECT entity_id, strength, matched_by, matched_alias, "
            "link_type, product_id, product_name "
            "FROM candidate_entities WHERE candidate_id=? "
            "ORDER BY entity_id, link_type, matched_by",
            (cid,),
        ).fetchall()

        return [
            {
                "entity_id": row["entity_id"],
                "strength": row["strength"],
                "matched_by": row["matched_by"],
                "matched_alias": row["matched_alias"],
                "link_type": row["link_type"],
                "product_id": row["product_id"],
                "product_name": row["product_name"],
            }
            for row in rows
        ]

    def list_by_status(self, statuses):
        # 候補本体と企業リンクを一括で引く (旧実装は候補ごとに2クエリで、
        # 候補数に比例してクエリが増えた。出力の dict 形状・順序は同一に保つ)
        q = ",".join("?" * len(statuses))
        rows = self.conn.execute(f"SELECT * FROM candidates WHERE status IN ({q})", statuses).fetchall()
        ids = [r["id"] for r in rows]
        links = {}
        for i in range(0, len(ids), 900):
            chunk = ids[i:i + 900]
            qq = ",".join("?" * len(chunk))
            for lr in self.conn.execute(
                    "SELECT candidate_id, entity_id, strength, matched_by, matched_alias, "
                    "link_type, product_id, product_name FROM candidate_entities "
                    f"WHERE candidate_id IN ({qq}) ORDER BY entity_id, link_type, matched_by", chunk):
                links.setdefault(lr["candidate_id"], []).append(
                    {"entity_id": lr["entity_id"], "strength": lr["strength"],
                     "matched_by": lr["matched_by"], "matched_alias": lr["matched_alias"],
                     "link_type": lr["link_type"], "product_id": lr["product_id"],
                     "product_name": lr["product_name"]})
        out = []
        for r in rows:
            d = dict(r)
            d["fields"] = json.loads(d.pop("fields_json") or "[]")
            d["entities"] = links.get(d["id"], [])
            out.append(d)
        return out

    def set_status(self, cid, status, reason=None, eval_at=None, retry_inc=False, retry_reset=False):
        # retry_reset: 成功 (hold/final) で再試行カウンタを戻す。戻さないと過去の失敗が
        # 生涯累積し、後年の1回の失敗で再試行上限に達してしまう
        self.conn.execute(
            "UPDATE candidates SET status=?, screen_reason=COALESCE(?,screen_reason), "
            "last_eval_at=COALESCE(?,last_eval_at), "
            "retry_count=CASE WHEN ? THEN 0 ELSE retry_count+? END WHERE id=?",
            (status, reason, eval_at, 1 if retry_reset else 0, 1 if retry_inc else 0, cid))
        self.conn.commit()

    def count_candidates(self):
        return self.conn.execute("SELECT COUNT(*) FROM candidates").fetchone()[0]

    def purge_evaluations(self):
        """評価とレビューキューを全て削除し、評価済み候補を screened(未評価) に戻す。
        候補・収集物・企業リンク・抽出キャッシュ・実行履歴は残す。削除件数を返す。"""
        c = self.conn
        n_eval = c.execute("SELECT COUNT(*) FROM evaluations").fetchone()[0]
        n_queue = c.execute("SELECT COUNT(*) FROM review_queue").fetchone()[0]
        c.execute("DELETE FROM evaluations")
        c.execute("DELETE FROM review_queue")
        cur = c.execute("UPDATE candidates SET status='screened', last_eval_at=NULL, retry_count=0 "
                        "WHERE status IN ('hold','error','final','evaluated')")
        c.commit()
        return {"evaluations": n_eval, "queue": n_queue, "candidates_reset": cur.rowcount}

    def prune(self, as_of, entity_markets=None, news_days=180, disclosure_days=800,
              rejected_days=180, cache_days=180):
        """古い材料を削除し、DB の単調増加を抑える。

        対象は「プール専用の材料」と「除外済み」と「旧キー形式の重複」だけに限る。
        評価が付いた候補と、未解決のレビューキューにある候補は決して消さない
        (サイト表示と再評価の前提になるため)。保持期間は利用側の窓の2倍に取る:
        ニュースは関連プール (90日) の2倍で180日、財務は財務プール (400日) の2倍で800日。
        """
        c = self.conn
        protected = {r[0] for r in c.execute("SELECT DISTINCT candidate_id FROM evaluations")}
        protected |= {r[0] for r in c.execute(
            "SELECT candidate_id FROM review_queue WHERE resolved_at IS NULL")}
        targets = {}  # cid -> 理由

        def add(cid, reason):
            if cid not in protected:
                targets.setdefault(cid, reason)

        news_cut = (as_of - timedelta(days=news_days)).isoformat()
        for (cid,) in c.execute("SELECT id FROM candidates WHERE info_type='news' "
                                "AND published IS NOT NULL AND published < ?", (news_cut,)):
            add(cid, "news")
        disc_cut = (as_of - timedelta(days=disclosure_days)).isoformat()
        for (cid,) in c.execute("SELECT id FROM candidates WHERE info_type='disclosure' "
                                "AND published IS NOT NULL AND published < ?", (disc_cut,)):
            add(cid, "disclosure")
        rej_cut = (as_of - timedelta(days=rejected_days)).isoformat()
        for (cid,) in c.execute("SELECT id FROM candidates WHERE status='rejected' AND "
                                "((published IS NOT NULL AND published < ?) OR "
                                " (published IS NULL AND substr(retrieved_at,1,10) < ?))",
                                (rej_cut, rej_cut)):
            add(cid, "rejected")
        for cid in self._stale_yahoo_key_ids(entity_markets or {}):
            add(cid, "stale_yahoo_keys")
        counts = {"news": 0, "disclosure": 0, "rejected": 0, "stale_yahoo_keys": 0}
        for cid, reason in targets.items():
            c.execute("DELETE FROM candidate_entities WHERE candidate_id=?", (cid,))
            c.execute("DELETE FROM identifiers WHERE candidate_id=?", (cid,))
            c.execute("DELETE FROM candidate_versions WHERE candidate_id=?", (cid,))
            c.execute("DELETE FROM candidates WHERE id=?", (cid,))
            counts[reason] += 1
        cache_cut = (as_of - timedelta(days=cache_days)).isoformat()
        cur = c.execute("DELETE FROM extraction_cache WHERE substr(created_at,1,10) < ?", (cache_cut,))
        counts["extraction_cache"] = cur.rowcount
        cur = c.execute("DELETE FROM invalid_items WHERE substr(last_seen,1,10) < ?", (cache_cut,))
        counts["invalid_items"] = cur.rowcount
        counts["total"] = sum(counts[k] for k in ("news", "disclosure", "rejected", "stale_yahoo_keys"))
        c.commit()
        if counts["total"]:
            try:
                c.execute("VACUUM")
            except Exception:
                pass  # ファイルが縮まないだけ (行の削除は完了している)
        return counts

    def prune_stale_links(self, entities):
        """辞書から消えた alias で作られた企業リンクを削除する。

        candidate_entities.matched_alias はリンク生成時に使われた alias の
        記録。その alias が現在の辞書 (企業名・aliases・製品名・製品 aliases)
        にもう存在しない場合、そのリンクは陳腐化している (例: 同名別組織への
        誤リンクを防ぐために単独形 alias を削除したケース)。候補そのものは
        消さずリンクだけを外す。辞書に載っていない企業へのリンクは判断
        できないため触らない。
        """
        valid = {}
        for e in entities or []:
            names = {e.get("name")} | set(e.get("aliases") or [])
            for p in e.get("products") or []:
                names.add(p.get("name"))
                names.update(p.get("aliases") or [])
            valid[e.get("id")] = {n for n in names if n}
        c = self.conn
        deleted = 0
        rows = c.execute(
            "SELECT candidate_id, entity_id, matched_alias FROM candidate_entities "
            "WHERE matched_alias IS NOT NULL AND matched_alias != ''").fetchall()
        for cid, eid, alias in rows:
            if eid in valid and alias not in valid[eid]:
                cur = c.execute(
                    "DELETE FROM candidate_entities WHERE candidate_id=? AND entity_id=? "
                    "AND matched_alias=?", (cid, eid, alias))
                deleted += cur.rowcount
        c.commit()
        return deleted

    def _stale_yahoo_key_ids(self, entity_markets):
        """旧形式キー (接尾辞なし) の Yahoo 財務で、新形式 (接尾辞つき) の同企業・
        同時期レコードが既にあるものを返す。移行期のツインのうち旧側だけを消す。
        新形式が無い企業の旧レコードは (それが唯一の財務のうちは) 残す。"""
        c = self.conn
        rows = c.execute(
            "SELECT i.key, i.candidate_id, n.published FROM identifiers i "
            "JOIN candidates n ON n.id = i.candidate_id "
            "WHERE i.key LIKE 'srcid:yahoo:%' AND n.source='yahoo'").fetchall()
        info = {}  # cid -> (symbol, published)
        for key, cid, published in rows:
            sid = key[len("srcid:yahoo:"):]
            info[cid] = (sid[:-11] if len(sid) > 11 else sid, published)
        groups = {}  # (entity_id, published) -> {"dotted": set, "plain": set}
        for cid, (sym, published) in info.items():
            for (eid,) in c.execute("SELECT entity_id FROM candidate_entities WHERE candidate_id=?", (cid,)):
                g = groups.setdefault((eid, published), {"dotted": set(), "plain": set()})
                g["dotted" if "." in sym else "plain"].add(cid)
        stale = set()
        for (eid, _pub), g in groups.items():
            if g["dotted"] and g["plain"] and (entity_markets.get(eid) or "JP") != "US":
                stale |= g["plain"]
        return stale

    def related_news(self, cid, as_of, limit=8, window_days=90):
        """同一企業に紐付く直近ニュースを、評価時の補助根拠として返す。

        ニュースは単体では評価せず、論文・治験などの評価に同梱する材料と位置付ける。
        """
        start = (as_of - timedelta(days=window_days)).isoformat()
        # プール側のリンクは企業リンク (link_type='entity') に限る (I-3)。
        # 製品名だけが一致したニュースは別文脈の材料になり得るため入れない。
        # ニュースの strength は本文言及でも weak が通常形で、強さでは絞らない
        # (strong 限定にするとニュースプールが全滅することを実測で確認済み)。
        rows = self.conn.execute(
            "SELECT DISTINCT n.title, n.published, n.url, n.fields_json, n.source, n.media_url FROM candidates n "
            "JOIN candidate_entities ne ON ne.candidate_id = n.id "
            "JOIN candidate_entities ce ON ce.entity_id = ne.entity_id "
            "WHERE ce.candidate_id = ? AND n.id != ? AND n.info_type = 'news' "
            "AND ne.link_type = 'entity' "
            # 重複統合された行 (rejected かつ理由が duplicate_of:) は同一記事が
            # survivor 側で代表されるためプールに出さない (二重表示の防止)。
            # スクリーニング除外 (abstract_too_short 等) は従来どおり材料として残す
            "AND NOT (n.status = 'rejected' AND n.screen_reason LIKE 'duplicate_of:%') "
            "AND n.published IS NOT NULL AND n.published <= ? AND n.published >= ? "
            "ORDER BY n.published DESC, n.id LIMIT ?",
            (cid, cid, as_of.isoformat(), start, limit)).fetchall()
        out = []
        for r in rows:
            fields = json.loads(r["fields_json"] or "[]")
            out.append({"title": r["title"], "published": r["published"], "url": r["url"],
                        "media": fields[0] if fields else "",
                        "media_url": r["media_url"] or "",
                        "origin": "企業発表" if r["source"] == "prtimes" else "報道"})
        return out

    def related_financials(self, cid, as_of, limit=2, window_days=400):
        """同一企業に紐付く開示・財務データのうち、財務数値が抽出できたものを返す。

        論文・治験の抄録には財務情報が原理的に含まれないため、公表財務数値を
        評価時の「関連財務根拠」として同梱する材料。出典 (EDINET / Yahoo Finance) を
        問わず、数値マーカー「財務数値(」を含む disclosure だけが対象。
        """
        start = (as_of - timedelta(days=window_days)).isoformat()
        # プール側のリンクは strong の企業リンクに限る (I-3)。開示は企業単位で
        # 取得され通常は strong で付くため、実測では現行と同件数になる。
        # I-6 の訂正除外で枠が空く場合に備え、limit より多めに取ってから絞る。
        rows = self.conn.execute(
            "SELECT DISTINCT n.title, n.published, n.url, n.abstract FROM candidates n "
            "JOIN candidate_entities ne ON ne.candidate_id = n.id "
            "JOIN candidate_entities ce ON ce.entity_id = ne.entity_id "
            "WHERE ce.candidate_id = ? AND n.id != ? AND n.info_type = 'disclosure' "
            "AND ne.strength = 'strong' AND ne.link_type = 'entity' "
            "AND n.abstract LIKE '%財務数値(%' "
            "AND n.published IS NOT NULL AND n.published <= ? AND n.published >= ? "
            "ORDER BY n.published DESC, n.id LIMIT ?",
            (cid, cid, as_of.isoformat(), start, limit + 4)).fetchall()
        out = [{"title": r["title"], "published": r["published"], "url": r["url"],
                "text": r["abstract"]} for r in rows]
        # I-6: 訂正報告書は別 docID で別候補になるため、同一企業の同一期に
        # 訂正版がある元書類はプールから外す (新旧の数値が併記されないように)。
        # 期の対応は EDINET の表題にある「第N期(開始－終了)」の一致で取る。
        corrected = {_filing_period_key(r["title"]) for r in out
                     if "訂正" in (r["title"] or "") and _filing_period_key(r["title"])}
        if corrected:
            out = [r for r in out if "訂正" in (r["title"] or "")
                   or _filing_period_key(r["title"]) not in corrected]
        return out[:limit]

    # --- evaluations ---
    def add_evaluation(self, run_id, cid, ev):
        extra = dict(ev.get("extra") or {})
        if ev["status"] == "final" and ev.get("total") is not None:
            # 非破壊の記録: 評価当時の総合点・帯域・規程版を固定で残す。
            # 重み改訂の recalc は total 列を現行値に書き換えるが、こちらは
            # 二度と触らない (当時いくつだったかが後から比較できるように)
            extra.setdefault("as_evaluated", {
                "total": ev["total"], "band": (ev["band"] or {}).get("label"),
                "config_version": ev["config_version"]})
        cur = self.conn.execute(
            "INSERT INTO evaluations(run_id,candidate_id,kind,as_of,evaluated_at,status,total,band,items_json,"
            "extraction_json,event_json,prompt_versions_json,config_version,config_hash,llm_provider,llm_model,"
            "input_hash,errors_json,extra_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, cid, ev["kind"], ev["as_of"], now_iso(), ev["status"], ev["total"],
             (ev["band"] or {}).get("label"), _j(ev["items"]), _j(ev.get("extraction")), _j(ev.get("event")),
             _j(ev["prompt_versions"]), ev["config_version"], ev["config_hash"], ev["llm_provider"],
             ev["llm_model"], ev["input_hash"], _j(ev["errors"]), _j(extra)))
        self.conn.commit()
        return cur.lastrowid

    def final_evaluations(self):
        """再計算 (trlcore/recalc) 用に final 評価の必要列を返す。"""
        rows = self.conn.execute(
            "SELECT id, candidate_id, kind, total, band, items_json, extra_json,"
            " config_version FROM evaluations WHERE status='final' ORDER BY id").fetchall()
        return [dict(r) for r in rows]

    def update_evaluation_recalc(self, eval_id, total, band, config_version, config_hash, extra):
        self.conn.execute(
            "UPDATE evaluations SET total=?, band=?, config_version=?, config_hash=?,"
            " extra_json=? WHERE id=?",
            (total, band, config_version, config_hash, _j(extra), eval_id))
        self.conn.commit()

    def get_extraction(self, input_hash, prompt_sha, model):
        r = self.conn.execute("SELECT extraction_json, meta_json FROM extraction_cache "
                              "WHERE input_hash=? AND prompt_sha=? AND model=?", (input_hash, prompt_sha, model)).fetchone()
        return (json.loads(r[0]), json.loads(r[1])) if r else (None, None)

    def put_extraction(self, input_hash, prompt_sha, model, extraction, meta):
        self.conn.execute("INSERT OR REPLACE INTO extraction_cache VALUES(?,?,?,?,?,?)",
                          (input_hash, prompt_sha, model, _j(extraction), _j(meta), now_iso()))
        self.conn.commit()

    def identifier_index(self):
        """収集の後処理を速くするための、安定ID -> (候補ID, published, 要旨の有無, リンク世代) の索引。

        既知の同一アイテム (published 不変・リンクが現行の辞書世代で作られた
        もの) は、企業リンクの再照合と再統合を省略できる。辞書が変われば
        世代 (config_hash) が変わり、省略は自動で止まる。開示 (訂正があり
        得る) は呼び出し側で対象外にする。
        """
        out = {}
        for k, cid, pub, has_abs, lh in self.conn.execute(
                "SELECT i.key, c.id, c.published, (c.abstract IS NOT NULL AND c.abstract != ''), "
                "c.links_hash FROM identifiers i JOIN candidates c ON c.id = i.candidate_id"):
            out[k] = (cid, pub, bool(has_abs), lh)
        return out

    def set_links_hash(self, cid, config_hash):
        """候補の企業リンクが、どの設定世代 (辞書) で作られたかを記録する。"""
        self.conn.execute("UPDATE candidates SET links_hash=? WHERE id=?", (config_hash, cid))
        self.conn.commit()

    def invalid_keys(self, config_hash):
        """現行の設定世代で「企業リンクが成立しなかった」安定IDの集合 {(key, published)}。

        世代 (config_hash) が変われば辞書も変わり得るため、旧世代の記録は
        ヒットさせない (安全側: 再照合して作り直す)。
        """
        return {(k, p) for k, p in self.conn.execute(
            "SELECT key, published FROM invalid_items WHERE config_hash=?", (config_hash,))}

    def add_invalid(self, pairs, config_hash):
        """リンク不成立だった安定IDをまとめて記録する。pairs は (key, published) の列。"""
        if not pairs:
            return
        now = now_iso()
        self.conn.executemany(
            "INSERT INTO invalid_items(key, published, config_hash, first_seen, last_seen) "
            "VALUES(?,?,?,?,?) ON CONFLICT(key, published, config_hash) "
            "DO UPDATE SET last_seen=excluded.last_seen",
            [(k, p or "", config_hash, now, now) for k, p in pairs])
        self.conn.commit()

    def touch_invalids(self, pairs, config_hash):
        """ヒットした invalid 記録の last_seen をまとめて更新する (prune の生存判定用)。"""
        if not pairs:
            return
        now = now_iso()
        self.conn.executemany(
            "UPDATE invalid_items SET last_seen=? WHERE key=? AND published=? AND config_hash=?",
            [(now, k, p, config_hash) for k, p in pairs])
        self.conn.commit()

    def only_infra_errors(self, cid):
        """最新の評価がすべて HttpError(API側の失敗)だけで終わっているか。旧版が error として保存した分の救済に使う。"""
        rows = self.conn.execute(
            "SELECT e.errors_json FROM evaluations e WHERE e.candidate_id=? AND e.id IN "
            "(SELECT MAX(id) FROM evaluations WHERE candidate_id=? GROUP BY kind)", (cid, cid)).fetchall()
        if not rows:
            return False
        for (ej,) in rows:
            errs = json.loads(ej or "[]")
            if not errs or not all(str(x).startswith("HttpError") for x in errs):
                return False
        return True

    def latest_evaluation(self, cid, kind):
        row = self.conn.execute(
            "SELECT * FROM evaluations WHERE candidate_id=? AND kind=? ORDER BY id DESC LIMIT 1",
            (cid, kind)).fetchone()
        return dict(row) if row else None

    def prev_evaluation(self, cid, kind, latest_id):
        """表示中の最新評価より前にある、直近の比較可能な (final/hold) 評価。

        差分表示 (前回比) 用。error は採点結果ではないので対象にしない。
        再計算 (recalc) は行を書き換えるだけで新しい行は作らないため、
        ここに現れるのは実際に再評価された分だけになる。
        """
        row = self.conn.execute(
            "SELECT * FROM evaluations WHERE candidate_id=? AND kind=? AND id<? "
            "AND status IN ('final','hold') ORDER BY id DESC LIMIT 1",
            (cid, kind, latest_id)).fetchone()
        return dict(row) if row else None

    def evaluations_for_run(self, run_id, kind=None):
        q = "SELECT e.*, c.title, c.url FROM evaluations e JOIN candidates c ON c.id=e.candidate_id WHERE run_id=?"
        args = [run_id]
        if kind:
            q += " AND kind=?"
            args.append(kind)
        return [dict(r) for r in self.conn.execute(q, args).fetchall()]

    # --- review queue ---
    def upsert_queue(self, cid, kind, reason, missing, next_sources, reeval, last_eval_at, versions):
        now = now_iso()
        self.conn.execute(
            "INSERT INTO review_queue(candidate_id,kind,reason,missing_json,next_sources_json,"
            "reeval_conditions_json,last_eval_at,versions_json,created_at,updated_at,resolved_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,NULL) ON CONFLICT(candidate_id,kind) DO UPDATE SET "
            "reason=excluded.reason, missing_json=excluded.missing_json, "
            "next_sources_json=excluded.next_sources_json, reeval_conditions_json=excluded.reeval_conditions_json, "
            "last_eval_at=excluded.last_eval_at, versions_json=excluded.versions_json, "
            "updated_at=excluded.updated_at, resolved_at=NULL",
            (cid, kind, reason, _j(missing), _j(next_sources), _j(reeval), last_eval_at, _j(versions), now, now))
        self.conn.commit()

    def resolve_queue(self, cid, kind):
        self.conn.execute("UPDATE review_queue SET resolved_at=? WHERE candidate_id=? AND kind=? AND resolved_at IS NULL",
                          (now_iso(), cid, kind))
        self.conn.commit()

    def list_queue(self, open_only=True):
        q = ("SELECT r.*, c.title, c.url FROM review_queue r JOIN candidates c ON c.id=r.candidate_id"
             + (" WHERE r.resolved_at IS NULL" if open_only else "") + " ORDER BY r.updated_at DESC")
        out = []
        for r in self.conn.execute(q).fetchall():
            d = dict(r)
            for k in ("missing_json", "next_sources_json", "reeval_conditions_json", "versions_json"):
                d[k[:-5]] = json.loads(d.pop(k) or "null")
            out.append(d)
        return out

    # --- runs ---
    def start_run(self, run_id, config_version, config_hash, as_of, params):
        # 前の実行が異常終了して 'running' のまま残っていたら、新しい実行の開始時点で
        # 'interrupted' に確定させる (放置すると最新実行の表示や観測の前提が崩れる)
        self.conn.execute("UPDATE runs SET status='interrupted' WHERE status='running'")
        self.conn.execute("INSERT INTO runs(run_id,started_at,config_version,config_hash,as_of,params_json,status) "
                          "VALUES(?,?,?,?,?,?,'running')", (run_id, now_iso(), config_version, config_hash, as_of, _j(params)))
        self.conn.commit()

    def finish_run(self, run_id, counts, status, duration):
        self.conn.execute("UPDATE runs SET finished_at=?, counts_json=?, status=?, duration_sec=? WHERE run_id=?",
                          (now_iso(), _j(counts), status, round(duration, 2), run_id))
        self.conn.commit()

    def add_run_source(self, run_id, source, query, fetched, new, dups, invalid, ok, error, seconds):
        self.conn.execute("INSERT INTO run_sources VALUES(?,?,?,?,?,?,?,?,?,?)",
                          (run_id, source, query, fetched, new, dups, invalid, 1 if ok else 0, error, round(seconds, 2)))
        self.conn.commit()

    # --- モデル比較 (検証専用。本体の evaluations には一切触れない) ---
    def add_compare_result(self, row):
        self.conn.execute(
            "INSERT INTO compare_results(batch, candidate_id, label, status, total, band,"
            " trl_level, items_json, seconds, error, baseline_status, baseline_total,"
            " baseline_trl, baseline_model, actual_model, created_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (row["batch"], row["candidate_id"], row["label"], row.get("status"), row.get("total"),
             row.get("band"), row.get("trl_level"), _j(row.get("items") or {}),
             row.get("seconds"), row.get("error"), row.get("baseline_status"),
             row.get("baseline_total"), row.get("baseline_trl"), row.get("baseline_model"),
             row.get("actual_model"), now_iso()))
        self.conn.commit()

    def compare_results(self, batch):
        rows = self.conn.execute(
            "SELECT * FROM compare_results WHERE batch = ? ORDER BY candidate_id, label",
            (batch,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["items"] = json.loads(d.pop("items_json") or "{}")
            except ValueError:
                d["items"] = {}
            out.append(d)
        return out

    def record_llm_call(self, run_id, rec):
        """LLM コール1回 (モデル試行の単位) の計装レコードを残す。"""
        self.conn.execute(
            "INSERT INTO llm_calls(run_id, candidate_id, purpose, provider, model, seconds, ok, error,"
            " chars_in, chars_out, tokens_in, tokens_out, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, rec.get("candidate_id"), rec.get("purpose"), rec.get("provider"), rec.get("model"),
             rec.get("seconds"), 1 if rec.get("ok") else 0, rec.get("error"), rec.get("chars_in"),
             rec.get("chars_out"), rec.get("tokens_in"), rec.get("tokens_out"), now_iso()))
        self.conn.commit()

    def llm_calls(self, run_id):
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM llm_calls WHERE run_id=? ORDER BY id", (run_id,)).fetchall()]

    def get_run(self, run_id):
        r = self.conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        return dict(r) if r else None

    def update_run_params(self, run_id, **items):
        """run レコードの params_json に項目をマージ保存する。選定リストのように
        「実行が途中で死んでも残したい記録」を書くために使う (counts は終了時に
        しか書かれないため、引き継ぎの手掛かりはここに残す)。"""
        row = self.conn.execute("SELECT params_json FROM runs WHERE run_id=?", (run_id,)).fetchone()
        if not row:
            return
        params = json.loads(row[0] or "{}")
        params.update(items)
        self.conn.execute("UPDATE runs SET params_json=? WHERE run_id=?", (_j(params), run_id))
        self.conn.commit()

    def previous_run(self, run_id):
        """指定した実行の直前に開始した実行 (無ければ None)。引き継ぎの判定に使う。
        開始時刻は秒精度で同着がありうるため、挿入順 (rowid) で判定する。"""
        row = self.conn.execute(
            "SELECT * FROM runs WHERE rowid < "
            "(SELECT rowid FROM runs WHERE run_id=?) "
            "ORDER BY rowid DESC LIMIT 1", (run_id,)).fetchone()
        return dict(row) if row else None

    def run_sources(self, run_id):
        return [dict(r) for r in self.conn.execute("SELECT * FROM run_sources WHERE run_id=?", (run_id,)).fetchall()]

    def last_source_successes(self):
        """(source, query) ごとの最終成功日時 (run_sources の ok な記録と runs の開始時刻)。
        財務系ソースの取得間隔 (前回成功から一定期間は休む) の判定に使う。"""
        rows = self.conn.execute(
            "SELECT rs.source, rs.query, MAX(r.started_at) FROM run_sources rs "
            "JOIN runs r ON r.run_id = rs.run_id WHERE rs.ok = 1 "
            "GROUP BY rs.source, rs.query").fetchall()
        return {(s, q): t for s, q, t in rows}

    def last_source_attempts(self, source):
        """指定ソースについて (source, query) ごとの最終試行 (開始日時, ok) を返す。"""
        rows = self.conn.execute(
            "SELECT rs.query, r.started_at, rs.ok FROM run_sources rs "
            "JOIN runs r ON r.run_id = rs.run_id WHERE rs.source = ? "
            "ORDER BY r.started_at DESC, rs.rowid DESC", (source,)).fetchall()
        out = {}
        for q, t, ok in rows:
            out.setdefault((source, q), (t, bool(ok)))
        return out

    def latest_financial_periods(self, source):
        """企業ごとの最新の財務レコードの期末日 (published) を返す。"""
        rows = self.conn.execute(
            "SELECT ce.entity_id, MAX(c.published) FROM candidates c "
            "JOIN candidate_entities ce ON ce.candidate_id = c.id "
            "WHERE c.source = ? GROUP BY ce.entity_id", (source,)).fetchall()
        return {eid: p for eid, p in rows if p}

    def latest_financial_retrievals(self, source):
        """企業ごとの最新財務レコードを取得した日時 (retrieved_at) を返す。

        最新期間を取得した試行と、それより後の空振り試行を区別するために使う。
        """
        rows = self.conn.execute(
            "SELECT ce.entity_id, c.published, c.retrieved_at FROM candidates c "
            "JOIN candidate_entities ce ON ce.candidate_id = c.id "
            "WHERE c.source = ?", (source,)).fetchall()
        best = {}
        for eid, pub, got in rows:
            if not pub or not got:
                continue
            if eid not in best or pub > best[eid][0]:
                best[eid] = (pub, got)
        return {eid: got for eid, (pub, got) in best.items()}
