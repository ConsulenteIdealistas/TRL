"""SQLite による永続化。重複排除は identifiers テーブルの安定IDで行う。"""

import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path


SCHEMA = """
CREATE TABLE IF NOT EXISTS candidates(
  id TEXT PRIMARY KEY, title TEXT, abstract TEXT, url TEXT, source TEXT, source_id TEXT,
  doi TEXT, published TEXT, retrieved_at TEXT, fields_json TEXT, info_type TEXT,
  status TEXT NOT NULL DEFAULT 'new', screen_reason TEXT, last_eval_at TEXT,
  retry_count INTEGER NOT NULL DEFAULT 0, first_seen_run TEXT,
  title_ja TEXT, abstract_ja TEXT, translation_src TEXT, media_url TEXT);
CREATE TABLE IF NOT EXISTS identifiers(key TEXT PRIMARY KEY, candidate_id TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS evaluations(
  id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, candidate_id TEXT, kind TEXT, as_of TEXT,
  evaluated_at TEXT, status TEXT, total REAL, band TEXT, items_json TEXT, extraction_json TEXT,
  event_json TEXT, prompt_versions_json TEXT, config_version TEXT, config_hash TEXT,
  llm_provider TEXT, llm_model TEXT, input_hash TEXT, errors_json TEXT);
CREATE TABLE IF NOT EXISTS extraction_cache(
  input_hash TEXT, prompt_sha TEXT, model TEXT, extraction_json TEXT, meta_json TEXT, created_at TEXT,
  PRIMARY KEY(input_hash, prompt_sha, model));
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
"""


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _j(x):
    return json.dumps(x, ensure_ascii=False, sort_keys=True)


class Store:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path))
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
        for col in ("title_ja", "abstract_ja", "translation_src", "material_updated_at", "media_url"):
            if col not in ccols:
                self.conn.execute(f"ALTER TABLE candidates ADD COLUMN {col} TEXT")
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

    # --- candidates ---
    def upsert_candidate(self, rec, run_id=None):
        """-> (candidate_id, is_new)。既存IDに一致すれば統合し、欠けた項目だけ補う。"""
        c = self.conn
        existing = None
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
            if rec.get("source") == "trials" and rec.get("abstract") and cur["abstract"] != rec["abstract"]:
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
        q = ",".join("?" * len(statuses))
        rows = self.conn.execute(f"SELECT id FROM candidates WHERE status IN ({q})", statuses).fetchall()
        return [self.get_candidate(r[0]) for r in rows]

    def set_status(self, cid, status, reason=None, eval_at=None, retry_inc=False):
        self.conn.execute(
            "UPDATE candidates SET status=?, screen_reason=COALESCE(?,screen_reason), "
            "last_eval_at=COALESCE(?,last_eval_at), retry_count=retry_count+? WHERE id=?",
            (status, reason, eval_at, 1 if retry_inc else 0, cid))
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
                        "WHERE status IN ('hold','error','final')")
        c.commit()
        return {"evaluations": n_eval, "queue": n_queue, "candidates_reset": cur.rowcount}

    def related_news(self, cid, as_of, limit=8, window_days=90):
        """同一企業に紐付く直近ニュースを、評価時の補助根拠として返す。

        ニュースは単体では評価せず、論文・治験などの評価に同梱する材料と位置付ける。
        """
        start = (as_of - timedelta(days=window_days)).isoformat()
        rows = self.conn.execute(
            "SELECT DISTINCT n.title, n.published, n.url, n.fields_json, n.source, n.media_url FROM candidates n "
            "JOIN candidate_entities ne ON ne.candidate_id = n.id "
            "JOIN candidate_entities ce ON ce.entity_id = ne.entity_id "
            "WHERE ce.candidate_id = ? AND n.id != ? AND n.info_type = 'news' "
            "AND n.published IS NOT NULL AND n.published <= ? AND n.published >= ? "
            "ORDER BY n.published DESC LIMIT ?",
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
        rows = self.conn.execute(
            "SELECT DISTINCT n.title, n.published, n.url, n.abstract FROM candidates n "
            "JOIN candidate_entities ne ON ne.candidate_id = n.id "
            "JOIN candidate_entities ce ON ce.entity_id = ne.entity_id "
            "WHERE ce.candidate_id = ? AND n.id != ? AND n.info_type = 'disclosure' "
            "AND n.abstract LIKE '%財務数値(%' "
            "AND n.published IS NOT NULL AND n.published <= ? AND n.published >= ? "
            "ORDER BY n.published DESC LIMIT ?",
            (cid, cid, as_of.isoformat(), start, limit)).fetchall()
        return [{"title": r["title"], "published": r["published"], "url": r["url"],
                 "text": r["abstract"]} for r in rows]

    # --- evaluations ---
    def add_evaluation(self, run_id, cid, ev):
        cur = self.conn.execute(
            "INSERT INTO evaluations(run_id,candidate_id,kind,as_of,evaluated_at,status,total,band,items_json,"
            "extraction_json,event_json,prompt_versions_json,config_version,config_hash,llm_provider,llm_model,"
            "input_hash,errors_json,extra_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, cid, ev["kind"], ev["as_of"], now_iso(), ev["status"], ev["total"],
             (ev["band"] or {}).get("label"), _j(ev["items"]), _j(ev.get("extraction")), _j(ev.get("event")),
             _j(ev["prompt_versions"]), ev["config_version"], ev["config_hash"], ev["llm_provider"],
             ev["llm_model"], ev["input_hash"], _j(ev["errors"]), _j(ev.get("extra") or {})))
        self.conn.commit()
        return cur.lastrowid

    def get_extraction(self, input_hash, prompt_sha, model):
        r = self.conn.execute("SELECT extraction_json, meta_json FROM extraction_cache "
                              "WHERE input_hash=? AND prompt_sha=? AND model=?", (input_hash, prompt_sha, model)).fetchone()
        return (json.loads(r[0]), json.loads(r[1])) if r else (None, None)

    def put_extraction(self, input_hash, prompt_sha, model, extraction, meta):
        self.conn.execute("INSERT OR REPLACE INTO extraction_cache VALUES(?,?,?,?,?,?)",
                          (input_hash, prompt_sha, model, _j(extraction), _j(meta), now_iso()))
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

    def get_run(self, run_id):
        r = self.conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        return dict(r) if r else None

    def run_sources(self, run_id):
        return [dict(r) for r in self.conn.execute("SELECT * FROM run_sources WHERE run_id=?", (run_id,)).fetchall()]
