from __future__ import annotations

import asyncio
import json
from decimal import Decimal
from functools import wraps
import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import aiosqlite
from dotenv import load_dotenv

load_dotenv(Path(__file__).with_name(".env"))

DB_PATH = Path(os.getenv("DB_PATH", Path(__file__).with_name("credits.db")))
_conn: aiosqlite.Connection | None = None

_db_lock = asyncio.Lock()
_lock_owner: asyncio.Task | None = None


def serialized(func):
    @wraps(func)
    async def wrapped(*args, **kwargs):
        global _lock_owner
        task = asyncio.current_task()
        if _lock_owner is task:
            return await func(*args, **kwargs)
        async with _db_lock:
            _lock_owner = task
            try:
                return await func(*args, **kwargs)
            finally:
                _lock_owner = None
    return wrapped


REQUIRED_TABLES = {
    "users",
    "payments",
    "subscriptions",
    "invite_links",
    "settings",
    "review_photos",
    "admin_states",
    "funnel_events",
    "followup_deliveries",
}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso_now() -> str:
    return utcnow().isoformat()


def parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def row_to_dict(row: aiosqlite.Row | None) -> dict[str, Any] | None:
    return dict(row) if row else None


@serialized
async def connection() -> aiosqlite.Connection:
    if _conn is None:
        await init_db()
    assert _conn is not None
    return _conn


@serialized
async def init_db() -> aiosqlite.Connection:
    global _conn
    if _conn is not None:
        return _conn
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    _conn = await aiosqlite.connect(DB_PATH, timeout=30)
    _conn.row_factory = aiosqlite.Row
    await _conn.execute("PRAGMA busy_timeout=30000")
    await _conn.execute("PRAGMA journal_mode=WAL")
    try:
        await create_tables()
    except BaseException:
        await _conn.close()
        _conn = None
        raise
    return _conn


@serialized
async def close_db() -> None:
    global _conn
    if _conn is not None:
        await _conn.close()
        _conn = None


async def _table_columns(db: aiosqlite.Connection, table: str) -> set[str]:
    async with db.execute(f"PRAGMA table_info({table})") as cur:
        return {row[1] for row in await cur.fetchall()}


async def _validate_existing_tables(db: aiosqlite.Connection) -> None:
    expected_columns = {
        "users": {"tg_user_id", "username", "full_name", "accepted_offer_at", "current_chat_id", "current_message_id", "current_message_kind", "created_at", "updated_at"},
        "payments": {"id", "tg_user_id", "payment_id", "provider", "plan_id", "amount_rub", "status", "confirmation_url", "metadata_json", "raw_json", "created_at", "paid_at", "finalized_at"},
        "subscriptions": {"tg_user_id", "access_until", "last_payment_id", "status", "updated_at"},
        "invite_links": {"id", "tg_user_id", "invite_link", "payment_id", "expire_at", "created_at", "used"},
        "settings": {"key", "value", "updated_at"},
        "review_photos": {"id", "file_id", "sort_order", "created_at"},
        "admin_states": {"tg_user_id", "state", "payload_json", "updated_at"},
    }
    for table, required_columns in expected_columns.items():
        columns = await _table_columns(db, table)
        if columns and not required_columns.issubset(columns):
            raise RuntimeError(f"Unsupported existing schema in {table}; database preserved. Back up and migrate explicitly.")
    await db.commit()


@serialized
async def create_tables() -> None:
    global _conn
    if _conn is None:
        _conn = await aiosqlite.connect(DB_PATH)
        _conn.row_factory = aiosqlite.Row
    db = _conn
    await _validate_existing_tables(db)
    await db.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
            tg_user_id INTEGER PRIMARY KEY,
            username TEXT,
            full_name TEXT,
            accepted_offer_at TEXT,
            current_chat_id INTEGER DEFAULT 0,
            current_message_id INTEGER DEFAULT 0,
            current_message_kind TEXT DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tg_user_id INTEGER NOT NULL,
            payment_id TEXT UNIQUE NOT NULL,
            provider TEXT NOT NULL DEFAULT 'yookassa',
            plan_id TEXT NOT NULL,
            amount_rub INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            confirmation_url TEXT,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            raw_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            paid_at TEXT,
            finalized_at TEXT
        );

        CREATE TABLE IF NOT EXISTS subscriptions (
            tg_user_id INTEGER PRIMARY KEY,
            access_until TEXT NOT NULL,
            last_payment_id TEXT,
            status TEXT NOT NULL DEFAULT 'active',
            renewal_notice_sent_at TEXT,
            expiration_notice_sent_at TEXT,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS invite_links (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tg_user_id INTEGER NOT NULL,
            invite_link TEXT NOT NULL,
            payment_id TEXT,
            expire_at TEXT,
            created_at TEXT NOT NULL,
            used INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS review_photos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_id TEXT NOT NULL,
            sort_order INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS admin_states (
            tg_user_id INTEGER PRIMARY KEY,
            state TEXT NOT NULL,
            payload_json TEXT NOT NULL DEFAULT '{}',
            updated_at TEXT NOT NULL
        );
        """
    )
    await db.executescript(
        """
        CREATE TABLE IF NOT EXISTS funnel_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tg_user_id INTEGER NOT NULL,
            event_name TEXT NOT NULL,
            source TEXT,
            payload_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            event_key TEXT UNIQUE
        );
        CREATE INDEX IF NOT EXISTS funnel_event_user_time
            ON funnel_events(event_name, tg_user_id, created_at);
        CREATE INDEX IF NOT EXISTS funnel_user_time
            ON funnel_events(tg_user_id, created_at);
        CREATE TABLE IF NOT EXISTS followup_deliveries (
            tg_user_id INTEGER PRIMARY KEY,
            claimed_at TEXT NOT NULL,
            sent_at TEXT
        );
        CREATE TABLE IF NOT EXISTS lesson_quizzes (
            tg_user_id INTEGER PRIMARY KEY, token TEXT NOT NULL UNIQUE,
            lesson_url TEXT NOT NULL, created_at TEXT NOT NULL,
            answer TEXT, discount_percent INTEGER, answered_at TEXT,
            quote_json TEXT, checkout_group_id TEXT, redeemed_payment_id TEXT
        );
        """
    )
    await _add_missing_columns(db, "users", {
        "first_source": "TEXT",
        "accepted_offer_url": "TEXT",
        "accepted_offer_version": "TEXT",
    })
    await _add_missing_columns(
        db,
        "subscriptions",
        {
            "renewal_notice_sent_at": "TEXT",
            "expiration_notice_sent_at": "TEXT",
        },
    )
    old_payment_columns = await _table_columns(db, "payments")
    await _add_missing_columns(db, "payments", {"checked_at": "TEXT", "access_notified_at": "TEXT", "delivery_attempt_at": "TEXT"})
    if "access_notified_at" not in old_payment_columns:
        # Historical fulfilled purchases must not trigger a notification campaign.
        await db.execute("UPDATE payments SET access_notified_at=finalized_at WHERE finalized_at IS NOT NULL")
    await _add_missing_columns(db, "users", {"goal": "TEXT", "marketing_opt_out": "INTEGER NOT NULL DEFAULT 0"})
    await _add_missing_columns(db, "users", {
        "lesson_chat_id": "INTEGER NOT NULL DEFAULT 0",
        "lesson_message_id": "INTEGER NOT NULL DEFAULT 0",
        "lesson_url": "TEXT",
    })
    await _add_missing_columns(db, "invite_links", {"requires_approval": "INTEGER NOT NULL DEFAULT 0"})
    await db.executescript("""
        CREATE TABLE IF NOT EXISTS tariffs (
            code TEXT PRIMARY KEY, title TEXT NOT NULL, amount_rub INTEGER NOT NULL,
            days INTEGER NOT NULL, enabled INTEGER NOT NULL DEFAULT 1, updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS checkout_requests (
            id TEXT PRIMARY KEY, tg_user_id INTEGER NOT NULL, plan_id TEXT NOT NULL,
            title TEXT NOT NULL, amount_rub INTEGER NOT NULL, days INTEGER NOT NULL,
            request_json TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'creating',
            payment_id TEXT UNIQUE, created_at TEXT NOT NULL, checked_at TEXT,
            auto_opt_in INTEGER NOT NULL DEFAULT 0, cycle_until TEXT, consent_key TEXT,
            failure_reason TEXT
        );
        CREATE UNIQUE INDEX IF NOT EXISTS checkout_cycle ON checkout_requests(tg_user_id,cycle_until)
            WHERE cycle_until IS NOT NULL;
        CREATE UNIQUE INDEX IF NOT EXISTS checkout_open_user ON checkout_requests(tg_user_id)
            WHERE status IN ('creating','created');
        CREATE TABLE IF NOT EXISTS renewal_contracts (
            tg_user_id INTEGER PRIMARY KEY, enabled INTEGER NOT NULL DEFAULT 0,
            payment_method_id TEXT, plan_id TEXT NOT NULL, amount_rub INTEGER NOT NULL,
            days INTEGER NOT NULL, consent_key TEXT NOT NULL, consented_at TEXT NOT NULL,
            disabled_at TEXT, disabled_reason TEXT, warned_until TEXT, warned_at TEXT
        );
        CREATE TABLE IF NOT EXISTS notification_attempts (
            key TEXT PRIMARY KEY, tg_user_id INTEGER NOT NULL, claimed_at TEXT NOT NULL, sent_at TEXT
        );
        CREATE TABLE IF NOT EXISTS activity_events (
            chat_id INTEGER NOT NULL, message_id INTEGER NOT NULL, tg_user_id INTEGER NOT NULL,
            sent_at TEXT NOT NULL, has_file INTEGER NOT NULL,
            PRIMARY KEY(chat_id,message_id)
        );
        CREATE INDEX IF NOT EXISTS activity_user_time ON activity_events(tg_user_id,sent_at);
        CREATE TABLE IF NOT EXISTS admin_audit (
            id INTEGER PRIMARY KEY, admin_id INTEGER NOT NULL, action TEXT NOT NULL,
            target TEXT NOT NULL, created_at TEXT NOT NULL
        );
    """)
    await _add_missing_columns(db,"renewal_contracts",{"warned_until":"TEXT","warned_at":"TEXT"})
    await _add_missing_columns(db,"checkout_requests",{"offer_url":"TEXT","offer_version":"TEXT","offer_presented_at":"TEXT"})
    await _add_missing_columns(db,"checkout_requests",{
        "checkout_group_id":"TEXT", "payment_route":"TEXT NOT NULL DEFAULT 'legacy'",
    })
    # Existing payloads and idempotence keys remain unchanged. A manual purchase
    # can offer one SBP invoice and one Smart Payment invoice for the same quote.
    async with db.execute("SELECT id,request_json FROM checkout_requests WHERE payment_route='legacy'") as cur:
        previous = await cur.fetchall()
    for row in previous:
        payload = json.loads(row['request_json'])
        await db.execute("UPDATE checkout_requests SET checkout_group_id=COALESCE(checkout_group_id,id),payment_route=? WHERE id=?",
                         (checkout_route(payload),row['id']))
    async with db.execute("SELECT sql FROM sqlite_master WHERE name='checkout_open_user'") as cur:
        index = await cur.fetchone()
    if index and 'payment_route' not in index['sql']:
        await db.execute("DROP INDEX checkout_open_user")
    await db.execute("CREATE UNIQUE INDEX IF NOT EXISTS checkout_open_user ON checkout_requests(tg_user_id,payment_route) WHERE status IN ('creating','created')")
    await db.execute("CREATE UNIQUE INDEX IF NOT EXISTS checkout_creating_user ON checkout_requests(tg_user_id) WHERE status='creating'")
    await db.execute("INSERT OR IGNORE INTO tariffs VALUES ('creative_30','Креативный мозг — 30 дней',400,30,1,?)", (iso_now(),))
    await db.commit()


async def _add_missing_columns(db: aiosqlite.Connection, table: str, columns: dict[str, str]) -> None:
    existing_columns = await _table_columns(db, table)
    for column, definition in columns.items():
        if column not in existing_columns:
            await db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


@serialized
async def upsert_user(tg_user_id: int, username: str | None = None, full_name: str | None = None) -> None:
    db = await connection()
    now = iso_now()
    await db.execute(
        """
        INSERT INTO users(tg_user_id, username, full_name, created_at, updated_at)
        VALUES(?, ?, ?, ?, ?)
        ON CONFLICT(tg_user_id) DO UPDATE SET
            username = excluded.username,
            full_name = excluded.full_name,
            updated_at = excluded.updated_at
        """,
        (tg_user_id, username or "", full_name or "", now, now),
    )
    await db.commit()


@serialized
async def accept_offer(tg_user_id: int, version: str = "1") -> None:
    db = await connection()
    now = iso_now()
    await db.execute(
        "UPDATE users SET accepted_offer_at = COALESCE(accepted_offer_at, ?), accepted_offer_url = ?, accepted_offer_version = ?, updated_at = ? WHERE tg_user_id = ?",
        (now, await get_offer_url(), version, now, tg_user_id),
    )
    await db.commit()


@serialized
async def has_accepted_offer(tg_user_id: int, version: str = "1") -> bool:
    db = await connection()
    async with db.execute("SELECT * FROM users WHERE tg_user_id = ?", (tg_user_id,)) as cur:
        row = await cur.fetchone()
    url = await get_offer_url()
    return bool(row and url and row["accepted_offer_at"]
                and row["accepted_offer_url"] == url and row["accepted_offer_version"] == version)


@serialized
async def set_current_message(tg_user_id: int, chat_id: int, message_id: int, kind: str) -> None:
    db = await connection()
    await db.execute(
        "UPDATE users SET current_chat_id = ?, current_message_id = ?, current_message_kind = ?, updated_at = ? WHERE tg_user_id = ?",
        (chat_id, message_id, kind, iso_now(), tg_user_id),
    )
    await db.commit()


@serialized
async def get_current_message(tg_user_id: int) -> dict[str, Any] | None:
    db = await connection()
    async with db.execute(
        "SELECT current_chat_id, current_message_id, current_message_kind FROM users WHERE tg_user_id = ?",
        (tg_user_id,),
    ) as cur:
        return row_to_dict(await cur.fetchone())


@serialized
async def clear_current_message(tg_user_id: int) -> None:
    await set_current_message(tg_user_id, 0, 0, "")


@serialized
async def set_lesson_message(tg_user_id: int, chat_id: int, message_id: int, url: str) -> None:
    db = await connection()
    await db.execute(
        "UPDATE users SET lesson_chat_id=?, lesson_message_id=?, lesson_url=? WHERE tg_user_id=?",
        (chat_id, message_id, url, tg_user_id),
    )
    await db.commit()


@serialized
async def get_lesson_message(tg_user_id: int) -> dict[str, Any] | None:
    db = await connection()
    async with db.execute(
        "SELECT lesson_chat_id, lesson_message_id, lesson_url FROM users WHERE tg_user_id=?",
        (tg_user_id,),
    ) as cur:
        return row_to_dict(await cur.fetchone())


@serialized
async def get_setting(key: str, default: Any = None) -> Any:
    db = await connection()
    async with db.execute("SELECT value FROM settings WHERE key = ?", (key,)) as cur:
        row = await cur.fetchone()
    return row["value"] if row else default


@serialized
async def set_setting(key: str, value: str) -> None:
    db = await connection()
    await db.execute(
        "INSERT INTO settings(key, value, updated_at) VALUES(?, ?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
        (key, value, iso_now()),
    )
    await db.commit()


@serialized
async def get_offer_url() -> str | None:
    return await get_setting("offer_url")


@serialized
async def set_offer_url(url: str) -> None:
    await set_setting("offer_url", url)


@serialized
async def get_intro_video_file_id() -> str | None:
    return await get_setting("intro_video_file_id")


@serialized
async def set_intro_video_file_id(file_id: str) -> None:
    await set_setting("intro_video_file_id", file_id)


@serialized
async def clear_intro_video_file_id() -> None:
    db = await connection()
    await db.execute("DELETE FROM settings WHERE key = 'intro_video_file_id'")
    await db.commit()


@serialized
async def count_review_photos() -> int:
    db = await connection()
    async with db.execute("SELECT COUNT(*) AS value FROM review_photos") as cur:
        return int((await cur.fetchone())["value"])


@serialized
async def add_review_photo(file_id: str) -> None:
    db = await connection()
    async with db.execute("SELECT COALESCE(MAX(sort_order), 0) + 10 AS next_order FROM review_photos") as cur:
        row = await cur.fetchone()
    await db.execute(
        "INSERT INTO review_photos(file_id, sort_order, created_at) VALUES(?, ?, ?)",
        (file_id, int(row["next_order"]), iso_now()),
    )
    await db.commit()


@serialized
async def delete_review_photo(photo_id: int) -> bool:
    db = await connection()
    cur = await db.execute("DELETE FROM review_photos WHERE id = ?", (photo_id,))
    deleted = cur.rowcount > 0
    await db.commit()
    return deleted


@serialized
async def list_review_photos() -> list[dict[str, Any]]:
    db = await connection()
    async with db.execute("SELECT id, file_id, sort_order, created_at FROM review_photos ORDER BY sort_order, id") as cur:
        return [dict(row) for row in await cur.fetchall()]


@serialized
async def clear_review_photos() -> None:
    db = await connection()
    await db.execute("DELETE FROM review_photos")
    await db.commit()


@serialized
async def create_payment(
    tg_user_id: int,
    payment_id: str,
    plan_id: str,
    amount_rub: int,
    confirmation_url: str | None,
    metadata: dict[str, Any],
    raw: dict[str, Any],
) -> None:
    db = await connection()
    await db.execute(
        """
        INSERT INTO payments(tg_user_id, payment_id, plan_id, amount_rub, status, confirmation_url, metadata_json, raw_json, created_at)
        VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(payment_id) DO UPDATE SET
            status = excluded.status,
            confirmation_url = excluded.confirmation_url,
            raw_json = excluded.raw_json
        """,
        (
            tg_user_id,
            payment_id,
            plan_id,
            amount_rub,
            str(raw.get("status") or "pending"),
            confirmation_url,
            json.dumps(metadata, ensure_ascii=False),
            json.dumps(raw, ensure_ascii=False),
            iso_now(),
        ),
    )
    await db.commit()


@serialized
async def get_payment(payment_id: str) -> dict[str, Any] | None:
    db = await connection()
    async with db.execute("SELECT * FROM payments WHERE payment_id = ?", (payment_id,)) as cur:
        return row_to_dict(await cur.fetchone())


@serialized
async def get_latest_pending_payment(tg_user_id: int) -> dict[str, Any] | None:
    db = await connection()
    async with db.execute(
        "SELECT * FROM payments WHERE tg_user_id = ? AND finalized_at IS NULL AND status IN ('pending', 'waiting_for_capture', 'succeeded') ORDER BY id DESC LIMIT 1",
        (tg_user_id,),
    ) as cur:
        return row_to_dict(await cur.fetchone())


@serialized
async def list_unfinalized_payments(tg_user_id: int) -> list[dict]:
    db = await connection()
    async with db.execute(
        "SELECT * FROM payments WHERE tg_user_id = ? AND finalized_at IS NULL AND status != 'canceled'",
        (tg_user_id,),
    ) as cur:
        return [dict(row) for row in await cur.fetchall()]


@serialized
async def update_payment_status(payment_id: str, status: str, raw: dict[str, Any]) -> None:
    db = await connection()
    paid_at = iso_now() if status == "succeeded" else None
    await db.execute(
        """
        UPDATE payments
        SET status = ?, raw_json = ?, paid_at = COALESCE(paid_at, ?), checked_at = ?
        WHERE payment_id = ? AND finalized_at IS NULL
        """,
        (status, json.dumps(raw, ensure_ascii=False), paid_at, iso_now(), payment_id),
    )
    if status == "canceled":
        await db.execute("UPDATE checkout_requests SET status='canceled' WHERE payment_id=?", (payment_id,))
    await db.commit()


@serialized
async def finalize_payment_once(
    payment_id: str,
    tg_user_id: int,
    expected_project_key: str,
    expected_kind: str,
    plan_id: str,
    days: int,
) -> dict[str, Any]:
    db = await connection()
    await db.execute("BEGIN IMMEDIATE")
    try:
        async with db.execute("SELECT * FROM payments WHERE payment_id = ?", (payment_id,)) as cur:
            payment = await cur.fetchone()
        if not payment:
            await db.rollback()
            return {"ok": False, "error": "payment_not_found"}
        payment_dict = dict(payment)
        metadata = json.loads(payment_dict.get("metadata_json") or "{}")
        if not 1 <= days <= 36500 or str(metadata.get("days")) != str(days):
            await db.rollback()
            return {"ok": False, "error": "duration_mismatch"}
        if int(payment_dict["tg_user_id"]) != int(tg_user_id):
            await db.rollback()
            return {"ok": False, "error": "payment_user_mismatch"}
        if payment_dict["plan_id"] != plan_id:
            await db.rollback()
            return {"ok": False, "error": "plan_mismatch"}
        if (
            metadata.get("project_key") != expected_project_key
            or str(metadata.get("user_id")) != str(tg_user_id)
            or metadata.get("kind") != expected_kind
        ):
            await db.rollback()
            return {"ok": False, "error": "metadata_mismatch"}

        async with db.execute("SELECT * FROM subscriptions WHERE tg_user_id = ?", (tg_user_id,)) as cur:
            sub = await cur.fetchone()
        if payment_dict.get("finalized_at"):
            await db.commit()
            return {
                "ok": True,
                "already_finalized": True,
                "access_until": dict(sub)["access_until"] if sub else None,
                "payment": payment_dict,
            }

        if payment_dict["status"] != "succeeded":
            await db.rollback()
            return {"ok": False, "error": "payment_not_succeeded"}

        now_dt = utcnow()
        current_until = parse_dt(sub["access_until"]) if sub else None
        base = current_until if sub and sub["status"] == "active" and current_until and current_until > now_dt else now_dt
        access_until = (base + timedelta(days=days)).isoformat()
        now = iso_now()
        await db.execute(
            """
            INSERT INTO subscriptions(tg_user_id, access_until, last_payment_id, status, updated_at)
            VALUES(?, ?, ?, 'active', ?)
            ON CONFLICT(tg_user_id) DO UPDATE SET
                access_until = excluded.access_until,
                last_payment_id = excluded.last_payment_id,
                status = 'active',
                renewal_notice_sent_at = NULL,
                expiration_notice_sent_at = NULL,
                updated_at = excluded.updated_at
            """,
            (tg_user_id, access_until, payment_id, now),
        )
        await db.execute(
            "UPDATE payments SET finalized_at = ?, paid_at = COALESCE(paid_at, ?), status = 'succeeded' WHERE payment_id = ?",
            (now, now, payment_id),
        )
        async with db.execute("SELECT * FROM checkout_requests WHERE payment_id=?",(payment_id,)) as cur:
            checkout=await cur.fetchone()
        if checkout and checkout['offer_presented_at']:
            # A URL button does not send a callback. Record acceptance only once the
            # customer has completed payment after the offer was displayed.
            await db.execute("UPDATE users SET accepted_offer_at=?,accepted_offer_url=?,accepted_offer_version=? WHERE tg_user_id=?",
                             (now,checkout['offer_url'],checkout['offer_version'],tg_user_id))
            await _insert_funnel_event(tg_user_id,'offer_accepted',
                                      {'source':'payment_after_offer','version':checkout['offer_version']},
                                      event_key='offer_paid:'+payment_id)
            await db.execute("UPDATE renewal_contracts SET enabled=0,disabled_at=?,disabled_reason='manual_payment' WHERE tg_user_id=?",
                             (now,tg_user_id))
        await _insert_funnel_event(tg_user_id, "payment_succeeded",
                                   {"payment_id": payment_id, "amount_rub": payment_dict["amount_rub"]},
                                   event_key=f"paid:{payment_id}")
        await _insert_funnel_event(tg_user_id, "access_granted",
                                   {"payment_id": payment_id, "access_until": access_until},
                                   event_key=f"access:{payment_id}")
        await db.execute("UPDATE checkout_requests SET status='paid' WHERE payment_id=?", (payment_id,))
        if checkout and metadata.get('quiz_token'):
            await db.execute("""UPDATE lesson_quizzes SET redeemed_payment_id=COALESCE(redeemed_payment_id,?)
                WHERE tg_user_id=? AND token=? AND checkout_group_id=?""",
                (payment_id,tg_user_id,metadata['quiz_token'],checkout['checkout_group_id']))
        await _activate_saved_method(db, payment_dict)
        await db.commit()
        return {"ok": True, "already_finalized": False, "access_until": access_until, "payment": payment_dict}
    except BaseException:
        await db.rollback()
        raise


@serialized
async def has_successful_payment(tg_user_id: int) -> bool:
    db = await connection()
    async with db.execute("SELECT 1 FROM payments WHERE tg_user_id = ? AND (finalized_at IS NOT NULL OR status = 'succeeded') LIMIT 1", (tg_user_id,)) as cur:
        return await cur.fetchone() is not None


@serialized
async def get_subscription(tg_user_id: int) -> dict[str, Any] | None:
    db = await connection()
    async with db.execute("SELECT * FROM subscriptions WHERE tg_user_id = ?", (tg_user_id,)) as cur:
        return row_to_dict(await cur.fetchone())


@serialized
async def grant_access(tg_user_id: int, days: int, source: str = "admin", payment_id: str | None = None) -> dict[str, Any]:
    db = await connection()
    if type(tg_user_id) is not int or not 0 < tg_user_id < 2**63 or type(days) is not int or not 1 <= days <= 36500:
        raise ValueError("Invalid user or duration")
    now_dt = utcnow()
    sub = await get_subscription(tg_user_id)
    current_until = parse_dt(sub["access_until"]) if sub else None
    base = current_until if sub and sub["status"] == "active" and current_until and current_until > now_dt else now_dt
    access_until = (base + timedelta(days=days)).isoformat()
    await db.execute(
        """
        INSERT INTO subscriptions(tg_user_id, access_until, last_payment_id, status, updated_at)
        VALUES(?, ?, ?, 'active', ?)
        ON CONFLICT(tg_user_id) DO UPDATE SET
            access_until = excluded.access_until,
            last_payment_id = excluded.last_payment_id,
            status = 'active',
            renewal_notice_sent_at = NULL,
            expiration_notice_sent_at = NULL,
            updated_at = excluded.updated_at
        """,
        (tg_user_id, access_until, payment_id or source, iso_now()),
    )
    await _insert_funnel_event(tg_user_id, "access_granted", {"source": source, "days": days, "access_until": access_until})
    await db.commit()
    return {"tg_user_id": tg_user_id, "access_until": access_until, "status": "active"}


@serialized
async def revoke_access(tg_user_id: int) -> None:
    db = await connection()
    await db.execute(
        """
        INSERT INTO subscriptions(tg_user_id, access_until, status, updated_at)
        VALUES(?, ?, 'revoked', ?)
        ON CONFLICT(tg_user_id) DO UPDATE SET
            access_until = excluded.access_until,
            status = 'revoked',
            updated_at = excluded.updated_at
        """,
        (tg_user_id, utcnow().isoformat(), iso_now()),
    )
    await db.execute("UPDATE renewal_contracts SET enabled=0,disabled_at=?,disabled_reason='access_revoked' WHERE tg_user_id=?", (iso_now(),tg_user_id))
    await db.commit()


@serialized
async def list_expired_subscriptions(now: str | datetime | None = None) -> list[dict[str, Any]]:
    db = await connection()
    now_iso = now.isoformat() if isinstance(now, datetime) else (now or iso_now())
    async with db.execute(
        "SELECT * FROM subscriptions WHERE status = 'active' AND julianday(access_until) <= julianday(?)",
        (now_iso,),
    ) as cur:
        return [dict(row) for row in await cur.fetchall()]


@serialized
async def list_subscriptions_due_for_renewal_notice(now: str | datetime | None = None, days_before: int = 3) -> list[dict[str, Any]]:
    db = await connection()
    now_dt = parse_dt(now.isoformat() if isinstance(now, datetime) else now) or utcnow()
    threshold = (now_dt + timedelta(days=days_before)).isoformat()
    now_value = now_dt.isoformat()
    async with db.execute(
        """
        SELECT * FROM subscriptions
        WHERE status = 'active'
          AND julianday(access_until) > julianday(?)
          AND julianday(access_until) <= julianday(?)
          AND renewal_notice_sent_at IS NULL
        """,
        (now_value, threshold),
    ) as cur:
        return [dict(row) for row in await cur.fetchall()]


@serialized
async def mark_renewal_notice_sent(tg_user_id: int) -> None:
    db = await connection()
    now = iso_now()
    await db.execute("UPDATE subscriptions SET renewal_notice_sent_at = ?, updated_at = ? WHERE tg_user_id = ?", (now, now, tg_user_id))
    await db.commit()


@serialized
async def mark_subscription_expired(tg_user_id: int) -> None:
    db = await connection()
    now = iso_now()
    await db.execute("UPDATE subscriptions SET status = 'expired', updated_at = ? WHERE tg_user_id = ?", (now, tg_user_id))
    await db.commit()


@serialized
async def pending_expiration_notices():
    con = await connection()
    async with con.execute("SELECT * FROM subscriptions WHERE status='expired' AND expiration_notice_sent_at IS NULL") as cur:
        return [dict(row) for row in await cur.fetchall()]


@serialized
async def mark_expiration_notice_sent(tg_user_id, access_until):
    con = await connection()
    await con.execute("UPDATE subscriptions SET expiration_notice_sent_at=?,updated_at=? WHERE tg_user_id=? AND status='expired' AND access_until=?",
                      (iso_now(),iso_now(),tg_user_id,access_until))
    await con.commit()


@serialized
async def supersede_used_discount_checkouts(tg_user_id):
    """Retire unused promo options after a purchase; retain their provider history."""
    con = await connection()
    if not await has_successful_payment(tg_user_id):
        return
    async with con.execute("SELECT c.* FROM checkout_requests c JOIN payments p ON p.payment_id=c.payment_id WHERE c.tg_user_id=? AND c.status='created' AND p.status='pending' AND p.finalized_at IS NULL",(tg_user_id,)) as cur:
        rows = await cur.fetchall()
    for row in rows:
        if (json.loads(row['request_json']).get('metadata') or {}).get('quiz_token'):
            await con.execute("UPDATE checkout_requests SET status='superseded' WHERE id=?",(row['id'],))
    await con.commit()


@serialized
async def prepare_subscription_checkout(tg_user_id, access_until, tariff, payload, direct_offer):
    """Freeze a full-price manual renewal without reusing a discounted quote."""
    con = await connection()
    await con.execute('BEGIN IMMEDIATE')
    try:
        sub = await get_subscription(tg_user_id)
        published = next((row for row in await list_tariffs() if row['code']==tariff.code),None)
        if not sub or sub['access_until']!=access_until or sub['status'] not in {'active','expired'}:
            await con.commit()
            return None
        meta = payload.get('metadata') or {}
        if (not published or (tariff.amount_rub,tariff.days)!=(published['amount_rub'],published['days'])
                or Decimal(payload['amount']['value'])!=Decimal(str(tariff.amount_rub))
                or meta.get('purpose')!='subscription_renewal' or meta.get('subscription_until')!=access_until
                or meta.get('quiz_token') or meta.get('discount_percent') or payload.get('save_payment_method')
                or payload.get('payment_method_id') or payload.get('payment_token')):
            raise ValueError('Renewal must use the current full-price manual tariff')
        async with con.execute("SELECT * FROM checkout_requests WHERE tg_user_id=? AND status IN ('creating','created') ORDER BY created_at",(tg_user_id,)) as cur:
            orders = [dict(row) for row in await cur.fetchall()]
        for order in orders:
            request = json.loads(order['request_json'])
            if (request.get('metadata') or {}).get('purpose')=='subscription_renewal' and (request.get('metadata') or {}).get('subscription_until')==access_until:
                if ((order['plan_id'],order['amount_rub'],order['days'],order['offer_url'],order['offer_version'])
                        ==(tariff.code,tariff.amount_rub,tariff.days,*direct_offer)):
                    await con.commit()
                    return order
            if (not order['payment_id'] or order['auto_opt_in'] or order['cycle_until']
                    or request.get('save_payment_method') or request.get('payment_method_id') or request.get('payment_token')):
                await con.commit()
                return None
        # The caller has refreshed all invoices. Known unpaid manual alternatives
        # can be superseded, but unknown requests or money in capture cannot.
        async with con.execute("""SELECT 1 FROM payments p LEFT JOIN checkout_requests c ON c.payment_id=p.payment_id
            WHERE p.tg_user_id=? AND p.finalized_at IS NULL AND p.status!='canceled'
            AND (c.id IS NULL OR p.status!='pending') LIMIT 1""",(tg_user_id,)) as cur:
            if await cur.fetchone():
                await con.commit()
                return None
        await con.execute("UPDATE checkout_requests SET status='superseded' WHERE tg_user_id=? AND status='created'",(tg_user_id,))
        order_id = uuid.uuid4().hex
        await con.execute("""INSERT INTO checkout_requests(id,tg_user_id,plan_id,title,amount_rub,days,
            request_json,created_at,offer_url,offer_version,checkout_group_id,payment_route) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (order_id,tg_user_id,tariff.code,tariff.title,tariff.amount_rub,tariff.days,
             json.dumps(payload,ensure_ascii=False),iso_now(),*direct_offer,order_id,checkout_route(payload)))
        await con.commit()
        return await get_checkout(order_id)
    except BaseException:
        await con.rollback()
        raise


@serialized
async def get_invite_link_by_payment(payment_id: str) -> dict[str, Any] | None:
    db = await connection()
    async with db.execute(
        "SELECT * FROM invite_links WHERE payment_id = ? ORDER BY id DESC LIMIT 1",
        (payment_id,),
    ) as cur:
        return row_to_dict(await cur.fetchone())


@serialized
async def save_invite_link(tg_user_id: int, invite_link: str, payment_id: str | None = None, expire_at: str | None = None) -> None:
    db = await connection()
    await db.execute(
        "INSERT INTO invite_links(tg_user_id, invite_link, payment_id, expire_at, created_at, requires_approval) VALUES(?, ?, ?, ?, ?, 1)",
        (tg_user_id, invite_link, payment_id, expire_at, iso_now()),
    )
    await db.commit()


@serialized
async def stats() -> dict[str, Any]:
    db = await connection()
    queries = {
        "users_count": "SELECT COUNT(*) AS value FROM users",
        "accepted_offer_count": "SELECT COUNT(*) AS value FROM users WHERE accepted_offer_at IS NOT NULL",
        "paid_count": "SELECT COUNT(*) AS value FROM payments WHERE finalized_at IS NOT NULL",
        "revenue_rub": "SELECT COALESCE(SUM(amount_rub), 0) AS value FROM payments WHERE finalized_at IS NOT NULL",
        "active_subscriptions_count": "SELECT COUNT(*) AS value FROM subscriptions WHERE status = 'active' AND julianday(access_until) > julianday(?)",
    }
    result: dict[str, Any] = {}
    for key, sql in queries.items():
        params = (iso_now(),) if "access_until" in sql else ()
        async with db.execute(sql, params) as cur:
            result[key] = (await cur.fetchone())["value"]
    result['revenue_rub']=round(result['revenue_rub'],2)
    return result


@serialized
async def set_admin_state(tg_user_id: int, state: str, payload: dict[str, Any] | None = None) -> None:
    db = await connection()
    await db.execute(
        """
        INSERT INTO admin_states(tg_user_id, state, payload_json, updated_at)
        VALUES(?, ?, ?, ?)
        ON CONFLICT(tg_user_id) DO UPDATE SET
            state = excluded.state,
            payload_json = excluded.payload_json,
            updated_at = excluded.updated_at
        """,
        (tg_user_id, state, json.dumps(payload or {}, ensure_ascii=False), iso_now()),
    )
    await db.commit()


@serialized
async def get_admin_state(tg_user_id: int) -> dict[str, Any] | None:
    db = await connection()
    async with db.execute("SELECT * FROM admin_states WHERE tg_user_id = ?", (tg_user_id,)) as cur:
        row = await cur.fetchone()
    if not row:
        return None
    data = dict(row)
    data["payload"] = json.loads(data.pop("payload_json") or "{}")
    return data


@serialized
async def clear_admin_state(tg_user_id: int) -> None:
    db = await connection()
    await db.execute("DELETE FROM admin_states WHERE tg_user_id = ?", (tg_user_id,))
    await db.commit()



EVENT_NAMES = {
    "start", "lead_magnet_sent", "product_opened", "buy_clicked", "offer_accepted",
    "payment_created", "payment_succeeded", "access_granted", "followup_sent",
    "renewal_started", "expired", "tariffs_opened", "tariff_selected", "goal_selected",
    "channel_joined", "auto_enabled", "auto_disabled", "auto_failed", "marketing_opt_out",
    "quiz_answered",
}


@serialized
async def get_lead_magnet_url() -> str | None:
    return await get_setting("lead_magnet_url")


@serialized
async def set_lead_magnet_url(url: str) -> None:
    await set_setting("lead_magnet_url", url)


@serialized
async def clear_lead_magnet_url() -> None:
    db = await connection()
    await db.execute("DELETE FROM settings WHERE key = 'lead_magnet_url'")
    await db.commit()


async def _insert_funnel_event(tg_user_id: int, event_name: str, payload: dict | None = None,
                               source: str | None = None, event_key: str | None = None) -> None:
    """Insert without committing, so finalization and its analytics remain atomic."""
    if event_name not in EVENT_NAMES:
        raise ValueError("Unknown funnel event")
    db = await connection()
    if source is None:
        async with db.execute("SELECT first_source FROM users WHERE tg_user_id = ?", (tg_user_id,)) as cur:
            row = await cur.fetchone()
        source = row["first_source"] if row and row["first_source"] else "unknown"
    await db.execute(
        "INSERT OR IGNORE INTO funnel_events(tg_user_id, event_name, source, payload_json, created_at, event_key) VALUES(?, ?, ?, ?, ?, ?)",
        (tg_user_id, event_name, source, json.dumps(payload or {}, ensure_ascii=False), iso_now(), event_key),
    )


@serialized
async def record_event(tg_user_id: int, event_name: str, payload: dict | None = None,
                       source: str | None = None, event_key: str | None = None) -> None:
    await _insert_funnel_event(tg_user_id, event_name, payload, source, event_key)
    await (await connection()).commit()


@serialized
async def record_start(tg_user_id: int, source: str) -> None:
    db = await connection()
    await db.execute("UPDATE users SET first_source = COALESCE(first_source, ?) WHERE tg_user_id = ?", (source, tg_user_id))
    await _insert_funnel_event(tg_user_id, "start", source=source)
    await db.commit()


FOLLOWUP_ELIGIBLE_SQL = """
    SELECT e.tg_user_id FROM funnel_events e
    WHERE e.event_name = 'lead_magnet_sent'
      AND NOT EXISTS (SELECT 1 FROM users u WHERE u.tg_user_id=e.tg_user_id AND u.marketing_opt_out=1)
      AND NOT EXISTS (SELECT 1 FROM followup_deliveries f WHERE f.tg_user_id = e.tg_user_id)
      AND NOT EXISTS (SELECT 1 FROM payments p WHERE p.tg_user_id = e.tg_user_id
                      AND (p.finalized_at IS NOT NULL OR p.status = 'succeeded'))
      AND NOT EXISTS (SELECT 1 FROM subscriptions s WHERE s.tg_user_id = e.tg_user_id
                      AND s.status = 'active' AND julianday(s.access_until) > julianday(?))
    GROUP BY e.tg_user_id
    HAVING MIN(e.created_at) <= ? AND MIN(e.created_at) >= ?
"""


@serialized
async def followup_candidates(now: datetime | None = None) -> list[int]:
    db = await connection()
    now = now or utcnow()
    params = (now.isoformat(), (now - timedelta(hours=22)).isoformat(), (now - timedelta(hours=24)).isoformat())
    async with db.execute(FOLLOWUP_ELIGIBLE_SQL, params) as cur:
        return [int(row[0]) for row in await cur.fetchall()]


@serialized
async def claim_followup(tg_user_id: int) -> bool:
    """Persist an at-most-once attempt before Telegram I/O, including across restarts."""
    db = await connection()
    if tg_user_id not in await followup_candidates():
        return False
    cur = await db.execute("INSERT OR IGNORE INTO followup_deliveries(tg_user_id, claimed_at) VALUES(?, ?)", (tg_user_id, iso_now()))
    await db.commit()
    return cur.rowcount == 1


@serialized
async def mark_followup_sent(tg_user_id: int) -> None:
    db = await connection()
    await db.execute("UPDATE followup_deliveries SET sent_at = ? WHERE tg_user_id = ?", (iso_now(), tg_user_id))
    await _insert_funnel_event(tg_user_id, "followup_sent", event_key=f"followup:{tg_user_id}")
    await db.commit()


@serialized
async def get_lesson_quiz(tg_user_id):
    con=await connection()
    async with con.execute('SELECT * FROM lesson_quizzes WHERE tg_user_id=?',(tg_user_id,)) as cur:
        return row_to_dict(await cur.fetchone())


@serialized
async def prepare_lesson_quiz(tg_user_id,lesson_url):
    con=await connection()
    async with con.execute('SELECT 1 FROM followup_deliveries WHERE tg_user_id=?',(tg_user_id,)) as cur:
        if not await cur.fetchone():
            return None
    await con.execute('INSERT OR IGNORE INTO lesson_quizzes(tg_user_id,token,lesson_url,created_at) VALUES(?,?,?,?)',
                      (tg_user_id,uuid.uuid4().hex,lesson_url,iso_now()))
    await con.commit()
    return await get_lesson_quiz(tg_user_id)


@serialized
async def answer_lesson_quiz(tg_user_id,token,answer,offer_version,preferred_plan=None):
    """The first answer and the first available quote are durable and immutable."""
    con=await connection()
    await con.execute('BEGIN IMMEDIATE')
    try:
        quiz=await get_lesson_quiz(tg_user_id)
        if not quiz or quiz['token']!=token or (not quiz['answer'] and answer not in {'form','function','rule'}):
            await con.commit()
            return None
        if not quiz['answer']:
            percent=30 if answer=='rule' else 15
            await con.execute('UPDATE lesson_quizzes SET answer=?,discount_percent=?,answered_at=? WHERE tg_user_id=?',
                              (answer,percent,iso_now(),tg_user_id))
            await _insert_funnel_event(tg_user_id,'quiz_answered',{'answer':answer,'discount_percent':percent},event_key='quiz:'+token)
            quiz=await get_lesson_quiz(tg_user_id)
        if not quiz['quote_json']:
            plans=[row for row in await list_tariffs() if row['amount_rub']>=2]
            plan=next((row for row in plans if row['code']==preferred_plan),plans[0] if plans else None)
            offer=await get_offer_url()
            if plan and offer:
                quote=dict(plan,base_amount_rub=plan['amount_rub'],
                           amount_kopecks=plan['amount_rub']*(100-quiz['discount_percent']),
                           offer_url=offer,offer_version=offer_version)
                await con.execute('UPDATE lesson_quizzes SET quote_json=? WHERE tg_user_id=? AND quote_json IS NULL',
                                  (json.dumps(quote,ensure_ascii=False),tg_user_id))
        await con.commit()
        return await get_lesson_quiz(tg_user_id)
    except BaseException:
        await con.rollback()
        raise


@serialized
async def prepare_quiz_checkout(tg_user_id,token,payload):
    """Create a new discounted request; never rewrite a previous provider POST."""
    con=await connection()
    await con.execute('BEGIN IMMEDIATE')
    try:
        quiz=await get_lesson_quiz(tg_user_id)
        if (not quiz or quiz['token']!=token or not quiz['answer'] or not quiz['quote_json']
                or quiz['redeemed_payment_id'] or await has_successful_payment(tg_user_id)):
            await con.commit()
            return None
        quote=json.loads(quiz['quote_json'])
        if (Decimal(payload['amount']['value'])*100!=quote['amount_kopecks']
                or payload['metadata'].get('quiz_token')!=token or payload.get('save_payment_method')
                or payload.get('payment_method_id') or payload.get('payment_token')
                or checkout_route(payload)!='sbp'):
            raise ValueError('Discounted quote mismatch')
        async with con.execute("SELECT * FROM checkout_requests WHERE tg_user_id=? AND status IN ('creating','created') ORDER BY (status='creating') DESC,created_at",(tg_user_id,)) as cur:
            orders=[dict(row) for row in await cur.fetchall()]
        own=[row for row in orders if (json.loads(row['request_json']).get('metadata') or {}).get('quiz_token')==token]
        if own:
            await con.commit()
            return own[0]
        # Known unpaid manual quotes can be replaced locally. They remain in
        # payments for provider reconciliation; no provider cancellation is faked.
        for order in orders:
            request=json.loads(order['request_json'])
            if (not order['payment_id'] or order['auto_opt_in'] or order['cycle_until']
                    or request.get('save_payment_method') or request.get('payment_method_id')):
                await con.commit()
                return None
        async with con.execute("""SELECT p.* FROM payments p LEFT JOIN checkout_requests c ON c.payment_id=p.payment_id
            WHERE p.tg_user_id=? AND p.finalized_at IS NULL AND p.status!='canceled'
            AND (c.id IS NULL OR p.status!='pending') LIMIT 1""",(tg_user_id,)) as cur:
            if await cur.fetchone():
                await con.commit()
                return None
        await con.execute("UPDATE checkout_requests SET status='superseded' WHERE tg_user_id=? AND status='created'",(tg_user_id,))
        order_id=uuid.uuid4().hex
        await con.execute("""INSERT INTO checkout_requests(id,tg_user_id,plan_id,title,amount_rub,days,
            request_json,created_at,offer_url,offer_version,checkout_group_id,payment_route) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (order_id,tg_user_id,quote['code'],quote['title'],quote['amount_kopecks']/100,quote['days'],
             json.dumps(payload,ensure_ascii=False),iso_now(),quote['offer_url'],quote['offer_version'],order_id,'sbp'))
        await con.execute('UPDATE lesson_quizzes SET checkout_group_id=? WHERE tg_user_id=?',(order_id,tg_user_id))
        await con.commit()
        return await get_checkout(order_id)
    except BaseException:
        await con.rollback()
        raise


@serialized
async def get_user_path(tg_user_id: int, limit: int = 25) -> list[dict]:
    db = await connection()
    async with db.execute("SELECT * FROM funnel_events WHERE tg_user_id = ? ORDER BY id DESC LIMIT ?", (tg_user_id, limit)) as cur:
        return [dict(row) for row in await cur.fetchall()][::-1]


@serialized
async def funnel_stats() -> dict:
    db = await connection()
    result = await stats()
    async with db.execute("SELECT event_name, COUNT(*) total, COUNT(DISTINCT tg_user_id) users FROM funnel_events GROUP BY event_name") as cur:
        result["events"] = {row["event_name"]: dict(row) for row in await cur.fetchall()}
    result["conversions"] = {}
    for event in ("start", "product_opened"):
        async with db.execute("""
            SELECT COUNT(DISTINCT e.tg_user_id) FROM funnel_events e
            WHERE e.event_name = ? AND EXISTS (
                SELECT 1 FROM funnel_events p WHERE p.event_name = 'payment_succeeded'
                AND p.tg_user_id = e.tg_user_id AND p.created_at >= e.created_at)
        """, (event,)) as cur:
            result["conversions"][event] = int((await cur.fetchone())[0])
    async with db.execute("""
        SELECT COALESCE(u.first_source, 'unknown') source,
            SUM(EXISTS(SELECT 1 FROM funnel_events e WHERE e.tg_user_id = u.tg_user_id AND e.event_name = 'start')) starts,
            SUM(EXISTS(SELECT 1 FROM payments p WHERE p.tg_user_id = u.tg_user_id AND p.finalized_at IS NOT NULL)) paid
        FROM users u GROUP BY COALESCE(u.first_source, 'unknown') ORDER BY starts DESC, source
    """) as cur:
        result["sources"] = [dict(row) for row in await cur.fetchall()]
    return result


@serialized
async def get_invite_link(invite_link: str) -> dict | None:
    db = await connection()
    async with db.execute("SELECT * FROM invite_links WHERE invite_link = ? ORDER BY id DESC LIMIT 1", (invite_link,)) as cur:
        return row_to_dict(await cur.fetchone())


@serialized
async def list_open_invites(tg_user_id: int) -> list[dict]:
    db = await connection()
    async with db.execute("SELECT * FROM invite_links WHERE tg_user_id = ? AND used = 0 AND (expire_at IS NULL OR expire_at > ?)", (tg_user_id, iso_now())) as cur:
        return [dict(row) for row in await cur.fetchall()]


@serialized
async def mark_invite_used(invite_link: str) -> None:
    db = await connection()
    await db.execute("UPDATE invite_links SET used = 1 WHERE invite_link = ?", (invite_link,))
    await db.commit()


@serialized
async def list_tariffs(include_disabled=False) -> list[dict]:
    con = await connection()
    query = "SELECT * FROM tariffs" + ("" if include_disabled else " WHERE enabled=1") + " ORDER BY days,code"
    async with con.execute(query) as cur:
        return [dict(row) for row in await cur.fetchall()]


@serialized
async def save_tariff(code, title, amount_rub, days, enabled, admin_id):
    import re
    if (not re.fullmatch(r"[a-z0-9_]{1,32}",code) or not 1 <= len(title.strip()) <= 70
            or type(amount_rub) is not int or not 1 <= amount_rub <= 1000000
            or type(days) is not int or not 1 <= days <= 3650):
        raise ValueError("Название: 1–70 знаков; цена: 1–1000000 ₽; срок: 1–3650 дней.")
    con = await connection()
    await con.execute("""INSERT INTO tariffs VALUES(?,?,?,?,?,?) ON CONFLICT(code) DO UPDATE SET
        title=excluded.title,amount_rub=excluded.amount_rub,days=excluded.days,
        enabled=excluded.enabled,updated_at=excluded.updated_at""",
        (code,title.strip(),amount_rub,days,int(bool(enabled)),iso_now()))
    await con.execute("INSERT INTO admin_audit(admin_id,action,target,created_at) VALUES(?,?,?,?)",
                      (admin_id,"tariff",code,iso_now()))
    await con.commit()


@serialized
async def set_admin_setting(key, value, admin_id):
    con = await connection()
    await con.execute("INSERT INTO settings VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
                      (key,str(value),iso_now()))
    await con.execute("INSERT INTO admin_audit(admin_id,action,target,created_at) VALUES(?,?,?,?)",
                      (admin_id,"setting",key,iso_now()))
    await con.commit()


@serialized
async def pending_checkout(tg_user_id):
    con = await connection()
    async with con.execute("SELECT * FROM checkout_requests WHERE tg_user_id=? AND status IN ('creating','created') ORDER BY (status='creating') DESC,created_at LIMIT 1",(tg_user_id,)) as cur:
        return row_to_dict(await cur.fetchone())


def checkout_route(payload):
    if payload.get('payment_method_id'):
        return 'saved'
    return 'sbp' if (payload.get('payment_method_data') or {}).get('type')=='sbp' else 'all'


@serialized
async def prepare_checkout(tg_user_id, tariff, payload, *, auto_opt_in=False, cycle_until=None, direct_offer=None):
    """Persist the exact request before sending money-related network traffic."""
    con = await connection()
    await con.execute("BEGIN IMMEDIATE")
    try:
        existing = await pending_checkout(tg_user_id)
        if existing:
            await con.commit()
            return existing
        if cycle_until:
            async with con.execute("SELECT * FROM checkout_requests WHERE tg_user_id=? AND cycle_until=?",(tg_user_id,cycle_until)) as cur:
                existing = row_to_dict(await cur.fetchone())
            if existing:
                await con.commit()
                return existing
        order_id, now = uuid.uuid4().hex, iso_now()
        consent_key = uuid.uuid4().hex if auto_opt_in else None
        if auto_opt_in:
            await con.execute("""INSERT INTO renewal_contracts(tg_user_id,plan_id,amount_rub,days,consent_key,consented_at)
                VALUES(?,?,?,?,?,?) ON CONFLICT(tg_user_id) DO UPDATE SET enabled=0,payment_method_id=NULL,
                plan_id=excluded.plan_id,amount_rub=excluded.amount_rub,days=excluded.days,
                consent_key=excluded.consent_key,consented_at=excluded.consented_at,disabled_at=NULL,disabled_reason=NULL,
                warned_until=NULL,warned_at=NULL""",
                (tg_user_id,tariff.code,tariff.amount_rub,tariff.days,consent_key,now))
        elif not cycle_until and not direct_offer:
            # A deliberate manual checkout cancels future scheduled attempts.
            await con.execute("UPDATE renewal_contracts SET enabled=0,disabled_at=?,disabled_reason='manual_checkout' WHERE tg_user_id=?",(now,tg_user_id))
        await con.execute("""INSERT INTO checkout_requests(id,tg_user_id,plan_id,title,amount_rub,days,
            request_json,created_at,auto_opt_in,cycle_until,consent_key,offer_url,offer_version,checkout_group_id,payment_route) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (order_id,tg_user_id,tariff.code,tariff.title,tariff.amount_rub,tariff.days,
             json.dumps(payload,ensure_ascii=False),now,int(auto_opt_in),cycle_until,consent_key,
             direct_offer[0] if direct_offer else None,direct_offer[1] if direct_offer else None,order_id,checkout_route(payload)))
        await con.commit()
        return await get_checkout(order_id)
    except BaseException:
        await con.rollback()
        raise


@serialized
async def get_checkout(order_id):
    con = await connection()
    async with con.execute("SELECT * FROM checkout_requests WHERE id=?",(order_id,)) as cur:
        return row_to_dict(await cur.fetchone())


@serialized
async def get_checkout_by_payment(payment_id):
    con=await connection()
    async with con.execute("SELECT * FROM checkout_requests WHERE payment_id=?",(payment_id,)) as cur:
        return row_to_dict(await cur.fetchone())


@serialized
async def checkout_choices(payment_id):
    con = await connection()
    source = await get_checkout_by_payment(payment_id)
    if not source:
        return []
    group = source['checkout_group_id'] or source['id']
    async with con.execute("SELECT * FROM checkout_requests WHERE COALESCE(checkout_group_id,id)=? AND status IN ('creating','created','paid') ORDER BY created_at",(group,)) as cur:
        return [dict(row) for row in await cur.fetchall()]


@serialized
async def prepare_alternative_checkout(payment_id):
    """Prepare only the other manual route, using the original frozen quote."""
    con = await connection()
    await con.execute("BEGIN IMMEDIATE")
    try:
        source = await get_checkout_by_payment(payment_id)
        payment = await get_payment(payment_id)
        if (not source or source['status']!='created' or not payment or payment['status']!='pending'
                or payment['finalized_at'] or source['auto_opt_in'] or source['cycle_until']):
            await con.commit()
            return None
        payload = json.loads(source['request_json'])
        if payload.get('payment_method_id') or payload.get('payment_token') or payload.get('save_payment_method') or (payload.get('confirmation') or {}).get('type')!='redirect':
            await con.commit()
            return None
        route = 'all' if checkout_route(payload)=='sbp' else 'sbp'
        group = source['checkout_group_id'] or source['id']
        quiz=await get_lesson_quiz(source['tg_user_id'])
        discounted=bool(quiz and quiz['token']==(payload.get('metadata') or {}).get('quiz_token')
                        and quiz['checkout_group_id']==group)
        replacement=discounted or (payload.get('metadata') or {}).get('purpose')=='subscription_renewal' or await has_successful_payment(source['tg_user_id'])
        # An unrelated or legacy invoice must still settle before another quote.
        async with con.execute("""SELECT 1 FROM payments p LEFT JOIN checkout_requests c ON c.payment_id=p.payment_id
            WHERE p.tg_user_id=? AND p.finalized_at IS NULL AND p.status!='canceled'
            AND (c.id IS NULL OR COALESCE(c.checkout_group_id,c.id)!=?)
            AND NOT (? AND COALESCE(c.status,'')='superseded') LIMIT 1""",(source['tg_user_id'],group,int(replacement))) as cur:
            blocked = await cur.fetchone()
        async with con.execute("SELECT 1 FROM checkout_requests WHERE tg_user_id=? AND status IN ('creating','created') AND COALESCE(checkout_group_id,id)!=? LIMIT 1",(source['tg_user_id'],group)) as cur:
            blocked = blocked or await cur.fetchone()
        if blocked:
            await con.commit()
            return None
        choices = await checkout_choices(payment_id)
        if any(row['status']=='paid' for row in choices):
            await con.commit()
            return None
        existing = next((row for row in choices if row['payment_route']==route),None)
        if existing:
            await con.commit()
            return existing
        if any(row['status']=='creating' for row in choices):
            await con.commit()
            return None
        if route=='sbp':
            payload['payment_method_data']={'type':'sbp'}
        else:
            payload.pop('payment_method_data',None)
        order_id = uuid.uuid4().hex
        await con.execute("""INSERT INTO checkout_requests(id,tg_user_id,plan_id,title,amount_rub,days,
            request_json,created_at,offer_url,offer_version,checkout_group_id,payment_route) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (order_id,source['tg_user_id'],source['plan_id'],source['title'],source['amount_rub'],source['days'],
             json.dumps(payload,ensure_ascii=False),iso_now(),source['offer_url'],source['offer_version'],group,route))
        await con.commit()
        return await get_checkout(order_id)
    except BaseException:
        await con.rollback()
        raise


@serialized
async def mark_offer_presented(payment_id):
    con=await connection()
    await con.execute("UPDATE checkout_requests SET offer_presented_at=COALESCE(offer_presented_at,?) WHERE payment_id=? AND offer_url IS NOT NULL",
                      (iso_now(),payment_id))
    await con.commit()


@serialized
async def attach_checkout(order, payment):
    con = await connection()
    await con.execute("BEGIN IMMEDIATE")
    try:
        payment_id = payment['id']
        async with con.execute("SELECT * FROM payments WHERE payment_id=?",(payment_id,)) as cur:
            old = await cur.fetchone()
        old_order = await get_checkout_by_payment(payment_id)
        if old_order and old_order['id'] != order['id']:
            raise ValueError("Payment already belongs to another checkout")
        if old and (old['tg_user_id'] != order['tg_user_id'] or old['plan_id'] != order['plan_id'] or old['amount_rub'] != order['amount_rub']):
            raise ValueError("Payment already belongs to another order")
        payload = json.loads(order['request_json'])
        await con.execute("""INSERT OR IGNORE INTO payments(tg_user_id,payment_id,plan_id,amount_rub,status,
            confirmation_url,metadata_json,raw_json,created_at) VALUES(?,?,?,?,?,?,?,?,?)""",
            (order['tg_user_id'],payment_id,order['plan_id'],order['amount_rub'],payment['status'],
             (payment.get('confirmation') or {}).get('confirmation_url'),json.dumps(payload['metadata']),
             json.dumps(payment,ensure_ascii=False),order['created_at']))
        await con.execute("UPDATE checkout_requests SET payment_id=?,status=?,checked_at=? WHERE id=?",
                          (payment_id,'canceled' if payment['status']=='canceled' else 'created',iso_now(),order['id']))
        await _insert_funnel_event(order['tg_user_id'],'payment_created',{'payment_id':payment_id,'amount_rub':order['amount_rub']},event_key='created:'+payment_id)
        await con.commit()
    except BaseException:
        await con.rollback()
        raise


@serialized
async def checkout_failed(order_id, reason):
    con = await connection()
    await con.execute("UPDATE checkout_requests SET status='failed',failure_reason=?,checked_at=? WHERE id=? AND payment_id IS NULL",(reason,iso_now(),order_id))
    await con.commit()


@serialized
async def touch_checkout(order_id):
    con = await connection()
    await con.execute("UPDATE checkout_requests SET checked_at=? WHERE id=?",(iso_now(),order_id))
    await con.commit()


@serialized
async def reconciliation_candidates(limit=30):
    con = await connection()
    async with con.execute("SELECT * FROM payments WHERE finalized_at IS NULL AND status!='canceled' ORDER BY COALESCE(checked_at,''),id LIMIT ?",(limit,)) as cur:
        payments = [dict(r) for r in await cur.fetchall()]
    async with con.execute("SELECT * FROM checkout_requests WHERE status='creating' ORDER BY COALESCE(checked_at,''),created_at LIMIT ?",(limit,)) as cur:
        orders = [dict(r) for r in await cur.fetchall()]
    return payments, orders


@serialized
async def touch_payment(payment_id):
    con = await connection()
    await con.execute("UPDATE payments SET checked_at=? WHERE payment_id=?",(iso_now(),payment_id))
    await con.commit()


@serialized
async def access_notifications():
    con = await connection()
    async with con.execute("""SELECT * FROM payments WHERE finalized_at IS NOT NULL AND access_notified_at IS NULL
        AND (delivery_attempt_at IS NULL OR julianday(delivery_attempt_at)<julianday(?))
        ORDER BY COALESCE(delivery_attempt_at,''),finalized_at LIMIT 30""",
        ((utcnow()-timedelta(minutes=5)).isoformat(),)) as cur:
        return [dict(r) for r in await cur.fetchall()]


@serialized
async def touch_access_delivery(payment_id):
    con = await connection()
    await con.execute("UPDATE payments SET delivery_attempt_at=? WHERE payment_id=?",(iso_now(),payment_id))
    await con.commit()


@serialized
async def mark_access_notified(payment_id):
    con = await connection()
    await con.execute("UPDATE payments SET access_notified_at=? WHERE payment_id=?",(iso_now(),payment_id))
    await con.commit()


async def _activate_saved_method(con, payment):
    async with con.execute("SELECT * FROM checkout_requests WHERE payment_id=?",(payment['payment_id'],)) as cur:
        order = await cur.fetchone()
    if not order or not order['auto_opt_in']:
        return
    method = (json.loads(payment['raw_json']).get('payment_method') or {})
    saved = method.get('saved') is True and isinstance(method.get('id'),str) and 1 <= len(method['id']) <= 128
    changed = await con.execute("""UPDATE renewal_contracts SET enabled=?,payment_method_id=?,disabled_reason=?
        WHERE tg_user_id=? AND consent_key=? AND disabled_at IS NULL""",
        (int(bool(saved)),method['id'] if saved else None,None if saved else 'method_not_saved',payment['tg_user_id'],order['consent_key']))
    if saved and changed.rowcount:
        await _insert_funnel_event(payment['tg_user_id'],'auto_enabled',event_key='auto:'+payment['payment_id'])


@serialized
async def get_renewal(tg_user_id):
    con = await connection()
    async with con.execute("SELECT * FROM renewal_contracts WHERE tg_user_id=?",(tg_user_id,)) as cur:
        return row_to_dict(await cur.fetchone())


@serialized
async def disable_renewal(tg_user_id, reason='user'):
    con = await connection()
    await con.execute("UPDATE renewal_contracts SET enabled=0,disabled_at=?,disabled_reason=? WHERE tg_user_id=?",(iso_now(),reason,tg_user_id))
    await _insert_funnel_event(tg_user_id,'auto_disabled',{'reason':reason})
    await con.commit()


@serialized
async def mark_auto_warning(tg_user_id,access_until):
    con=await connection()
    await con.execute("UPDATE renewal_contracts SET warned_until=?,warned_at=? WHERE tg_user_id=? AND enabled=1",(access_until,iso_now(),tg_user_id))
    await con.commit()


@serialized
async def due_renewals():
    con = await connection()
    async with con.execute("""SELECT r.*,s.access_until,s.renewal_notice_sent_at FROM renewal_contracts r
        JOIN subscriptions s USING(tg_user_id) WHERE r.enabled=1 AND s.status='active'
        AND julianday(s.access_until)<=julianday(?) ORDER BY s.access_until""",
        ((utcnow()+timedelta(days=1)).isoformat(),)) as cur:
        return [dict(r) for r in await cur.fetchall()]


@serialized
async def notification_claimed(key):
    con = await connection()
    async with con.execute('SELECT 1 FROM notification_attempts WHERE key=?',(key,)) as cur:
        return bool(await cur.fetchone())


@serialized
async def claim_notification(key, tg_user_id):
    con = await connection()
    cur = await con.execute("INSERT OR IGNORE INTO notification_attempts(key,tg_user_id,claimed_at) VALUES(?,?,?)",(key,tg_user_id,iso_now()))
    await con.commit()
    return cur.rowcount == 1


@serialized
async def mark_notification_sent(key):
    con = await connection()
    await con.execute("UPDATE notification_attempts SET sent_at=? WHERE key=?",(iso_now(),key))
    await con.commit()


@serialized
async def record_activity(chat_id,message_id,tg_user_id,sent_at,has_file):
    con = await connection()
    cur = await con.execute("INSERT OR IGNORE INTO activity_events VALUES(?,?,?,?,?)",(chat_id,message_id,tg_user_id,sent_at,int(has_file)))
    await con.commit()
    return cur.rowcount == 1


@serialized
async def activity_summary(tg_user_id=None, days=30):
    con = await connection()
    where = "sent_at>=?"
    params = [(utcnow()-timedelta(days=days)).isoformat()]
    if tg_user_id is not None:
        where += " AND tg_user_id=?"
        params.append(tg_user_id)
    async with con.execute("SELECT COUNT(*) messages,COALESCE(SUM(has_file),0) files,COUNT(DISTINCT tg_user_id) people,MAX(sent_at) last_at FROM activity_events WHERE "+where,params) as cur:
        return dict(await cur.fetchone())


@serialized
async def set_goal(tg_user_id, goal):
    if goal not in {'work','content','everyday'}:
        raise ValueError('Unknown goal')
    con = await connection()
    await con.execute("UPDATE users SET goal=? WHERE tg_user_id=?",(goal,tg_user_id))
    await _insert_funnel_event(tg_user_id,'goal_selected',{'goal':goal})
    await con.commit()


@serialized
async def set_marketing_opt_out(tg_user_id, enabled):
    con = await connection()
    await con.execute("UPDATE users SET marketing_opt_out=? WHERE tg_user_id=?",(int(enabled),tg_user_id))
    await con.commit()


@serialized
async def funnel_breakdown(days=30):
    stages = ['start','lead_magnet_sent','product_opened','tariffs_opened','buy_clicked','offer_accepted','payment_created','payment_succeeded','channel_joined']
    con = await connection()
    since = (utcnow()-timedelta(days=days)).isoformat() if days else '0001-01-01'
    async with con.execute("""WITH cohort AS (SELECT tg_user_id,MIN(created_at) entry FROM funnel_events
        WHERE event_name='start' GROUP BY tg_user_id HAVING MIN(created_at)>=?)
        SELECT e.tg_user_id,e.event_name FROM funnel_events e JOIN cohort c USING(tg_user_id)
        WHERE e.created_at>=c.entry GROUP BY e.tg_user_id,e.event_name""",(since,)) as cur:
        rows = await cur.fetchall()
    users = {}
    for row in rows:
        users.setdefault(row['tg_user_id'],set()).add(row['event_name'])
    reached = {s:sum(s in events for events in users.values()) for s in stages}
    stopped = {s:0 for s in stages}
    for events in users.values():
        furthest = next((s for s in reversed(stages) if s in events),'start')
        stopped[furthest] += 1
    async with con.execute("SELECT COUNT(*) FROM checkout_requests WHERE status='creating' AND created_at<?",((utcnow()-timedelta(hours=23)).isoformat(),)) as cur:
        unknown = (await cur.fetchone())[0]
    return {'cohort':len(users),'reached':reached,'stopped':stopped,'unknown_payments':unknown}
