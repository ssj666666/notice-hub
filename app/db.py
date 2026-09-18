"""SQLite 存储层。单文件、零运维，够这个规模用。"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

CST = timezone(timedelta(hours=8))

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    uid           TEXT    NOT NULL UNIQUE,
    source        TEXT    NOT NULL,
    source_type   TEXT    NOT NULL,
    title         TEXT    NOT NULL,
    url           TEXT,
    content       TEXT,
    published_at  TEXT,
    fetched_at    TEXT    NOT NULL,
    importance    INTEGER NOT NULL DEFAULT 2,
    category      TEXT,
    topic         TEXT,
    summary       TEXT,
    deadline      TEXT,
    audience      TEXT,
    action        TEXT,
    analysis      TEXT,
    matched       TEXT,
    dropped       INTEGER NOT NULL DEFAULT 0,
    is_baseline   INTEGER NOT NULL DEFAULT 0,
    is_stale      INTEGER NOT NULL DEFAULT 0,
    pushed        INTEGER NOT NULL DEFAULT 0,
    pushed_at     TEXT,
    is_read       INTEGER NOT NULL DEFAULT 0,
    starred       INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_items_fetched   ON items(fetched_at DESC);
CREATE INDEX IF NOT EXISTS idx_items_importance ON items(importance DESC);
CREATE INDEX IF NOT EXISTS idx_items_unread    ON items(is_read, dropped);

CREATE TABLE IF NOT EXISTS snapshots (
    key        TEXT PRIMARY KEY,
    payload    TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  TEXT,
    finished_at TEXT,
    collected   INTEGER DEFAULT 0,
    new_items   INTEGER DEFAULT 0,
    pushed      INTEGER DEFAULT 0,
    llm_calls   INTEGER DEFAULT 0,
    notes       TEXT
);
"""


def now_iso() -> str:
    return datetime.now(CST).isoformat(timespec="seconds")


def today_prefix() -> str:
    return datetime.now(CST).strftime("%Y-%m-%d")


class Database:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._migrate()
            self._conn.commit()

    def _migrate(self) -> None:
        """给旧库补上后加的列，避免升级时炸掉。"""
        wanted = {
            "is_baseline": "INTEGER NOT NULL DEFAULT 0",
            "is_stale": "INTEGER NOT NULL DEFAULT 0",
            "category": "TEXT",
        }
        existing = {row[1] for row in self._conn.execute("PRAGMA table_info(items)")}
        for column, decl in wanted.items():
            if column not in existing:
                self._conn.execute(f"ALTER TABLE items ADD COLUMN {column} {decl}")

    # ---------- 基础 ----------
    def _exec(self, sql: str, params: Iterable = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._conn.execute(sql, tuple(params))
            self._conn.commit()
            return cur

    def _query(self, sql: str, params: Iterable = ()) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._conn.execute(sql, tuple(params)).fetchall())

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ---------- items ----------
    def insert_item(self, item: dict) -> int | None:
        """返回新行 id；若已存在（去重）返回 None。"""
        try:
            cur = self._exec(
                """INSERT INTO items
                   (uid, source, source_type, title, url, content, published_at,
                    fetched_at, importance, category, analysis, matched, dropped,
                    is_baseline, is_stale)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    item["uid"], item["source"], item["source_type"], item["title"],
                    item.get("url", ""), item.get("content", "")[:20000],
                    item.get("published_at", ""), now_iso(),
                    item.get("importance", 2), item.get("category", ""),
                    item.get("analysis", "prescreen"),
                    json.dumps(item.get("matched", []), ensure_ascii=False),
                    1 if item.get("dropped") else 0,
                    1 if item.get("is_baseline") else 0,
                    1 if item.get("is_stale") else 0,
                ),
            )
            return int(cur.lastrowid)
        except sqlite3.IntegrityError:
            return None

    def update_analysis(self, item_id: int, result: dict) -> None:
        self._exec(
            """UPDATE items SET importance=?, category=?, topic=?, summary=?, deadline=?,
                   audience=?, action=?, analysis=? WHERE id=?""",
            (
                result.get("importance", 2), result.get("category", ""),
                result.get("topic", ""),
                result.get("summary", ""), result.get("deadline", ""),
                result.get("audience", ""), result.get("action", ""),
                result.get("analysis", "llm"), item_id,
            ),
        )

    def mark_pushed(self, item_id: int) -> None:
        self._exec("UPDATE items SET pushed=1, pushed_at=? WHERE id=?", (now_iso(), item_id))

    def set_flag(self, item_id: int, flag: str, value: bool) -> None:
        if flag not in ("is_read", "starred"):
            raise ValueError(f"bad flag: {flag}")
        self._exec(f"UPDATE items SET {flag}=? WHERE id=?", (1 if value else 0, item_id))

    def mark_all_read(self) -> None:
        self._exec("UPDATE items SET is_read=1 WHERE is_read=0")

    def list_items(self, limit: int = 100, min_importance: int = 1,
                   unread_only: bool = False, include_dropped: bool = False,
                   starred_only: bool = False, q: str = "",
                   category: str = "", include_stale: bool = False,
                   sort: str = "importance") -> list[dict]:
        where, params = [], []
        where.append("importance >= ?")
        params.append(int(min_importance))
        if not include_dropped:
            where.append("dropped = 0")
        if not include_stale:
            where.append("is_stale = 0")
        if unread_only:
            where.append("is_read = 0")
        if starred_only:
            where.append("starred = 1")
        if category:
            where.append("category = ?")
            params.append(category)
        if q:
            where.append("(title LIKE ? OR content LIKE ? OR summary LIKE ?)")
            like = f"%{q}%"
            params += [like, like, like]

        # 排序：全部通知按重要度，细分栏目按时间
        if sort == "time":
            order = "COALESCE(NULLIF(published_at,''), fetched_at) DESC, importance DESC"
        else:
            order = ("importance DESC, "
                     "COALESCE(NULLIF(published_at,''), fetched_at) DESC")

        sql = f"SELECT * FROM items WHERE {' AND '.join(where)} ORDER BY {order} LIMIT ?"
        params.append(int(limit))
        return [dict(r) for r in self._query(sql, params)]

    def category_counts(self, min_importance: int = 1,
                        unread_only: bool = False,
                        include_stale: bool = True) -> list[dict]:
        """按分类统计，供看板的分组/筛选用。"""
        where = ["dropped = 0", "importance >= ?"]
        params: list = [int(min_importance)]
        if not include_stale:
            where.append("is_stale = 0")
        if unread_only:
            where.append("is_read = 0")
        rows = self._query(
            f"""SELECT COALESCE(NULLIF(category,''), '其他') AS category,
                       COUNT(*) AS n,
                       SUM(CASE WHEN is_read=0 THEN 1 ELSE 0 END) AS unread,
                       SUM(CASE WHEN importance>=4 THEN 1 ELSE 0 END) AS important
                FROM items WHERE {' AND '.join(where)}
                GROUP BY 1 ORDER BY n DESC""",
            params,
        )
        return [dict(r) for r in rows]

    def get_item(self, item_id: int) -> dict | None:
        rows = self._query("SELECT * FROM items WHERE id=?", (item_id,))
        return dict(rows[0]) if rows else None

    def update_verdict(self, item_id: int, importance: int, category: str,
                       analysis: str, dropped: bool, is_stale: bool) -> None:
        """重算用：按当前配置覆写判定结果。"""
        self._exec(
            "UPDATE items SET importance=?, category=?, analysis=?, dropped=?, is_stale=? "
            "WHERE id=?",
            (int(importance), category, analysis, 1 if dropped else 0,
             1 if is_stale else 0, int(item_id)),
        )

    def all_items(self, limit: int = 200000) -> list[dict]:
        return [dict(r) for r in self._query(
            "SELECT * FROM items ORDER BY id LIMIT ?", (int(limit),))]

    def candidates_for_push(self, min_importance: int) -> list[dict]:
        """未被推送、未丢弃、非基线、达到门槛的，按重要度与时间排序。

        is_baseline=1 的是"首次接入某个源时已有的历史条目"，
        留在看板里可查，但绝不该当成新通知推给你。
        """
        rows = self._query(
            """SELECT * FROM items
               WHERE pushed=0 AND dropped=0 AND is_baseline=0 AND is_stale=0
                     AND importance >= ?
               ORDER BY importance DESC, COALESCE(NULLIF(published_at,''), fetched_at) ASC""",
            (int(min_importance),),
        )
        return [dict(r) for r in rows]

    # ---------- 每个源的抓取节奏 ----------
    def last_fetch(self, source: str) -> str:
        rows = self._query("SELECT payload FROM snapshots WHERE key=?",
                           (f"lastfetch::{source}",))
        return rows[0]["payload"] if rows else ""

    def mark_fetched(self, source: str) -> None:
        self.set_snapshot(f"lastfetch::{source}", now_iso())

    def pushed_count_today(self) -> int:
        rows = self._query("SELECT COUNT(*) AS n FROM items WHERE pushed=1 AND pushed_at LIKE ?",
                           (f"{today_prefix()}%",))
        return int(rows[0]["n"]) if rows else 0

    def stats(self) -> dict:
        total = self._query("SELECT COUNT(*) AS n FROM items")[0]["n"]
        unread = self._query(
            "SELECT COUNT(*) AS n FROM items WHERE is_read=0 AND dropped=0 AND is_stale=0"
        )[0]["n"]
        high = self._query(
            "SELECT COUNT(*) AS n FROM items WHERE is_read=0 AND dropped=0 "
            "AND is_stale=0 AND importance>=4"
        )[0]["n"]
        today = self._query("SELECT COUNT(*) AS n FROM items WHERE fetched_at LIKE ?",
                            (f"{today_prefix()}%",))[0]["n"]
        stale = self._query("SELECT COUNT(*) AS n FROM items WHERE is_stale=1")[0]["n"]
        fresh = self._query(
            "SELECT COUNT(*) AS n FROM items WHERE is_stale=0 AND dropped=0"
        )[0]["n"]
        today_published = self._query(
            "SELECT COUNT(*) AS n FROM items WHERE published_at = ? AND dropped=0",
            (datetime.now(CST).date().isoformat(),),
        )[0]["n"]
        last = self._query("SELECT * FROM runs ORDER BY id DESC LIMIT 1")
        return {
            "total": total,
            "unread": unread,
            "high_unread": high,
            "today": today,
            "pushed_today": self.pushed_count_today(),
            "stale": stale,
            "fresh": fresh,
            "published_today": today_published,
            "last_run": dict(last[0]) if last else None,
        }

    # ---------- snapshots（网页变化检测） ----------
    def get_snapshot(self, key: str) -> str | None:
        rows = self._query("SELECT payload FROM snapshots WHERE key=?", (key,))
        return rows[0]["payload"] if rows else None

    def set_snapshot(self, key: str, payload: str) -> None:
        self._exec(
            "INSERT INTO snapshots(key, payload, updated_at) VALUES(?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET payload=excluded.payload, updated_at=excluded.updated_at",
            (key, payload, now_iso()),
        )

    # ---------- runs ----------
    def start_run(self) -> int:
        cur = self._exec("INSERT INTO runs(started_at) VALUES(?)", (now_iso(),))
        return int(cur.lastrowid)

    def finish_run(self, run_id: int, collected: int, new_items: int,
                   pushed: int, llm_calls: int, notes: str = "") -> None:
        self._exec(
            """UPDATE runs SET finished_at=?, collected=?, new_items=?, pushed=?,
                   llm_calls=?, notes=? WHERE id=?""",
            (now_iso(), collected, new_items, pushed, llm_calls, notes[:2000], run_id),
        )

    def recent_runs(self, limit: int = 10) -> list[dict]:
        return [dict(r) for r in
                self._query("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,))]

    def source_stats(self) -> list[dict]:
        """每个源的抓取情况。用来回答"某个站到底有没有东西"。"""
        rows = self._query(
            """SELECT source,
                      COUNT(*)                                                 AS total,
                      SUM(CASE WHEN is_stale=0 AND dropped=0 THEN 1 ELSE 0 END) AS visible,
                      SUM(CASE WHEN dropped=1 THEN 1 ELSE 0 END)               AS dropped,
                      SUM(CASE WHEN category IS NULL OR category='' THEN 1 ELSE 0 END)
                                                                               AS uncategorized,
                      MAX(NULLIF(published_at,''))                             AS newest,
                      MAX(fetched_at)                                          AS last_seen
               FROM items GROUP BY source ORDER BY source"""
        )
        out = []
        for r in rows:
            row = dict(r)
            row["last_fetch"] = self.last_fetch(row["source"])
            newest = row.get("newest") or ""
            age = None
            if newest:
                try:
                    age = (datetime.now(CST).date()
                           - datetime.fromisoformat(newest).date()).days
                except ValueError:
                    age = None
            row["newest_age_days"] = age
            out.append(row)
        return out
