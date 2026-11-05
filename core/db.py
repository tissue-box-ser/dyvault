"""SQLite 持久化：任务、作品记录、日志、设置。"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    title       TEXT NOT NULL,
    sec_uid     TEXT NOT NULL,
    nickname    TEXT,
    source      TEXT NOT NULL DEFAULT 'post',
    options     TEXT NOT NULL DEFAULT '{}',
    status      TEXT NOT NULL DEFAULT 'pending',
    total       INTEGER DEFAULT 0,
    done        INTEGER DEFAULT 0,
    failed      INTEGER DEFAULT 0,
    comments    INTEGER DEFAULT 0,
    bytes       INTEGER DEFAULT 0,
    dir         TEXT,
    error       TEXT,
    created_at  INTEGER,
    updated_at  INTEGER
);

CREATE TABLE IF NOT EXISTS awemes (
    job_id      INTEGER NOT NULL,
    aweme_id    TEXT NOT NULL,
    desc        TEXT,
    create_time INTEGER,
    digg        INTEGER DEFAULT 0,
    comment     INTEGER DEFAULT 0,
    collect     INTEGER DEFAULT 0,
    share       INTEGER DEFAULT 0,
    media_type  TEXT,
    status      TEXT DEFAULT 'pending',
    files       TEXT,
    media       TEXT,
    error       TEXT,
    PRIMARY KEY (job_id, aweme_id)
);

CREATE TABLE IF NOT EXISTS cookies (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    label       TEXT NOT NULL,
    value       TEXT NOT NULL,
    enabled     INTEGER DEFAULT 1,
    status      TEXT DEFAULT 'unknown',
    note        TEXT,
    last_used   INTEGER DEFAULT 0,
    use_count   INTEGER DEFAULT 0,
    created_at  INTEGER
);

CREATE TABLE IF NOT EXISTS logs (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id  INTEGER,
    ts      INTEGER,
    level   TEXT,
    message TEXT
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE INDEX IF NOT EXISTS idx_awemes_job ON awemes(job_id, status);
CREATE INDEX IF NOT EXISTS idx_logs_ts ON logs(id DESC);
"""


class Database:
    def __init__(self, path: str | Path):
        self.path = str(Path(path).expanduser())
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        with self.conn() as c:
            c.executescript(SCHEMA)
            columns = {row["name"] for row in c.execute("PRAGMA table_info(awemes)")}
            if "media" not in columns:
                c.execute("ALTER TABLE awemes ADD COLUMN media TEXT")
            c.commit()

    def conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=30.0, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA busy_timeout=30000")
            self._local.conn = conn
        return conn

    # ------------------------------------------------------------ settings

    def get_setting(self, key: str, default: str = "") -> str:
        row = self.conn().execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set_setting(self, key: str, value: str) -> None:
        conn = self.conn()
        conn.execute(
            "INSERT INTO settings(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        conn.commit()

    def all_settings(self) -> Dict[str, str]:
        rows = self.conn().execute("SELECT key, value FROM settings").fetchall()
        return {r["key"]: r["value"] for r in rows}

    # ---------------------------------------------------------------- jobs

    def create_job(
        self,
        title: str,
        sec_uid: str,
        nickname: str,
        source: str,
        options: dict,
    ) -> int:
        now = int(time.time())
        conn = self.conn()
        cur = conn.execute(
            "INSERT INTO jobs(title, sec_uid, nickname, source, options, status, created_at, updated_at)"
            " VALUES(?,?,?,?,?,'pending',?,?)",
            (title, sec_uid, nickname, source, json.dumps(options, ensure_ascii=False), now, now),
        )
        conn.commit()
        return int(cur.lastrowid)

    def update_job(self, job_id: int, **fields) -> None:
        if not fields:
            return
        allowed = {
            "title", "nickname", "status", "total", "done", "failed",
            "comments", "bytes", "dir", "error", "options", "source",
        }
        pairs = [(k, v) for k, v in fields.items() if k in allowed]
        if not pairs:
            return
        sets = ", ".join(f"{k}=?" for k, _ in pairs)
        values = [v for _, v in pairs]
        conn = self.conn()
        conn.execute(
            f"UPDATE jobs SET {sets}, updated_at=? WHERE id=?",
            (*values, int(time.time()), job_id),
        )
        conn.commit()

    def get_job(self, job_id: int) -> Optional[dict]:
        row = self.conn().execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return self._job_row(row) if row else None

    def list_jobs(self, limit: int = 100) -> List[dict]:
        rows = self.conn().execute(
            "SELECT * FROM jobs ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [self._job_row(r) for r in rows]

    @staticmethod
    def _job_row(row: sqlite3.Row) -> dict:
        job = dict(row)
        try:
            job["options"] = json.loads(job.get("options") or "{}")
        except json.JSONDecodeError:
            job["options"] = {}
        return job

    def delete_job(self, job_id: int) -> None:
        conn = self.conn()
        conn.execute("DELETE FROM awemes WHERE job_id=?", (job_id,))
        conn.execute("DELETE FROM logs WHERE job_id=?", (job_id,))
        conn.execute("DELETE FROM jobs WHERE id=?", (job_id,))
        conn.commit()

    def reset_running_jobs(self) -> int:
        """服务重启后，把中断的任务标记为 interrupted，可一键续跑。"""
        conn = self.conn()
        cur = conn.execute(
            "UPDATE jobs SET status='interrupted', updated_at=? WHERE status='running'",
            (int(time.time()),),
        )
        conn.commit()
        return cur.rowcount

    # -------------------------------------------------------------- cookies

    def list_cookies(self, enabled_only: bool = False) -> List[dict]:
        sql = "SELECT * FROM cookies"
        if enabled_only:
            sql += " WHERE enabled=1"
        sql += " ORDER BY id"
        return [dict(r) for r in self.conn().execute(sql).fetchall()]

    def get_cookie(self, cookie_id: int) -> Optional[dict]:
        row = self.conn().execute("SELECT * FROM cookies WHERE id=?", (cookie_id,)).fetchone()
        return dict(row) if row else None

    def add_cookie(self, label: str, value: str) -> int:
        conn = self.conn()
        cur = conn.execute(
            "INSERT INTO cookies(label, value, enabled, status, last_used, use_count, created_at)"
            " VALUES(?,?,1,'unknown',0,0,?)",
            (label or "未命名账号", value, int(time.time())),
        )
        conn.commit()
        return int(cur.lastrowid)

    def update_cookie(self, cookie_id: int, **fields) -> None:
        allowed = {"label", "value", "enabled", "status", "note"}
        pairs = [(k, v) for k, v in fields.items() if k in allowed]
        if not pairs:
            return
        sets = ", ".join(f"{k}=?" for k, _ in pairs)
        conn = self.conn()
        conn.execute(
            f"UPDATE cookies SET {sets} WHERE id=?", (*[v for _, v in pairs], cookie_id)
        )
        conn.commit()

    def delete_cookie(self, cookie_id: int) -> None:
        conn = self.conn()
        conn.execute("DELETE FROM cookies WHERE id=?", (cookie_id,))
        conn.commit()

    def touch_cookie(self, cookie_id: int) -> None:
        conn = self.conn()
        conn.execute(
            "UPDATE cookies SET last_used=?, use_count=use_count+1 WHERE id=?",
            (int(time.time()), cookie_id),
        )
        conn.commit()

    # -------------------------------------------------------------- awemes

    def upsert_aweme(self, job_id: int, summary: dict) -> None:
        conn = self.conn()
        conn.execute(
            "INSERT INTO awemes(job_id, aweme_id, desc, create_time, digg, comment, collect, share,"
            " media_type, status, files, media, error) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(job_id, aweme_id) DO UPDATE SET"
            " status=excluded.status, files=excluded.files, media=excluded.media,"
            " error=excluded.error",
            (
                job_id,
                summary["aweme_id"],
                summary.get("desc"),
                summary.get("create_time"),
                summary.get("digg_count", 0),
                summary.get("comment_count", 0),
                summary.get("collect_count", 0),
                summary.get("share_count", 0),
                summary.get("media_type"),
                summary.get("_status", "pending"),
                json.dumps(summary.get("_files") or {}, ensure_ascii=False),
                json.dumps(summary.get("_media") or [], ensure_ascii=False),
                summary.get("_error"),
            ),
        )
        conn.commit()

    def list_aweme_media(self, job_id: int) -> List[dict]:
        """作品媒体清单：本地相对路径与远端直链都存在这里。"""
        rows = self.conn().execute(
            "SELECT aweme_id, desc, create_time, media_type, status, files, media"
            " FROM awemes WHERE job_id=? ORDER BY create_time ASC",
            (job_id,),
        ).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            for key in ("files", "media"):
                try:
                    item[key] = json.loads(item.get(key) or ("{}" if key == "files" else "[]"))
                except json.JSONDecodeError:
                    item[key] = {} if key == "files" else []
            out.append(item)
        return out


    def list_awemes(self, job_id: int, limit: int = 200, offset: int = 0) -> List[dict]:
        rows = self.conn().execute(
            "SELECT * FROM awemes WHERE job_id=? ORDER BY create_time DESC LIMIT ? OFFSET ?",
            (job_id, limit, offset),
        ).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            try:
                item["files"] = json.loads(item.get("files") or "{}")
            except json.JSONDecodeError:
                item["files"] = {}
            out.append(item)
        return out

    def count_awemes(self, job_id: int) -> int:
        row = self.conn().execute(
            "SELECT COUNT(*) AS n FROM awemes WHERE job_id=?", (job_id,)
        ).fetchone()
        return int(row["n"]) if row else 0

    # ---------------------------------------------------------------- logs

    def add_log(self, job_id: Optional[int], level: str, message: str) -> None:
        conn = self.conn()
        conn.execute(
            "INSERT INTO logs(job_id, ts, level, message) VALUES(?,?,?,?)",
            (job_id, int(time.time()), level, message[:2000]),
        )
        conn.commit()

    def get_logs(self, job_id: Optional[int] = None, limit: int = 200) -> List[dict]:
        if job_id is None:
            rows = self.conn().execute(
                "SELECT * FROM logs ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        else:
            rows = self.conn().execute(
                "SELECT * FROM logs WHERE job_id=? ORDER BY id DESC LIMIT ?", (job_id, limit)
            ).fetchall()
        return [dict(r) for r in reversed(rows)]

    def trim_logs(self, keep: int = 5000) -> None:
        conn = self.conn()
        conn.execute(
            "DELETE FROM logs WHERE id NOT IN (SELECT id FROM logs ORDER BY id DESC LIMIT ?)",
            (keep,),
        )
        conn.commit()
