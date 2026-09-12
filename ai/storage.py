"""SQLite-хранилище: пользователи, контекст диалогов, база знаний, статистика."""
from __future__ import annotations

import sqlite3
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from .text import normalize, similarity, words

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id     INTEGER PRIMARY KEY,
    username    TEXT,
    first_name  TEXT,
    last_name   TEXT,
    lang        TEXT,
    created_at  REAL,
    last_seen   REAL,
    messages    INTEGER DEFAULT 0,
    blocked     INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER,
    ts         REAL,
    question   TEXT,
    answer     TEXT,
    source     TEXT,
    latency_ms INTEGER,
    ok         INTEGER DEFAULT 1,
    media      TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_messages_ts ON messages(ts);
CREATE INDEX IF NOT EXISTS idx_messages_user ON messages(user_id);

CREATE TABLE IF NOT EXISTS knowledge (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    question      TEXT,
    question_norm TEXT UNIQUE,
    answer        TEXT,
    teacher       TEXT,
    created_at    REAL,
    updated_at    REAL,
    hits          INTEGER DEFAULT 0,
    last_hit      REAL,
    learned_from  INTEGER
);
CREATE INDEX IF NOT EXISTS idx_knowledge_created ON knowledge(created_at);

CREATE TABLE IF NOT EXISTS context (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    role    TEXT,
    content TEXT,
    ts      REAL
);
CREATE INDEX IF NOT EXISTS idx_context_user ON context(user_id, id);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS events (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    ts     REAL,
    level  TEXT,
    kind   TEXT,
    detail TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
"""

FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_fts
    USING fts5(question_norm, content='knowledge', content_rowid='id');

CREATE TRIGGER IF NOT EXISTS knowledge_ai AFTER INSERT ON knowledge BEGIN
    INSERT INTO knowledge_fts(rowid, question_norm) VALUES (new.id, new.question_norm);
END;
CREATE TRIGGER IF NOT EXISTS knowledge_ad AFTER DELETE ON knowledge BEGIN
    INSERT INTO knowledge_fts(knowledge_fts, rowid, question_norm)
        VALUES ('delete', old.id, old.question_norm);
END;
CREATE TRIGGER IF NOT EXISTS knowledge_au AFTER UPDATE ON knowledge BEGIN
    INSERT INTO knowledge_fts(knowledge_fts, rowid, question_norm)
        VALUES ('delete', old.id, old.question_norm);
    INSERT INTO knowledge_fts(rowid, question_norm) VALUES (new.id, new.question_norm);
END;
"""

DEFAULT_SETTINGS = {
    "learning": "1",      # запоминать ответы учителя
    "brain_first": "1",   # сначала искать ответ в своей памяти
    "autolearn": "0",     # самостоятельное дообучение в фоне
    "threshold": None,    # None -> берём из config при первом запуске
}


def start_of_day(ts: float) -> float:
    lt = time.localtime(ts)
    return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))


class Storage:
    def __init__(self, path, default_threshold: float = 0.88):
        self.path = str(path)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self.started_at = time.time()
        with self._lock:
            self._conn.executescript(SCHEMA)
            try:
                self._conn.executescript(FTS_SCHEMA)
                self.fts = True
            except sqlite3.Error:
                self.fts = False
            self._conn.commit()
        self._migrate()
        for key, val in DEFAULT_SETTINGS.items():
            if val is None:
                val = str(default_threshold)
            if self.get_setting(key) is None:
                self.set_setting(key, val)

    def _migrate(self) -> None:
        """Догоняет схему на базах, созданных прошлыми версиями бота."""
        with self._lock:
            have = {row["name"] for row in
                    self._conn.execute("PRAGMA table_info(messages)").fetchall()}
            if "media" not in have:
                self._conn.execute(
                    "ALTER TABLE messages ADD COLUMN media TEXT DEFAULT ''")
            self._conn.commit()

    # ---------------- низкий уровень ----------------
    def _q(self, sql: str, params: tuple = ()) -> List[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def _one(self, sql: str, params: tuple = (), default: Any = 0) -> Any:
        rows = self._q(sql, params)
        if not rows:
            return default
        val = rows[0][0]
        return default if val is None else val

    def _exec(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ---------------- настройки ----------------
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
        val = self.get_setting(key)
        return default if val is None else val == "1"

    def toggle(self, key: str) -> bool:
        new = not self.flag(key)
        self.set_setting(key, "1" if new else "0")
        return new

    def threshold(self) -> float:
        try:
            return float(self.get_setting("threshold", "0.88"))
        except (TypeError, ValueError):
            return 0.88

    # ---------------- пользователи ----------------
    def upsert_user(self, user) -> None:
        now = time.time()
        self._exec(
            "INSERT INTO users(user_id, username, first_name, last_name, lang, created_at, last_seen) "
            "VALUES(?,?,?,?,?,?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET username=excluded.username, "
            "first_name=excluded.first_name, last_name=excluded.last_name, "
            "lang=excluded.lang, last_seen=excluded.last_seen",
            (user.id, user.username, user.first_name, user.last_name,
             user.language_code, now, now),
        )

    def bump_user(self, user_id: int) -> None:
        self._exec(
            "UPDATE users SET messages = messages + 1, last_seen = ? WHERE user_id = ?",
            (time.time(), user_id),
        )

    def is_blocked(self, user_id: int) -> bool:
        return bool(self._one("SELECT blocked FROM users WHERE user_id=?", (user_id,), 0))

    def set_blocked(self, user_id: int, blocked: bool) -> None:
        self._exec("UPDATE users SET blocked=? WHERE user_id=?", (1 if blocked else 0, user_id))

    def all_user_ids(self, only_active: bool = True) -> List[int]:
        sql = "SELECT user_id FROM users"
        if only_active:
            sql += " WHERE blocked = 0"
        return [r["user_id"] for r in self._q(sql)]

    # ---------------- контекст диалога ----------------
    def add_context(self, user_id: int, role: str, content: str, keep: int = 12) -> None:
        self._exec(
            "INSERT INTO context(user_id, role, content, ts) VALUES(?,?,?,?)",
            (user_id, role, content, time.time()),
        )
        self._exec(
            "DELETE FROM context WHERE user_id=? AND id NOT IN "
            "(SELECT id FROM context WHERE user_id=? ORDER BY id DESC LIMIT ?)",
            (user_id, user_id, keep),
        )

    def get_context(self, user_id: int, limit: int = 12) -> List[Dict[str, str]]:
        rows = self._q(
            "SELECT role, content FROM context WHERE user_id=? ORDER BY id DESC LIMIT ?",
            (user_id, limit),
        )
        return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]

    def clear_context(self, user_id: int) -> int:
        cur = self._exec("DELETE FROM context WHERE user_id=?", (user_id,))
        return cur.rowcount or 0

    def context_size(self, user_id: int) -> int:
        return int(self._one("SELECT COUNT(*) FROM context WHERE user_id=?", (user_id,), 0))

    # ---------------- база знаний ----------------
    def _fts_candidates(self, norm: str, limit: int = 60) -> List[sqlite3.Row]:
        if not self.fts:
            return []
        tokens = [t for t in norm.split() if len(t) >= 2][:8]
        if not tokens:
            return []
        query = " OR ".join('"%s"' % t.replace('"', '') for t in tokens)
        try:
            return self._q(
                "SELECT k.* FROM knowledge_fts f JOIN knowledge k ON k.id = f.rowid "
                "WHERE knowledge_fts MATCH ? ORDER BY bm25(knowledge_fts) LIMIT ?",
                (query, limit),
            )
        except sqlite3.Error:
            return []

    def recall(self, question: str) -> Optional[Tuple[sqlite3.Row, float]]:
        """Ищет в собственной памяти самый близкий выученный вопрос."""
        norm = normalize(question)
        if not norm:
            return None

        exact = self._q("SELECT * FROM knowledge WHERE question_norm=?", (norm,))
        if exact:
            return exact[0], 1.0

        candidates = self._fts_candidates(norm)
        if not candidates:
            candidates = self._q(
                "SELECT * FROM knowledge ORDER BY hits DESC, id DESC LIMIT 1500"
            )
        best_row, best_score = None, 0.0
        for row in candidates:
            score = similarity(norm, row["question_norm"] or "")
            if score > best_score:
                best_row, best_score = row, score
        if best_row is None:
            return None
        return best_row, best_score

    def learn(self, question: str, answer: str, teacher: str, user_id: int = 0) -> int:
        """Запоминает пару вопрос-ответ. Повторный вопрос обновляет ответ."""
        norm = normalize(question)
        now = time.time()
        cur = self._exec(
            "INSERT INTO knowledge(question, question_norm, answer, teacher, "
            "created_at, updated_at, learned_from) VALUES(?,?,?,?,?,?,?) "
            "ON CONFLICT(question_norm) DO UPDATE SET answer=excluded.answer, "
            "updated_at=excluded.updated_at, teacher=excluded.teacher",
            (question.strip(), norm, answer, teacher, now, now, user_id),
        )
        if cur.lastrowid:
            return cur.lastrowid
        return int(self._one("SELECT id FROM knowledge WHERE question_norm=?", (norm,), 0))

    def register_hit(self, knowledge_id: int) -> None:
        self._exec(
            "UPDATE knowledge SET hits = hits + 1, last_hit = ? WHERE id = ?",
            (time.time(), knowledge_id),
        )

    def forget(self, knowledge_id: int) -> bool:
        return (self._exec("DELETE FROM knowledge WHERE id=?", (knowledge_id,)).rowcount or 0) > 0

    def wipe_knowledge(self) -> int:
        count = int(self._one("SELECT COUNT(*) FROM knowledge", (), 0))
        self._exec("DELETE FROM knowledge")
        if self.fts:
            try:
                self._exec("INSERT INTO knowledge_fts(knowledge_fts) VALUES('rebuild')")
            except sqlite3.Error:
                pass
        return count

    def knowledge_top(self, limit: int = 10) -> List[sqlite3.Row]:
        return self._q(
            "SELECT * FROM knowledge ORDER BY hits DESC, updated_at DESC LIMIT ?", (limit,)
        )

    def knowledge_recent(self, limit: int = 10) -> List[sqlite3.Row]:
        return self._q("SELECT * FROM knowledge ORDER BY id DESC LIMIT ?", (limit,))

    def knowledge_search(self, text: str, limit: int = 10) -> List[sqlite3.Row]:
        norm = normalize(text)
        rows = self._fts_candidates(norm, limit=limit * 5)
        if not rows:
            rows = self._q(
                "SELECT * FROM knowledge WHERE question_norm LIKE ? LIMIT ?",
                ("%" + norm + "%", limit),
            )
            return rows
        scored = sorted(rows, key=lambda r: similarity(norm, r["question_norm"] or ""), reverse=True)
        return scored[:limit]

    def learn_seeds(self, limit: int = 20) -> List[str]:
        rows = self._q(
            "SELECT question FROM knowledge ORDER BY RANDOM() LIMIT ?", (limit,)
        )
        return [r["question"] for r in rows]

    # ---------------- журнал ----------------
    def add_message(self, user_id: int, question: str, answer: str,
                    source: str, latency_ms: int, ok: bool = True,
                    media: str = "") -> None:
        self._exec(
            "INSERT INTO messages(user_id, ts, question, answer, source, "
            "latency_ms, ok, media) VALUES(?,?,?,?,?,?,?,?)",
            (user_id, time.time(), question[:2000], (answer or "")[:4000],
             source, int(latency_ms), 1 if ok else 0, media[:200]),
        )

    def log(self, level: str, kind: str, detail: str = "") -> None:
        self._exec(
            "INSERT INTO events(ts, level, kind, detail) VALUES(?,?,?,?)",
            (time.time(), level, kind, str(detail)[:1000]),
        )

    def recent_events(self, limit: int = 15, level: Optional[str] = None) -> List[sqlite3.Row]:
        if level:
            return self._q(
                "SELECT * FROM events WHERE level=? ORDER BY id DESC LIMIT ?", (level, limit)
            )
        return self._q("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))

    # ---------------- статистика ----------------
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
        d["brain_today"] = self._one(
            "SELECT COUNT(*) FROM messages WHERE source='brain' AND ts>=?", (today,))
        d["teacher_today"] = self._one(
            "SELECT COUNT(*) FROM messages WHERE source='teacher' AND ts>=?", (today,))

        d["lat_brain"] = self._one(
            "SELECT AVG(latency_ms) FROM messages WHERE source='brain'", (), 0)
        d["lat_teacher"] = self._one(
            "SELECT AVG(latency_ms) FROM messages WHERE source='teacher'", (), 0)

        d["kb_total"] = self._one("SELECT COUNT(*) FROM knowledge")
        d["kb_today"] = self._one("SELECT COUNT(*) FROM knowledge WHERE created_at>=?", (today,))
        d["kb_week"] = self._one("SELECT COUNT(*) FROM knowledge WHERE created_at>=?", (week,))
        d["kb_hits"] = self._one("SELECT SUM(hits) FROM knowledge", (), 0)
        d["kb_used"] = self._one("SELECT COUNT(*) FROM knowledge WHERE hits>0")
        d["kb_size_kb"] = self._one(
            "SELECT SUM(LENGTH(question)+LENGTH(answer)) FROM knowledge", (), 0) / 1024.0

        d["media_total"] = self._one(
            "SELECT COUNT(*) FROM messages WHERE media != '' AND media IS NOT NULL")
        d["media_today"] = self._one(
            "SELECT COUNT(*) FROM messages WHERE media != '' AND media IS NOT NULL AND ts>=?",
            (today,))

        d["errors_day"] = self._one(
            "SELECT COUNT(*) FROM events WHERE level='error' AND ts>=?", (day,))
        d["errors_total"] = self._one("SELECT COUNT(*) FROM events WHERE level='error'")

        total_answered = (d["from_brain"] or 0) + (d["from_teacher"] or 0)
        d["brain_share"] = (100.0 * d["from_brain"] / total_answered) if total_answered else 0.0
        d["ctx_rows"] = self._one("SELECT COUNT(*) FROM context")
        d["ctx_users"] = self._one("SELECT COUNT(DISTINCT user_id) FROM context")
        return d

    def media_breakdown(self, limit: int = 8) -> List[sqlite3.Row]:
        return self._q(
            "SELECT media AS kind, COUNT(*) AS c FROM messages "
            "WHERE media != '' AND media IS NOT NULL "
            "GROUP BY media ORDER BY c DESC LIMIT ?", (limit,)
        )

    def activity(self, days: int = 7) -> List[Dict[str, Any]]:
        since = start_of_day(time.time()) - (days - 1) * 86400
        rows = self._q(
            "SELECT strftime('%d.%m', ts, 'unixepoch', 'localtime') AS label, "
            "COUNT(*) AS msgs, COUNT(DISTINCT user_id) AS users, "
            "SUM(CASE WHEN source='brain' THEN 1 ELSE 0 END) AS brain "
            "FROM messages WHERE ts >= ? "
            "GROUP BY strftime('%Y-%m-%d', ts, 'unixepoch', 'localtime') ORDER BY MIN(ts)",
            (since,),
        )
        return [dict(r) for r in rows]

    def top_users(self, limit: int = 10) -> List[sqlite3.Row]:
        return self._q(
            "SELECT * FROM users ORDER BY messages DESC, last_seen DESC LIMIT ?", (limit,)
        )

    def recent_users(self, limit: int = 10) -> List[sqlite3.Row]:
        return self._q("SELECT * FROM users ORDER BY created_at DESC LIMIT ?", (limit,))

    def user_card(self, user_id: int) -> Optional[sqlite3.Row]:
        rows = self._q("SELECT * FROM users WHERE user_id=?", (user_id,))
        return rows[0] if rows else None
