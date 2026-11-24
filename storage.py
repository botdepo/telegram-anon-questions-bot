"""SQLite: владелец, маршруты ответов, блокировки и тематические сборы.

На botdepo писать на диск можно только в /data — эта папка переживает
перезапуски и передеплои. Остальная файловая система только для чтения.
Локально (где /data нет) база ляжет в ./data рядом с кодом.
"""
import os
import secrets
import sqlite3
from pathlib import Path
from typing import NamedTuple

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
    CREATE TABLE IF NOT EXISTS collections (
        id TEXT PRIMARY KEY,
        title TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS guest_collections (
        user_id INTEGER PRIMARY KEY,
        collection_id TEXT NOT NULL
    );
    """
)
# Existing bots keep their reply routes when the template is updated.
if "collection_id" not in {row[1] for row in _db.execute("PRAGMA table_info(questions)")}:
    _db.execute("ALTER TABLE questions ADD COLUMN collection_id TEXT")
_db.execute("CREATE INDEX IF NOT EXISTS questions_collection ON questions (collection_id)")
_db.commit()

MAX_COLLECTION_TITLE = 100


class Collection(NamedTuple):
    id: str
    title: str


def create_collection(title: str) -> Collection:
    title = " ".join(title.split())
    if not title or len(title) > MAX_COLLECTION_TITLE:
        raise ValueError(f"Название должно содержать от 1 до {MAX_COLLECTION_TITLE} символов.")
    collection = Collection(secrets.token_urlsafe(9), title)
    _db.execute("INSERT INTO collections (id, title) VALUES (?, ?)", collection)
    _db.commit()
    return collection


def collection_by_id(collection_id: str) -> Collection | None:
    row = _db.execute("SELECT id, title FROM collections WHERE id = ?", (collection_id,)).fetchone()
    return Collection(*row) if row else None


def collections_page(offset: int, limit: int) -> list[Collection]:
    rows = _db.execute(
        "SELECT id, title FROM collections ORDER BY rowid DESC LIMIT ? OFFSET ?",
        (limit, offset),
    ).fetchall()
    return [Collection(*row) for row in rows]


def select_collection(user_id: int, collection_id: str | None) -> None:
    if collection_id is None:
        _db.execute("DELETE FROM guest_collections WHERE user_id = ?", (user_id,))
    else:
        _db.execute(
            "INSERT OR REPLACE INTO guest_collections (user_id, collection_id) VALUES (?, ?)",
            (user_id, collection_id),
        )
    _db.commit()


def selected_collection(user_id: int) -> Collection | None:
    row = _db.execute(
        "SELECT c.id, c.title FROM collections c "
        "JOIN guest_collections g ON g.collection_id = c.id WHERE g.user_id = ?",
        (user_id,),
    ).fetchone()
    return Collection(*row) if row else None


def get(key: str) -> str | None:
    row = _db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def put(key: str, value: str) -> None:
    _db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))
    _db.commit()


def claim_owner(user_id: int) -> bool:
    if owner_id() is None:
        put("owner_id", str(user_id))
    return owner_id() == user_id


def owner_id() -> int | None:
    configured = os.getenv("OWNER_ID")
    if configured is not None and (not configured.isascii() or not configured.isdigit() or int(configured) <= 0):
        raise ValueError("OWNER_ID must be a positive Telegram user id")
    stored = get("owner_id")
    if configured and stored not in (None, configured):
        raise ValueError("Stored owner differs from OWNER_ID; refusing reassignment")
    if configured and stored is None:
        put("owner_id", configured)
    value = configured or stored
    return int(value) if value else None


def remember_question(owner_message_id: int, user_id: int, collection_id: str | None = None) -> None:
    _db.execute(
        "INSERT OR REPLACE INTO questions (owner_message_id, user_id, collection_id) VALUES (?, ?, ?)",
        (owner_message_id, user_id, collection_id),
    )
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


def question_count(collection_id: str | None = None) -> int:
    if collection_id is not None:
        return _db.execute("SELECT COUNT(*) FROM questions WHERE collection_id = ?", (collection_id,)).fetchone()[0]
    return _db.execute("SELECT COUNT(*) FROM questions").fetchone()[0]
