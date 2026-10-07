"""Хранилище в SQLite: владелец, связь «вопрос у владельца → автор», блокировки.

На botdepo писать на диск можно только в /data — эта папка переживает
перезапуски и передеплои. Остальная файловая система только для чтения.
Локально (где /data нет) база ляжет в ./data рядом с кодом.
"""
import os
import sqlite3
from pathlib import Path

DATA_DIR = Path(os.getenv("DATA_DIR") or ("/data" if Path("/data").is_dir() else "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

_db = sqlite3.connect(DATA_DIR / "questions.db")
_db.executescript(
    """
    CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    -- owner_message_id: сообщение с вопросом в чате владельца; по нему находим автора, когда владелец отвечает
    CREATE TABLE IF NOT EXISTS questions (
        owner_message_id INTEGER PRIMARY KEY,
        user_id INTEGER NOT NULL,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS banned (user_id INTEGER PRIMARY KEY);
    """
)
_db.commit()


def get(key: str) -> str | None:
    row = _db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def put(key: str, value: str) -> None:
    _db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))
    _db.commit()


def claim_owner(user_id: int) -> bool:
    """Первый, кто написал /start, становится владельцем. True — если это он."""
    if get("owner_id") is None:
        put("owner_id", str(user_id))
    return get("owner_id") == str(user_id)


def owner_id() -> int | None:
    value = get("owner_id")
    return int(value) if value else None


def remember_question(owner_message_id: int, user_id: int) -> None:
    _db.execute("INSERT OR REPLACE INTO questions (owner_message_id, user_id) VALUES (?, ?)", (owner_message_id, user_id))
    _db.commit()


def author_of(owner_message_id: int) -> int | None:
    row = _db.execute("SELECT user_id FROM questions WHERE owner_message_id = ?", (owner_message_id,)).fetchone()
    return row[0] if row else None


def ban(user_id: int) -> None:
    _db.execute("INSERT OR IGNORE INTO banned (user_id) VALUES (?)", (user_id,))
    _db.commit()


def unban_all() -> int:
    cur = _db.execute("DELETE FROM banned")
    _db.commit()
    return cur.rowcount


def is_banned(user_id: int) -> bool:
    return _db.execute("SELECT 1 FROM banned WHERE user_id = ?", (user_id,)).fetchone() is not None


def question_count() -> int:
    return _db.execute("SELECT COUNT(*) FROM questions").fetchone()[0]
