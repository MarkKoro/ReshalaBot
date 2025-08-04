import aiosqlite
from pathlib import Path
from datetime import date

DB_PATH = Path(__file__).with_name("credits.db")

async def create_tables():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
              uid           INTEGER PRIMARY KEY,
              credits       INTEGER    DEFAULT 0,
              free_photo    INTEGER    DEFAULT 1,
              free_text_dt  DATE       DEFAULT '',
              ref_parent    INTEGER    DEFAULT 0
            );
            """
        )
        await db.commit()

async def _ensure_user(db, uid: int):
    await db.execute("INSERT OR IGNORE INTO users(uid) VALUES(?)", (uid,))

async def get_credits(uid: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        await _ensure_user(db, uid)
        cur = await db.execute("SELECT credits FROM users WHERE uid=?", (uid,))
        row = await cur.fetchone()
        return row[0] if row else 0

async def add_credits(uid: int, n: int):
    if n <= 0:
        return
    async with aiosqlite.connect(DB_PATH) as db:
        await _ensure_user(db, uid)
        await db.execute("UPDATE users SET credits = credits + ? WHERE uid=?", (n, uid))
        await db.commit()

async def dec_credits(uid: int, n: int):
    if n <= 0:
        return
    async with aiosqlite.connect(DB_PATH) as db:
        await _ensure_user(db, uid)
        cur = await db.execute("SELECT credits FROM users WHERE uid=?", (uid,))
        row = await cur.fetchone()
        have = row[0] if row else 0
        if have < n:
            raise ValueError("not enough credits")
        await db.execute("UPDATE users SET credits = credits - ? WHERE uid=?", (n, uid))
        await db.commit()

async def can_use_daily_text(uid: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        await _ensure_user(db, uid)
        cur = await db.execute("SELECT free_text_dt FROM users WHERE uid=?", (uid,))
        row = await cur.fetchone()
        last = row[0] if row else ''
    return last != date.today().isoformat()

async def mark_daily_text(uid: int):
    today = date.today().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await _ensure_user(db, uid)
        await db.execute("UPDATE users SET free_text_dt=? WHERE uid=?", (today, uid))
        await db.commit()

async def get_free_photo(uid: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        await _ensure_user(db, uid)
        cur = await db.execute("SELECT free_photo FROM users WHERE uid=?", (uid,))
        row = await cur.fetchone()
        return row[0] if row else 0

async def use_free_photo(uid: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await _ensure_user(db, uid)
        cur = await db.execute("SELECT free_photo FROM users WHERE uid=?", (uid,))
        row = await cur.fetchone()
        qty = row[0] if row else 0
        if qty <= 0:
            raise ValueError("no free photo")
        await db.execute("UPDATE users SET free_photo = free_photo - 1 WHERE uid=?", (uid,))
        await db.commit()

async def grant_start_bonus(uid: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await _ensure_user(db, uid)
        await db.commit()

async def grant_referral_bonus(uid: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await _ensure_user(db, uid)
        await db.execute("UPDATE users SET free_photo = free_photo + 2 WHERE uid=?", (uid,))
        await db.commit()

async def set_ref_parent(child_uid: int, parent_uid: int):
    if child_uid == parent_uid:
        return
    async with aiosqlite.connect(DB_PATH) as db:
        await _ensure_user(db, child_uid)
        await _ensure_user(db, parent_uid)
        await db.execute(
            "UPDATE users SET ref_parent=? WHERE uid=? AND ref_parent=0",
            (parent_uid, child_uid),
        )
        await db.commit()
