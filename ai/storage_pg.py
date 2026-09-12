"""PostgreSQL storage backend for shared hosting deployments.

All tables live in a dedicated schema (``aibot`` by default), so DATABASE_URL
can point at the same PostgreSQL database as Emerald without table collisions.
"""
from __future__ import annotations

import re
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from psycopg import InterfaceError, OperationalError, connect
from psycopg.rows import dict_row

from .text import normalize, similarity
from .storage import DEFAULT_SETTINGS, start_of_day

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id BIGINT PRIMARY KEY, username TEXT, first_name TEXT, last_name TEXT,
    lang TEXT, created_at DOUBLE PRECISION, last_seen DOUBLE PRECISION,
    messages BIGINT DEFAULT 0, blocked INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS messages (
    id BIGSERIAL PRIMARY KEY, user_id BIGINT, ts DOUBLE PRECISION,
    question TEXT, answer TEXT, source TEXT, latency_ms INTEGER,
    ok INTEGER DEFAULT 1, media TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_messages_ts ON messages(ts);
CREATE INDEX IF NOT EXISTS idx_messages_user ON messages(user_id);
CREATE TABLE IF NOT EXISTS knowledge (
    id BIGSERIAL PRIMARY KEY, question TEXT, question_norm TEXT UNIQUE,
    answer TEXT, teacher TEXT, created_at DOUBLE PRECISION,
    updated_at DOUBLE PRECISION, hits BIGINT DEFAULT 0,
    last_hit DOUBLE PRECISION, learned_from BIGINT
);
CREATE INDEX IF NOT EXISTS idx_knowledge_created ON knowledge(created_at);
CREATE TABLE IF NOT EXISTS context (
    id BIGSERIAL PRIMARY KEY, user_id BIGINT, role TEXT, content TEXT,
    ts DOUBLE PRECISION
);
CREATE INDEX IF NOT EXISTS idx_context_user ON context(user_id, id);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS events (
    id BIGSERIAL PRIMARY KEY, ts DOUBLE PRECISION, level TEXT,
    kind TEXT, detail TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
"""


def _dsn(value: str) -> str:
    value = (value or "").strip()
    value = re.sub(r"^postgresql\+psycopg2?://", "postgresql://", value, flags=re.I)
    if value.lower().startswith("postgres://"):
        value = "postgresql://" + value[len("postgres://"):]
    return value


class PostgresStorage:
    fts = False
    backend = "postgresql"
    is_postgres = True

    def __init__(self, database_url: str, default_threshold: float = 0.88,
                 schema: str = "aibot"):
        if not database_url:
            raise ValueError("DATABASE_URL пуст")
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,62}", schema or ""):
            raise ValueError("Некорректный AIBOT_DB_SCHEMA")
        self.database_url = _dsn(database_url)
        self.schema = schema
        self.started_at = time.time()
        self._lock = threading.RLock()
        self._conn = self._connect()
        with self._lock, self._conn.cursor() as cur:
            cur.execute(f'CREATE SCHEMA IF NOT EXISTS "{self.schema}"')
            cur.execute(f'SET search_path TO "{self.schema}"')
            for statement in SCHEMA.split(";"):
                if statement.strip():
                    cur.execute(statement)
            cur.execute("ALTER TABLE messages ADD COLUMN IF NOT EXISTS media TEXT DEFAULT ''")
            self._conn.commit()
        for key, value in DEFAULT_SETTINGS.items():
            if value is None:
                value = str(default_threshold)
            if self.get_setting(key) is None:
                self.set_setting(key, value)

    def _connect(self):
        conn = connect(
            self.database_url,
            row_factory=dict_row,
            connect_timeout=12,
            application_name="aibot",
        )
        with conn.cursor() as cur:
            cur.execute(f'SET search_path TO "{self.schema}"')
        conn.commit()
        return conn

    @staticmethod
    def _sql(sql: str) -> str:
        return sql.replace("?", "%s")

    def _ensure(self) -> None:
        if self._conn.closed:
            self._conn = self._connect()

    def _retry(self, func):
        for attempt in range(2):
            try:
                self._ensure()
                return func()
            except (OperationalError, InterfaceError):
                if attempt:
                    raise
                try:
                    self._conn.close()
                except Exception:
                    pass
                self._conn = self._connect()

    def _q(self, sql: str, params: tuple = ()) -> List[dict]:
        with self._lock:
            def run():
                with self._conn.cursor() as cur:
                    cur.execute(self._sql(sql), params)
                    return cur.fetchall()
            return self._retry(run)

    def _one(self, sql: str, params: tuple = (), default: Any = 0) -> Any:
        rows = self._q(sql, params)
        if not rows:
            return default
        value = next(iter(rows[0].values()), default)
        return default if value is None else value

    def _exec(self, sql: str, params: tuple = ()):
        with self._lock:
            def run():
                with self._conn.cursor() as cur:
                    cur.execute(self._sql(sql), params)
                    rowcount = cur.rowcount
                self._conn.commit()
                return rowcount
            return self._retry(run)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def get_setting(self, key: str, default: Any = None) -> Any:
        rows = self._q("SELECT value FROM settings WHERE key=?", (key,))
        return rows[0]["value"] if rows else default

    def set_setting(self, key: str, value: Any) -> None:
        self._exec(
            "INSERT INTO settings(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )

    def flag(self, key: str, default: bool = True) -> bool:
        value = self.get_setting(key)
        return default if value is None else value == "1"

    def toggle(self, key: str) -> bool:
        value = not self.flag(key)
        self.set_setting(key, "1" if value else "0")
        return value

    def threshold(self) -> float:
        try:
            return float(self.get_setting("threshold", "0.88"))
        except (TypeError, ValueError):
            return 0.88

    def upsert_user(self, user) -> None:
        now = time.time()
        self._exec(
            "INSERT INTO users(user_id, username, first_name, last_name, lang, created_at, last_seen) "
            "VALUES(?,?,?,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET "
            "username=excluded.username, first_name=excluded.first_name, "
            "last_name=excluded.last_name, lang=excluded.lang, last_seen=excluded.last_seen",
            (user.id, user.username, user.first_name, user.last_name,
             user.language_code, now, now),
        )

    def bump_user(self, user_id: int) -> None:
        self._exec("UPDATE users SET messages=messages+1, last_seen=? WHERE user_id=?",
                   (time.time(), user_id))

    def is_blocked(self, user_id: int) -> bool:
        return bool(self._one("SELECT blocked FROM users WHERE user_id=?", (user_id,), 0))

    def set_blocked(self, user_id: int, blocked: bool) -> None:
        self._exec("UPDATE users SET blocked=? WHERE user_id=?",
                   (1 if blocked else 0, user_id))

    def all_user_ids(self, only_active: bool = True) -> List[int]:
        sql = "SELECT user_id FROM users" + (" WHERE blocked=0" if only_active else "")
        return [row["user_id"] for row in self._q(sql)]

    def add_context(self, user_id: int, role: str, content: str, keep: int = 12) -> None:
        self._exec("INSERT INTO context(user_id, role, content, ts) VALUES(?,?,?,?)",
                   (user_id, role, content, time.time()))
        self._exec(
            "DELETE FROM context WHERE user_id=? AND id NOT IN "
            "(SELECT id FROM context WHERE user_id=? ORDER BY id DESC LIMIT ?)",
            (user_id, user_id, keep),
        )

    def get_context(self, user_id: int, limit: int = 12) -> List[Dict[str, str]]:
        rows = self._q("SELECT role, content FROM context WHERE user_id=? ORDER BY id DESC LIMIT ?",
                       (user_id, limit))
        return [{"role": row["role"], "content": row["content"]} for row in reversed(rows)]

    def clear_context(self, user_id: int) -> int:
        return max(0, self._exec("DELETE FROM context WHERE user_id=?", (user_id,)))

    def context_size(self, user_id: int) -> int:
        return int(self._one("SELECT COUNT(*) FROM context WHERE user_id=?", (user_id,), 0))

    def recall(self, question: str) -> Optional[Tuple[dict, float]]:
        norm = normalize(question)
        if not norm:
            return None
        exact = self._q("SELECT * FROM knowledge WHERE question_norm=?", (norm,))
        if exact:
            return exact[0], 1.0
        rows = self._q("SELECT * FROM knowledge ORDER BY hits DESC, id DESC LIMIT 1500")
        best, score = None, 0.0
        for row in rows:
            current = similarity(norm, row["question_norm"] or "")
            if current > score:
                best, score = row, current
        return None if best is None else (best, score)

    def learn(self, question: str, answer: str, teacher: str, user_id: int = 0) -> int:
        norm = normalize(question)
        now = time.time()
        self._exec(
            "INSERT INTO knowledge(question, question_norm, answer, teacher, created_at, updated_at, learned_from) "
            "VALUES(?,?,?,?,?,?,?) ON CONFLICT(question_norm) DO UPDATE SET "
            "answer=excluded.answer, updated_at=excluded.updated_at, teacher=excluded.teacher",
            (question.strip(), norm, answer, teacher, now, now, user_id),
        )
        return int(self._one("SELECT id FROM knowledge WHERE question_norm=?", (norm,), 0))

    def register_hit(self, knowledge_id: int) -> None:
        self._exec("UPDATE knowledge SET hits=hits+1, last_hit=? WHERE id=?",
                   (time.time(), knowledge_id))

    def forget(self, knowledge_id: int) -> bool:
        return self._exec("DELETE FROM knowledge WHERE id=?", (knowledge_id,)) > 0

    def wipe_knowledge(self) -> int:
        count = int(self._one("SELECT COUNT(*) FROM knowledge", (), 0))
        self._exec("DELETE FROM knowledge")
        return count

    def knowledge_top(self, limit: int = 10) -> List[dict]:
        return self._q("SELECT * FROM knowledge ORDER BY hits DESC, updated_at DESC LIMIT ?",
                       (limit,))

    def knowledge_recent(self, limit: int = 10) -> List[dict]:
        return self._q("SELECT * FROM knowledge ORDER BY id DESC LIMIT ?", (limit,))

    def knowledge_search(self, text: str, limit: int = 10) -> List[dict]:
        norm = normalize(text)
        rows = self._q("SELECT * FROM knowledge WHERE question_norm LIKE ? LIMIT ?",
                       ("%" + norm + "%", limit * 5))
        rows.sort(key=lambda row: similarity(norm, row["question_norm"] or ""), reverse=True)
        return rows[:limit]

    def learn_seeds(self, limit: int = 20) -> List[str]:
        return [row["question"] for row in
                self._q("SELECT question FROM knowledge ORDER BY RANDOM() LIMIT ?", (limit,))]

    def add_message(self, user_id: int, question: str, answer: str,
                    source: str, latency_ms: int, ok: bool = True,
                    media: str = "") -> None:
        self._exec(
            "INSERT INTO messages(user_id, ts, question, answer, source, latency_ms, ok, media) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (user_id, time.time(), question[:2000], (answer or "")[:4000],
             source, int(latency_ms), 1 if ok else 0, media[:200]),
        )

    def log(self, level: str, kind: str, detail: str = "") -> None:
        self._exec("INSERT INTO events(ts, level, kind, detail) VALUES(?,?,?,?)",
                   (time.time(), level, kind, str(detail)[:1000]))

    def recent_events(self, limit: int = 15, level: Optional[str] = None) -> List[dict]:
        if level:
            return self._q("SELECT * FROM events WHERE level=? ORDER BY id DESC LIMIT ?",
                           (level, limit))
        return self._q("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))

    def overview(self) -> Dict[str, Any]:
        now = time.time()
        today = start_of_day(now)
        day = now - 86400
        week = now - 7 * 86400
        d: Dict[str, Any] = {}
        d["users_total"] = self._one("SELECT COUNT(*) FROM users")
        d["users_today"] = self._one("SELECT COUNT(*) FROM users WHERE created_at>=?", (today,))
        d["users_week"] = self._one("SELECT COUNT(*) FROM users WHERE created_at>=?", (week,))
        d["users_active_day"] = self._one("SELECT COUNT(*) FROM users WHERE last_seen>=?", (day,))
        d["users_blocked"] = self._one("SELECT COUNT(*) FROM users WHERE blocked=1")
        d["msg_total"] = self._one("SELECT COUNT(*) FROM messages")
        d["msg_today"] = self._one("SELECT COUNT(*) FROM messages WHERE ts>=?", (today,))
        d["msg_week"] = self._one("SELECT COUNT(*) FROM messages WHERE ts>=?", (week,))
        d["from_brain"] = self._one("SELECT COUNT(*) FROM messages WHERE source='brain'")
        d["from_teacher"] = self._one("SELECT COUNT(*) FROM messages WHERE source='teacher'")
        d["brain_today"] = self._one("SELECT COUNT(*) FROM messages WHERE source='brain' AND ts>=?", (today,))
        d["teacher_today"] = self._one("SELECT COUNT(*) FROM messages WHERE source='teacher' AND ts>=?", (today,))
        d["lat_brain"] = self._one("SELECT AVG(latency_ms) FROM messages WHERE source='brain'", (), 0)
        d["lat_teacher"] = self._one("SELECT AVG(latency_ms) FROM messages WHERE source='teacher'", (), 0)
        d["kb_total"] = self._one("SELECT COUNT(*) FROM knowledge")
        d["kb_today"] = self._one("SELECT COUNT(*) FROM knowledge WHERE created_at>=?", (today,))
        d["kb_week"] = self._one("SELECT COUNT(*) FROM knowledge WHERE created_at>=?", (week,))
        d["kb_hits"] = self._one("SELECT SUM(hits) FROM knowledge", (), 0)
        d["kb_used"] = self._one("SELECT COUNT(*) FROM knowledge WHERE hits>0")
        d["kb_size_kb"] = self._one("SELECT SUM(LENGTH(question)+LENGTH(answer)) FROM knowledge", (), 0) / 1024.0
        d["media_total"] = self._one("SELECT COUNT(*) FROM messages WHERE media!='' AND media IS NOT NULL")
        d["media_today"] = self._one("SELECT COUNT(*) FROM messages WHERE media!='' AND media IS NOT NULL AND ts>=?", (today,))
        d["errors_day"] = self._one("SELECT COUNT(*) FROM events WHERE level='error' AND ts>=?", (day,))
        d["errors_total"] = self._one("SELECT COUNT(*) FROM events WHERE level='error'")
        total = (d["from_brain"] or 0) + (d["from_teacher"] or 0)
        d["brain_share"] = 100.0 * d["from_brain"] / total if total else 0.0
        d["ctx_rows"] = self._one("SELECT COUNT(*) FROM context")
        d["ctx_users"] = self._one("SELECT COUNT(DISTINCT user_id) FROM context")
        return d

    def media_breakdown(self, limit: int = 8) -> List[dict]:
        return self._q(
            "SELECT media AS kind, COUNT(*) AS c FROM messages "
            "WHERE media!='' AND media IS NOT NULL GROUP BY media ORDER BY c DESC LIMIT ?",
            (limit,),
        )

    def activity(self, days: int = 7) -> List[Dict[str, Any]]:
        since = start_of_day(time.time()) - (days - 1) * 86400
        rows = self._q(
            "SELECT to_char(to_timestamp(ts), 'DD.MM') AS label, COUNT(*) AS msgs, "
            "COUNT(DISTINCT user_id) AS users, "
            "SUM(CASE WHEN source='brain' THEN 1 ELSE 0 END) AS brain "
            "FROM messages WHERE ts>=? GROUP BY date_trunc('day', to_timestamp(ts)) "
            "ORDER BY MIN(ts)",
            (since,),
        )
        return rows

    def top_users(self, limit: int = 10) -> List[dict]:
        return self._q("SELECT * FROM users ORDER BY messages DESC, last_seen DESC LIMIT ?", (limit,))

    def recent_users(self, limit: int = 10) -> List[dict]:
        return self._q("SELECT * FROM users ORDER BY created_at DESC LIMIT ?", (limit,))

    def user_card(self, user_id: int) -> Optional[dict]:
        rows = self._q("SELECT * FROM users WHERE user_id=?", (user_id,))
        return rows[0] if rows else None
