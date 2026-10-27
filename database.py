import aiosqlite
from pathlib import Path
from utils import serialize_settings, utc_now

DB_PATH = Path("data/grnd.sqlite3")


async def db_initialize() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                tg_id INTEGER PRIMARY KEY,
                nickname TEXT NOT NULL,
                lvl INTEGER NOT NULL DEFAULT 0,
                cookies_enc TEXT NOT NULL,
                created_at TEXT NOT NULL,
                is_active INTEGER NOT NULL DEFAULT 1,
                is_paused INTEGER NOT NULL DEFAULT 0,
                is_banned INTEGER NOT NULL DEFAULT 0,
                settings_json TEXT NOT NULL DEFAULT '{}'
            );
            CREATE TABLE IF NOT EXISTS blacklist (
                tg_id INTEGER NOT NULL,
                target_nickname TEXT NOT NULL,
                PRIMARY KEY (tg_id, target_nickname),
                FOREIGN KEY (tg_id) REFERENCES users(tg_id)
            );
            CREATE TABLE IF NOT EXISTS complaints (
                site_id TEXT NOT NULL,
                tg_id INTEGER NOT NULL,
                is_sent INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                PRIMARY KEY (site_id, tg_id)
            );
            CREATE TABLE IF NOT EXISTS logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tg_id INTEGER NOT NULL,
                action TEXT NOT NULL,
                timestamp TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS reminders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tg_id INTEGER NOT NULL,
                site_id TEXT NOT NULL,
                remind_at TEXT NOT NULL,
                message TEXT NOT NULL,
                delivered INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_complaints_user_sent
            ON complaints(tg_id, is_sent);
            CREATE INDEX IF NOT EXISTS idx_reminders_due
            ON reminders(delivered, remind_at);
        """)
        await db.commit()


async def db_fetch_user(tg_id: int) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM users WHERE tg_id = ?", (tg_id,))
        row = await cur.fetchone()
        return dict(row) if row else None


async def db_save_user(
    tg_id: int,
    nickname: str,
    lvl: int,
    cookies_enc: str,
) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO users (
                tg_id, nickname, lvl, cookies_enc, created_at
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(tg_id) DO UPDATE SET
                nickname = excluded.nickname,
                lvl = excluded.lvl,
                cookies_enc = excluded.cookies_enc,
                is_active = 1
        """, (tg_id, nickname, lvl, cookies_enc, utc_now()))
        await db.commit()


async def db_update_user(tg_id: int, **fields) -> None:
    allowed = {
        "nickname", "lvl", "cookies_enc", "is_active",
        "is_paused", "is_banned", "settings_json"
    }
    updates = {key: value for key, value in fields.items() if key in allowed}
    if not updates:
        return
    assignments = ", ".join(f"{key} = ?" for key in updates)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            f"UPDATE users SET {assignments} WHERE tg_id = ?",
            (*updates.values(), tg_id),
        )
        await db.commit()


async def db_all_users() -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM users ORDER BY created_at")
        return [dict(row) for row in await cur.fetchall()]


async def db_log(tg_id: int, action: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO logs(tg_id, action, timestamp) VALUES (?, ?, ?)",
            (tg_id, action[:500], utc_now()),
        )
        await db.commit()


async def db_recent_logs(limit: int = 30) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM logs ORDER BY id DESC LIMIT ?",
            (max(1, min(limit, 100)),),
        )
        return [dict(row) for row in await cur.fetchall()]


async def db_blacklist(tg_id: int) -> list[str]:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT target_nickname FROM blacklist WHERE tg_id = ? ORDER BY target_nickname",
            (tg_id,),
        )
        return [row[0] for row in await cur.fetchall()]


async def db_blacklist_add(tg_id: int, nickname: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO blacklist(tg_id, target_nickname) VALUES (?, ?)",
            (tg_id, nickname),
        )
        await db.commit()


async def db_blacklist_remove(tg_id: int, nickname: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "DELETE FROM blacklist WHERE tg_id = ? AND target_nickname = ?",
            (tg_id, nickname),
        )
        await db.commit()


async def db_complaint_seen(site_id: str, tg_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT 1 FROM complaints WHERE site_id = ? AND tg_id = ?",
            (site_id, tg_id),
        )
        return await cur.fetchone() is not None


async def db_mark_complaint(site_id: str, tg_id: int, is_sent: bool) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO complaints(site_id, tg_id, is_sent, created_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(site_id, tg_id) DO UPDATE SET
                is_sent = MAX(complaints.is_sent, excluded.is_sent)
        """, (site_id, tg_id, int(is_sent), utc_now()))
        await db.commit()


async def db_add_reminder(
    tg_id: int,
    site_id: str,
    remind_at: str,
    message: str,
) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO reminders(tg_id, site_id, remind_at, message) VALUES (?, ?, ?, ?)",
            (tg_id, site_id, remind_at, message),
        )
        await db.commit()


async def db_due_reminders(now: str) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM reminders WHERE delivered = 0 AND remind_at <= ?",
            (now,),
        )
        return [dict(row) for row in await cur.fetchall()]


async def db_mark_reminder_delivered(reminder_id: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE reminders SET delivered = 1 WHERE id = ?",
            (reminder_id,),
        )
        await db.commit()
