"""SQLite persistence for the harvester.

The published catalogue and the harvest watermark live in the same SQLite
database.  A harvest round writes into ``run_records`` (a per-run staging
area) and only the final commit transaction copies staging into ``catalog``
and advances the watermark.  This is what makes a failed or interrupted
round a no-op for readers of the published catalogue.
"""

from __future__ import annotations

import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from .subjects import Rule, classify_catalog, normalize_rule_input


class SubjectConflict(Exception):
    """Current rules disagree on a title's subject at the highest priority.

    Raised inside a commit transaction so the whole transaction is rolled
    back: neither catalogue rows, subjects nor the watermark may change while
    a conflict exists.
    """


# How many classified rows a preview payload carries back (conflicts are
# always reported in full).
PREVIEW_ROW_LIMIT = 500


def utc_now_millis() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


class Store:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            str(self.db_path), check_same_thread=False, isolation_level=None
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._init_schema()
        self._recover_interrupted_runs()
        self._ensure_invariants()

    # ------------------------------------------------------------------ schema
    def _init_schema(self) -> None:
        with self._lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT
                );

                CREATE TABLE IF NOT EXISTS published_state (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    watermark TEXT,
                    updated_at TEXT,
                    success_count INTEGER NOT NULL DEFAULT 0,
                    last_run_id TEXT
                );

                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY,
                    status TEXT NOT NULL CHECK (
                        status IN ('running', 'succeeded', 'failed', 'cancelled')
                    ),
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    window_from TEXT,
                    window_until TEXT,
                    pages_fetched INTEGER NOT NULL DEFAULT 0,
                    records_seen INTEGER NOT NULL DEFAULT 0,
                    token_restarts INTEGER NOT NULL DEFAULT 0,
                    attempt INTEGER NOT NULL DEFAULT 1,
                    complete_list_size INTEGER,
                    phase TEXT,
                    last_error TEXT,
                    cancel_reason TEXT,
                    watermark_after TEXT
                );

                CREATE TABLE IF NOT EXISTS run_records (
                    run_id TEXT NOT NULL,
                    identifier TEXT NOT NULL,
                    title TEXT,
                    creator TEXT,
                    datestamp TEXT NOT NULL,
                    deleted INTEGER NOT NULL DEFAULT 0,
                    seq INTEGER NOT NULL,
                    PRIMARY KEY (run_id, identifier),
                    FOREIGN KEY (run_id) REFERENCES runs(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS catalog (
                    identifier TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    creator TEXT,
                    source_datestamp TEXT NOT NULL,
                    harvested_at TEXT NOT NULL,
                    subject TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_catalog_title ON catalog(title);

                CREATE TABLE IF NOT EXISTS subject_rules (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    keyword TEXT NOT NULL,
                    subject TEXT NOT NULL,
                    priority INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_subject_rules_priority
                    ON subject_rules(priority DESC, id);

                INSERT OR IGNORE INTO published_state
                    (id, watermark, updated_at, success_count, last_run_id)
                VALUES (1, NULL, NULL, 0, NULL);

                -- Monotonic versions backing the preview/confirm optimistic
                -- concurrency check.  Every rule edit bumps rules_version;
                -- every successful catalogue publication (harvest commit or
                -- rule confirmation) bumps catalog_version.
                INSERT OR IGNORE INTO meta(key, value)
                VALUES ('subject_rules_version', '0');
                INSERT OR IGNORE INTO meta(key, value)
                VALUES ('catalog_version', '0');
                """
            )
            self._migrate_catalog_subject()

    def _migrate_catalog_subject(self) -> None:
        """Add the subject column to databases created before this feature."""
        with self._lock:
            columns = {
                row["name"]
                for row in self._conn.execute("PRAGMA table_info(catalog)").fetchall()
            }
            if "subject" not in columns:
                self._conn.execute("ALTER TABLE catalog ADD COLUMN subject TEXT")

    def _recover_interrupted_runs(self) -> None:
        """A previous process died.  No in-memory task can own these runs."""
        with self._lock:
            now = utc_now_millis()
            rows = self._conn.execute(
                "SELECT id FROM runs WHERE status = 'running'"
            ).fetchall()
            for row in rows:
                self._conn.execute(
                    """
                    UPDATE runs
                       SET status = 'failed',
                           finished_at = ?,
                           phase = 'failed',
                           last_error = COALESCE(
                               last_error,
                               '采集进程中断，本轮未提交（已发布目录与水位保持不变）'
                           )
                     WHERE id = ? AND status = 'running'
                    """,
                    (now, row["id"]),
                )
                self._conn.execute(
                    "DELETE FROM run_records WHERE run_id = ?", (row["id"],)
                )
            self._conn.execute(
                "DELETE FROM meta WHERE key = 'active_run_id'"
            )

    def _ensure_invariants(self) -> None:
        """At most one round may be ``running``, even across processes.

        Created after recovery so a legacy database with stray ``running``
        rows cannot make startup fail.
        """
        with self._lock:
            self._conn.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_runs_one_active
                    ON runs(status) WHERE status = 'running'
                """
            )

    def _safe_rollback(self) -> None:
        """Roll back if a transaction is active; ignore a missing one.

        Some error paths roll back deliberately and then re-raise into a
        generic handler that would otherwise issue a second ROLLBACK.
        """
        if self._conn.in_transaction:
            self._conn.execute("ROLLBACK")

    # ----------------------------------------------------------------- helpers
    def _meta_get(self, key: str) -> str | None:
        row = self._conn.execute(
            "SELECT value FROM meta WHERE key = ?", (key,)
        ).fetchone()
        return row["value"] if row else None

    def _meta_set(self, key: str, value: str | None) -> None:
        if value is None:
            self._conn.execute("DELETE FROM meta WHERE key = ?", (key,))
        else:
            self._conn.execute(
                """
                INSERT INTO meta(key, value) VALUES(?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (key, value),
            )

    # -------------------------------------------------------------------- runs
    def create_run(self) -> dict[str, Any]:
        with self._lock:
            run_id = uuid.uuid4().hex
            now = utc_now_millis()
            state = self._conn.execute(
                "SELECT watermark FROM published_state WHERE id = 1"
            ).fetchone()
            watermark = state["watermark"] if state else None
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.execute(
                    """
                    INSERT INTO runs
                        (id, status, started_at, window_from, phase)
                    VALUES (?, 'running', ?, ?, 'connecting')
                    """,
                    (run_id, now, watermark),
                )
                self._meta_set("active_run_id", run_id)
                self._conn.execute("COMMIT")
            except sqlite3.IntegrityError as exc:
                self._conn.execute("ROLLBACK")
                raise RuntimeError("已有采集轮次在运行") from exc
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            run = self.get_run(run_id)
            assert run is not None
            return run

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM runs WHERE id = ?", (run_id,)
            ).fetchone()
            if row is None:
                return None
            run = dict(row)
            staged = self._conn.execute(
                "SELECT COUNT(*) AS n FROM run_records WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            run["staged_count"] = int(staged["n"])
            return run

    def get_active_run(self) -> dict[str, Any] | None:
        with self._lock:
            run_id = self._meta_get("active_run_id")
            if run_id:
                run = self.get_run(run_id)
                if run and run["status"] == "running":
                    return run
            row = self._conn.execute(
                """
                SELECT * FROM runs WHERE status = 'running'
                 ORDER BY started_at DESC LIMIT 1
                """
            ).fetchone()
            return self.get_run(row["id"]) if row else None

    def list_runs(self, limit: int = 15) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM runs ORDER BY started_at DESC LIMIT ?", (limit,)
            ).fetchall()
            return [dict(r) for r in rows]

    def is_run_running(self, run_id: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM runs WHERE id = ? AND status = 'running'",
                (run_id,),
            ).fetchone()
            return row is not None

    def set_window(self, run_id: str, until: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                """
                UPDATE runs SET window_until = ?, phase = 'fetching'
                 WHERE id = ? AND status = 'running'
                """,
                (until, run_id),
            )
            return cur.rowcount > 0

    def update_progress(
        self,
        run_id: str,
        *,
        pages_fetched: int,
        records_seen: int,
        token_restarts: int,
        attempt: int,
        complete_list_size: int | None,
        phase: str | None = None,
    ) -> bool:
        with self._lock:
            if phase is None:
                cur = self._conn.execute(
                    """
                    UPDATE runs
                       SET pages_fetched = ?,
                           records_seen = ?,
                           token_restarts = ?,
                           attempt = ?,
                           complete_list_size = ?
                     WHERE id = ? AND status = 'running'
                    """,
                    (
                        pages_fetched,
                        records_seen,
                        token_restarts,
                        attempt,
                        complete_list_size,
                        run_id,
                    ),
                )
            else:
                cur = self._conn.execute(
                    """
                    UPDATE runs
                       SET pages_fetched = ?,
                           records_seen = ?,
                           token_restarts = ?,
                           attempt = ?,
                           complete_list_size = ?,
                           phase = ?
                     WHERE id = ? AND status = 'running'
                    """,
                    (
                        pages_fetched,
                        records_seen,
                        token_restarts,
                        attempt,
                        complete_list_size,
                        phase,
                        run_id,
                    ),
                )
            return cur.rowcount > 0

    def set_last_error(self, run_id: str, message: str) -> None:
        with self._lock:
            self._conn.execute(
                """
                UPDATE runs SET last_error = ?
                 WHERE id = ? AND status = 'running'
                """,
                (message[:4000], run_id),
            )

    def finish_cancelled(self, run_id: str, reason: str) -> bool:
        """Finalise a cancelled run; safe to call twice."""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                cur = self._conn.execute(
                    """
                    UPDATE runs
                       SET status = 'cancelled',
                           finished_at = COALESCE(finished_at, ?),
                           phase = 'cancelled',
                           cancel_reason = COALESCE(cancel_reason, ?)
                     WHERE id = ? AND status = 'running'
                    """,
                    (utc_now_millis(), reason[:1000], run_id),
                )
                changed = cur.rowcount > 0
                self._delete_stage(run_id)
                if changed:
                    self._clear_active_if(run_id)
                self._conn.execute("COMMIT")
                return changed
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise

    def finish_failed(self, run_id: str, error: str) -> bool:
        """Finalise a failed run.  Staging is discarded, published data not touched."""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                cur = self._conn.execute(
                    """
                    UPDATE runs
                       SET status = 'failed',
                           finished_at = COALESCE(finished_at, ?),
                           phase = 'failed',
                           last_error = ?
                     WHERE id = ? AND status = 'running'
                    """,
                    (utc_now_millis(), error[:4000], run_id),
                )
                changed = cur.rowcount > 0
                self._delete_stage(run_id)
                if changed:
                    self._clear_active_if(run_id)
                self._conn.execute("COMMIT")
                return changed
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise

    def reset_stage(self, run_id: str) -> bool:
        """Discard all pages collected for the current attempt."""
        with self._lock:
            if not self.is_run_running(run_id):
                return False
            self._conn.execute(
                "DELETE FROM run_records WHERE run_id = ?", (run_id,)
            )
            return True

    def stage_records(
        self, run_id: str, records: Sequence[dict[str, Any]], seq_start: int
    ) -> int:
        """Insert one page into staging, keeping one row per identifier.

        If the same identifier occurs again (whether because of a malformed
        source or a repeated page), the record with the newer OAI datestamp
        wins; the later observation wins a tie.
        """
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                if not self.is_run_running(run_id):
                    self._conn.execute("ROLLBACK")
                    return 0
                for offset, record in enumerate(records):
                    seq = seq_start + offset
                    self._conn.execute(
                        """
                        INSERT INTO run_records
                            (run_id, identifier, title, creator, datestamp,
                             deleted, seq)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(run_id, identifier) DO UPDATE SET
                            title = excluded.title,
                            creator = excluded.creator,
                            datestamp = excluded.datestamp,
                            deleted = excluded.deleted,
                            seq = excluded.seq
                        WHERE excluded.datestamp > run_records.datestamp
                           OR (excluded.datestamp = run_records.datestamp
                               AND excluded.seq > run_records.seq)
                        """,
                        (
                            run_id,
                            record["identifier"],
                            record.get("title"),
                            record.get("creator"),
                            record["datestamp"],
                            1 if record.get("deleted") else 0,
                            seq,
                        ),
                    )
                self._conn.execute("COMMIT")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            return len(records)

    def _delete_stage(self, run_id: str) -> None:
        self._conn.execute("DELETE FROM run_records WHERE run_id = ?", (run_id,))

    def _clear_active_if(self, run_id: str) -> None:
        if self._meta_get("active_run_id") == run_id:
            self._meta_set("active_run_id", None)

    def commit_run(self, run_id: str) -> dict[str, Any]:
        """Atomically publish staging and advance the watermark.

        The check for ownership (``active_run_id``) happens inside the same
        IMMEDIATE transaction as the catalogue writes.  A cancelled or
        superseded round can therefore never publish, even if its HTTP
        response arrives late.
        """
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                run = self._conn.execute(
                    "SELECT * FROM runs WHERE id = ?", (run_id,)
                ).fetchone()
                active = self._meta_get("active_run_id")
                if (
                    run is None
                    or run["status"] != "running"
                    or active != run_id
                    or not run["window_until"]
                ):
                    self._conn.execute("ROLLBACK")
                    return {"committed": False, "reason": "run is not active"}

                rows = self._conn.execute(
                    """
                    SELECT identifier, title, creator, datestamp, deleted
                      FROM run_records WHERE run_id = ?
                     ORDER BY seq
                    """,
                    (run_id,),
                ).fetchall()

                now = utc_now_millis()
                upserts = 0
                deletions = 0
                touched_identifiers: list[str] = []
                for row in rows:
                    if row["deleted"]:
                        self._conn.execute(
                            "DELETE FROM catalog WHERE identifier = ?",
                            (row["identifier"],),
                        )
                        deletions += 1
                    else:
                        self._conn.execute(
                            """
                            INSERT INTO catalog
                                (identifier, title, creator, source_datestamp,
                                 harvested_at, subject)
                            VALUES (?, ?, ?, ?, ?, NULL)
                            ON CONFLICT(identifier) DO UPDATE SET
                                title = excluded.title,
                                creator = excluded.creator,
                                source_datestamp = excluded.source_datestamp,
                                harvested_at = excluded.harvested_at
                            """,
                            (
                                row["identifier"],
                                row["title"] or "",
                                row["creator"],
                                row["datestamp"],
                                now,
                            ),
                        )
                        upserts += 1
                        touched_identifiers.append(row["identifier"])

                # Subjects are decided by the rules in force at commit time.
                # A disagreement at the highest priority fails the whole
                # round: roll back so catalogue, subjects and watermark stay
                # exactly as published.
                rules = self._rules_as_objects()
                conflicts = self._apply_subjects_locked(
                    rules, identifiers=touched_identifiers
                )
                if conflicts:
                    self._safe_rollback()
                    raise SubjectConflict(conflicts)

                watermark = run["window_until"]
                new_catalog_version = self._bump_meta("catalog_version")
                self._conn.execute(
                    """
                    UPDATE published_state
                       SET watermark = ?,
                           updated_at = ?,
                           success_count = success_count + 1,
                           last_run_id = ?
                     WHERE id = 1
                    """,
                    (watermark, now, run_id),
                )
                self._conn.execute(
                    """
                    UPDATE runs
                       SET status = 'succeeded',
                           finished_at = ?,
                           phase = 'published',
                           watermark_after = ?,
                           last_error = NULL
                     WHERE id = ?
                    """,
                    (now, watermark, run_id),
                )
                self._clear_active_if(run_id)
                self._delete_stage(run_id)
                self._conn.execute("COMMIT")
                return {
                    "committed": True,
                    "upserts": upserts,
                    "deletions": deletions,
                    "watermark": watermark,
                }
            except BaseException:
                self._safe_rollback()
                raise

    def fail_run(self, run_id: str, error: str) -> bool:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                cur = self._conn.execute(
                    """
                    UPDATE runs
                       SET status = 'failed',
                           finished_at = ?,
                           phase = 'failed',
                           last_error = ?
                     WHERE id = ? AND status = 'running'
                    """,
                    (utc_now_millis(), error[:4000], run_id),
                )
                self._delete_stage(run_id)
                self._clear_active_if(run_id)
                self._conn.execute("COMMIT")
                return cur.rowcount > 0
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise

    def cancel_run(self, run_id: str, reason: str) -> bool:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                cur = self._conn.execute(
                    """
                    UPDATE runs
                       SET status = 'cancelled',
                           finished_at = ?,
                           phase = 'cancelled',
                           cancel_reason = ?
                     WHERE id = ? AND status = 'running'
                    """,
                    (utc_now_millis(), reason[:1000], run_id),
                )
                self._delete_stage(run_id)
                self._clear_active_if(run_id)
                self._conn.execute("COMMIT")
                return cur.rowcount > 0
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise

    # --------------------------------------------------------------- published
    def published_state(self) -> dict[str, Any]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM published_state WHERE id = 1"
            ).fetchone()
            result = dict(row) if row else {
                "watermark": None,
                "updated_at": None,
                "success_count": 0,
                "last_run_id": None,
            }
            count = self._conn.execute(
                "SELECT COUNT(*) AS n FROM catalog"
            ).fetchone()
            result["catalog_size"] = int(count["n"])
            return result

    def catalog_page(
        self, query: str = "", page: int = 1, page_size: int = 25
    ) -> dict[str, Any]:
        query = (query or "").strip()
        page = max(1, int(page or 1))
        page_size = min(200, max(1, int(page_size or 25)))
        like = f"%{query}%"
        where = ""
        params: list[Any] = []
        if query:
            where = (
                "WHERE identifier LIKE ? OR title LIKE ? "
                "OR COALESCE(creator, '') LIKE ?"
            )
            params = [like, like, like]
        with self._lock:
            total = self._conn.execute(
                f"SELECT COUNT(*) AS n FROM catalog {where}", params
            ).fetchone()["n"]
            total = int(total)
            pages = max(1, (total + page_size - 1) // page_size)
            if page > pages:
                page = pages
            rows = self._conn.execute(
                f"""
                SELECT identifier, title, creator, source_datestamp,
                       harvested_at, subject
                  FROM catalog {where}
                 ORDER BY title COLLATE NOCASE, identifier
                 LIMIT ? OFFSET ?
                """,
                [*params, page_size, (page - 1) * page_size],
            ).fetchall()
            return {
                "records": [dict(r) for r in rows],
                "total": total,
                "page": page,
                "page_size": page_size,
                "pages": pages,
                "query": query,
            }

    # ------------------------------------------------------------- subject rules
    def _meta_int(self, key: str) -> int:
        value = self._meta_get(key)
        try:
            return int(value) if value is not None else 0
        except (TypeError, ValueError):
            return 0

    def _bump_meta(self, key: str) -> int:
        value = self._meta_int(key) + 1
        self._meta_set(key, str(value))
        return value

    def subject_rules_version(self) -> int:
        with self._lock:
            return self._meta_int("subject_rules_version")

    def catalog_version(self) -> int:
        with self._lock:
            return self._meta_int("catalog_version")

    def list_subject_rules(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT id, keyword, subject, priority, created_at, updated_at
                  FROM subject_rules
                 ORDER BY priority DESC, id
                """
            ).fetchall()
            return [dict(r) for r in rows]

    def _rules_as_objects(self) -> list[Rule]:
        rows = self._conn.execute(
            "SELECT id, keyword, subject, priority FROM subject_rules"
        ).fetchall()
        return [
            Rule(
                id=row["id"],
                keyword=row["keyword"],
                subject=row["subject"],
                priority=row["priority"],
            )
            for row in rows
        ]

    def upsert_subject_rule(
        self, keyword: str, subject: str, priority: Any, rule_id: int | None = None
    ) -> dict[str, Any]:
        """Create or update one rule.  Every edit bumps the rules version."""
        keyword, subject, priority = normalize_rule_input(keyword, subject, priority)
        now = utc_now_millis()
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                if rule_id is not None:
                    cur = self._conn.execute(
                        """
                        UPDATE subject_rules
                           SET keyword = ?, subject = ?, priority = ?,
                               updated_at = ?
                         WHERE id = ?
                        """,
                        (keyword, subject, priority, now, rule_id),
                    )
                    if cur.rowcount == 0:
                        raise KeyError(rule_id)
                    new_id = rule_id
                else:
                    cur = self._conn.execute(
                        """
                        INSERT INTO subject_rules
                            (keyword, subject, priority, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (keyword, subject, priority, now, now),
                    )
                    new_id = int(cur.lastrowid)
                self._bump_meta("subject_rules_version")
                self._conn.execute("COMMIT")
            except BaseException:
                self._safe_rollback()
                raise
            row = self._conn.execute(
                "SELECT * FROM subject_rules WHERE id = ?", (new_id,)
            ).fetchone()
            return dict(row)

    def delete_subject_rule(self, rule_id: int) -> bool:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                cur = self._conn.execute(
                    "DELETE FROM subject_rules WHERE id = ?", (rule_id,)
                )
                changed = cur.rowcount > 0
                if changed:
                    self._bump_meta("subject_rules_version")
                self._conn.execute("COMMIT")
                return changed
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise

    def preview_subject_rules(
        self, *, limit: int = PREVIEW_ROW_LIMIT
    ) -> dict[str, Any]:
        """Dry-run the rules currently in force against the published catalogue.

        Nothing is written.  Conflicts are reported in full; the row preview
        is capped at ``limit`` rows (conflict rows first, then ordinary rows).
        """
        with self._lock:
            rules = self._rules_as_objects()
            rows = self._conn.execute(
                """
                SELECT identifier, title, creator, source_datestamp,
                       harvested_at, subject AS current_subject
                  FROM catalog
                 ORDER BY title COLLATE NOCASE, identifier
                """
            ).fetchall()
            classified = classify_catalog([dict(r) for r in rows], rules)

        conflicts = [row for row in classified if row["conflict"]]
        changed = [
            row
            for row in classified
            if not row["conflict"]
            and (row["subject"] or None) != (row.get("current_subject") or None)
        ]
        assigned = [row for row in classified if row["subject"]]
        conflicts.sort(key=lambda r: r["title"])
        changed.sort(key=lambda r: r["title"])
        preview_rows = (conflicts + changed)[:limit]
        return {
            "rules_version": self.subject_rules_version(),
            "catalog_version": self.catalog_version(),
            "rules": self.list_subject_rules(),
            "catalog_size": len(classified),
            "conflict_count": len(conflicts),
            "would_change_count": len(changed),
            "assigned_count": len(assigned),
            "conflicts": conflicts,
            "changes": [
                {
                    "identifier": row["identifier"],
                    "title": row["title"],
                    "current_subject": row.get("current_subject"),
                    "subject": row["subject"],
                    "matched_rule_ids": row["matched_rule_ids"],
                }
                for row in changed
            ],
            "preview_rows": preview_rows,
        }

    def _apply_subjects_locked(
        self, rules: list[Rule], *, identifiers: Sequence[str] | None = None
    ) -> list[dict[str, Any]]:
        """Recompute subjects inside an already-open IMMEDIATE transaction.

        When ``identifiers`` is given only those catalogue rows are
        reclassified (used by a harvest commit, which has just upserted or
        deleted them); otherwise the whole catalogue is reclassified (rule
        confirmation).  Returns the conflict rows.  On conflict the caller is
        expected to roll the outer transaction back.
        """
        if identifiers is not None:
            if not identifiers:
                # An empty window (no upserts, no deletions) has nothing to
                # reclassify; the watermark still advances in the caller.
                return []
            placeholders = ",".join("?" for _ in identifiers)
            sql = (
                "SELECT identifier, title FROM catalog "
                f"WHERE identifier IN ({placeholders})"
            )
            rows = self._conn.execute(sql, list(identifiers)).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT identifier, title FROM catalog"
            ).fetchall()
        classified = classify_catalog([dict(r) for r in rows], rules)
        conflicts = [row for row in classified if row["conflict"]]
        if conflicts:
            return conflicts
        for row in classified:
            self._conn.execute(
                "UPDATE catalog SET subject = ? WHERE identifier = ?",
                (row["subject"], row["identifier"]),
            )
        return []

    def confirm_subject_rules(
        self, *, rules_version: int, catalog_version: int
    ) -> dict[str, Any]:
        """Publish the subjects implied by the current rules.

        Optimistic concurrency: a confirmation is only accepted when neither
        the rules nor the catalogue has changed since the preview the user is
        confirming.  A conflict also rejects the confirmation without touching
        anything.  On success the catalogue version advances.
        """
        with self._lock:
            current_rules_version = self._meta_int("subject_rules_version")
            current_catalog_version = self._meta_int("catalog_version")
            if (
                current_rules_version != rules_version
                or current_catalog_version != catalog_version
            ):
                return {
                    "confirmed": False,
                    "reason": "stale",
                    "rules_version": current_rules_version,
                    "catalog_version": current_catalog_version,
                }
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                rules = self._rules_as_objects()
                conflicts = self._apply_subjects_locked(rules)
                if conflicts:
                    self._conn.execute("ROLLBACK")
                    return {
                        "confirmed": False,
                        "reason": "conflict",
                        "conflicts": conflicts,
                        "rules_version": current_rules_version,
                        "catalog_version": current_catalog_version,
                    }
                new_catalog_version = self._bump_meta("catalog_version")
                self._conn.execute(
                    "UPDATE published_state SET updated_at = ? WHERE id = 1",
                    (utc_now_millis(),),
                )
                self._conn.execute("COMMIT")
            except BaseException:
                self._safe_rollback()
                raise
            return {
                "confirmed": True,
                "catalog_version": new_catalog_version,
                "rules_version": current_rules_version,
            }

    def close(self) -> None:
        with self._lock:
            self._conn.close()
