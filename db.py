import aiosqlite
from pathlib import Path
from datetime import date

DB_PATH = Path(__file__).with_name("credits.db")


async def create_tables(db):
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
          id            INTEGER PRIMARY KEY,
          credits       INTEGER    DEFAULT 0,
          free_photo    INTEGER    DEFAULT 1,
          free_text_dt  DATE       DEFAULT '',
          ref_parent    INTEGER    DEFAULT 0,
          text_cnt      INTEGER    DEFAULT 0,
          photo_cnt     INTEGER    DEFAULT 0,
          menu_msg      INTEGER    DEFAULT 0,
          alias         TEXT       UNIQUE
        );
        """
    )
    # upgrade legacy tables
    try:
        await db.execute("ALTER TABLE users ADD COLUMN text_cnt INTEGER DEFAULT 0")
    except Exception:
        pass
    try:
        await db.execute("ALTER TABLE users ADD COLUMN photo_cnt INTEGER DEFAULT 0")
    except Exception:
        pass
    try:
        await db.execute("ALTER TABLE users ADD COLUMN menu_msg INTEGER DEFAULT 0")
    except Exception:
        pass
    try:
        await db.execute("ALTER TABLE users ADD COLUMN alias TEXT UNIQUE")
    except Exception:
        pass
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS payments (
          uid   INTEGER,
          rub   INTEGER DEFAULT 0,
          stars INTEGER DEFAULT 0
        );
        """
    )
    await db.commit()


async def _ensure_user(db, uid: int):
    await db.execute("INSERT OR IGNORE INTO users(id) VALUES(?)", (uid,))


async def get_credits(db, uid: int) -> int:
    await _ensure_user(db, uid)
    async with db.execute("SELECT credits FROM users WHERE id=?", (uid,)) as cur:
        row = await cur.fetchone()
    return row[0] if row else 0


async def add_credits(db, uid: int, n: int):
    if n <= 0:
        return
    await _ensure_user(db, uid)
    await db.execute("UPDATE users SET credits = credits + ? WHERE id=?", (n, uid))
    await db.commit()


async def dec_credits(db, uid: int, n: int):
    if n <= 0:
        return
    await _ensure_user(db, uid)
    async with db.execute("SELECT credits FROM users WHERE id=?", (uid,)) as cur:
        row = await cur.fetchone()
    have = row[0] if row else 0
    if have < n:
        raise ValueError("not enough credits")
    await db.execute("UPDATE users SET credits = credits - ? WHERE id=?", (n, uid))
    await db.commit()


async def inc_text_cnt(db, uid: int):
    await _ensure_user(db, uid)
    await db.execute("UPDATE users SET text_cnt = text_cnt + 1 WHERE id=?", (uid,))
    await db.commit()


async def inc_photo_cnt(db, uid: int):
    await _ensure_user(db, uid)
    await db.execute("UPDATE users SET photo_cnt = photo_cnt + 1 WHERE id=?", (uid,))
    await db.commit()


async def get_counts(db, uid: int) -> tuple[int, int]:
    await _ensure_user(db, uid)
    async with db.execute("SELECT text_cnt, photo_cnt FROM users WHERE id=?", (uid,)) as cur:
        row = await cur.fetchone()
    if row:
        return row[0], row[1]
    return 0, 0


async def can_use_daily_text(db, uid: int) -> bool:
    await _ensure_user(db, uid)
    async with db.execute("SELECT free_text_dt FROM users WHERE id=?", (uid,)) as cur:
        row = await cur.fetchone()
    last = row[0] if row else ''
    return last != date.today().isoformat()


async def mark_daily_text(db, uid: int):
    today = date.today().isoformat()
    await _ensure_user(db, uid)
    await db.execute("UPDATE users SET free_text_dt=? WHERE id=?", (today, uid))
    await db.commit()


async def get_free_photo(db, uid: int) -> int:
    await _ensure_user(db, uid)
    async with db.execute("SELECT free_photo FROM users WHERE id=?", (uid,)) as cur:
        row = await cur.fetchone()
    return row[0] if row else 0


async def use_free_photo(db, uid: int):
    await _ensure_user(db, uid)
    async with db.execute("SELECT free_photo FROM users WHERE id=?", (uid,)) as cur:
        row = await cur.fetchone()
    qty = row[0] if row else 0
    if qty <= 0:
        raise ValueError("no free photo")
    await db.execute("UPDATE users SET free_photo = free_photo - 1 WHERE id=?", (uid,))
    await db.commit()


async def grant_start_bonus(db, uid: int):
    await _ensure_user(db, uid)
    await db.commit()


async def grant_referral_bonus(db, uid: int):
    await _ensure_user(db, uid)
    await db.execute("UPDATE users SET free_photo = free_photo + 1 WHERE id=?", (uid,))
    await db.commit()


async def set_ref_parent(db, child_uid: int, parent_uid: int):
    if child_uid == parent_uid:
        return
    await _ensure_user(db, child_uid)
    await _ensure_user(db, parent_uid)
    await db.execute(
        "UPDATE users SET ref_parent=? WHERE id=? AND ref_parent=0",
        (parent_uid, child_uid),
    )
    await db.commit()


async def get_all_uids(db) -> list[int]:
    async with db.execute("SELECT id FROM users") as cur:
        rows = await cur.fetchall()
    return [r[0] for r in rows]


async def set_menu_msg(db, uid: int, msg_id: int):
    await _ensure_user(db, uid)
    await db.execute("UPDATE users SET menu_msg=? WHERE id=?", (msg_id, uid))
    await db.commit()


async def get_menu_msg(db, uid: int) -> int:
    await _ensure_user(db, uid)
    async with db.execute("SELECT menu_msg FROM users WHERE id=?", (uid,)) as cur:
        row = await cur.fetchone()
    return row[0] if row else 0


async def get_ref_count(db, uid: int) -> int:
    async with db.execute("SELECT COUNT(*) FROM users WHERE ref_parent=?", (uid,)) as cur:
        row = await cur.fetchone()
    return row[0] if row else 0


async def set_free_photo(db, uid: int, qty: int):
    await _ensure_user(db, uid)
    await db.execute("UPDATE users SET free_photo=? WHERE id=?", (qty, uid))
    await db.commit()


async def set_alias(db, uid: int, alias: str) -> bool:
    await _ensure_user(db, uid)
    try:
        await db.execute("UPDATE users SET alias=? WHERE id=?", (alias, uid))
        await db.commit()
        return True
    except aiosqlite.IntegrityError:
        return False


async def get_uid_by_alias(db, alias: str) -> int:
    async with db.execute("SELECT id FROM users WHERE alias=?", (alias,)) as cur:
        row = await cur.fetchone()
    return row[0] if row else 0


async def get_alias(db, uid: int) -> str:
    await _ensure_user(db, uid)
    async with db.execute("SELECT alias FROM users WHERE id=?", (uid,)) as cur:
        row = await cur.fetchone()
    return row[0] if row else ''


async def log_payment(db, uid: int, rub: int = 0, stars: int = 0):
    if rub <= 0 and stars <= 0:
        return
    await _ensure_user(db, uid)
    await db.execute(
        "INSERT INTO payments(uid, rub, stars) VALUES(?,?,?)",
        (uid, rub, stars),
    )
    await db.commit()


async def get_referral_stats(db) -> list[dict]:
    async with db.execute(
        """
        SELECT p.id, p.alias, COUNT(c.id) AS cnt,
               IFNULL(SUM(pay.rub),0) AS rub,
               IFNULL(SUM(pay.stars),0) AS stars
        FROM users AS c
        JOIN users AS p ON c.ref_parent = p.id
        LEFT JOIN payments AS pay ON pay.uid = c.id
        GROUP BY p.id
        """
    ) as cur:
        rows = await cur.fetchall()
    return [
        {
            "id": r[0],
            "alias": r[1] or "",
            "count": r[2],
            "rub": r[3],
            "stars": r[4],
        }
        for r in rows
    ]


async def get_alias_stats(db, uid: int) -> tuple[int, int, int]:
    async with db.execute(
        """
        SELECT COUNT(c.id) AS cnt,
               IFNULL(SUM(pay.rub),0) AS rub,
               IFNULL(SUM(pay.stars),0) AS stars
        FROM users AS c
        LEFT JOIN payments AS pay ON pay.uid = c.id
        WHERE c.ref_parent=?
        """,
        (uid,),
    ) as cur:
        row = await cur.fetchone()
    return row if row else (0, 0, 0)

