from __future__ import annotations

import asyncio
import json
from functools import wraps
import os
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
    global _conn
    if _conn is None:
        await init_db()
    assert _conn is not None
    return _conn


@serialized
async def init_db() -> aiosqlite.Connection:
    global _conn
    if _conn is not None:
        return _conn
    _conn = await aiosqlite.connect(DB_PATH)
    _conn.row_factory = aiosqlite.Row
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
        SET status = ?, raw_json = ?, paid_at = COALESCE(paid_at, ?)
        WHERE payment_id = ?
        """,
        (status, json.dumps(raw, ensure_ascii=False), paid_at, payment_id),
    )
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
        await _insert_funnel_event(tg_user_id, "payment_succeeded",
                                   {"payment_id": payment_id, "amount_rub": payment_dict["amount_rub"]},
                                   event_key=f"paid:{payment_id}")
        await _insert_funnel_event(tg_user_id, "access_granted",
                                   {"payment_id": payment_id, "access_until": access_until},
                                   event_key=f"access:{payment_id}")
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
    if days <= 0:
        raise ValueError("days must be positive")
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
    await db.commit()


@serialized
async def list_expired_subscriptions(now: str | datetime | None = None) -> list[dict[str, Any]]:
    db = await connection()
    now_iso = now.isoformat() if isinstance(now, datetime) else (now or iso_now())
    async with db.execute(
        "SELECT * FROM subscriptions WHERE status = 'active' AND access_until <= ?",
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
          AND access_until > ?
          AND access_until <= ?
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
    await db.execute("UPDATE subscriptions SET status = 'expired', expiration_notice_sent_at = ?, updated_at = ? WHERE tg_user_id = ?", (now, now, tg_user_id))
    await db.commit()


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
        "INSERT INTO invite_links(tg_user_id, invite_link, payment_id, expire_at, created_at) VALUES(?, ?, ?, ?, ?)",
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
        "active_subscriptions_count": "SELECT COUNT(*) AS value FROM subscriptions WHERE status = 'active' AND access_until > ?",
    }
    result: dict[str, Any] = {}
    for key, sql in queries.items():
        params = (iso_now(),) if "access_until" in sql else ()
        async with db.execute(sql, params) as cur:
            result[key] = (await cur.fetchone())["value"]
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
    "renewal_started", "expired",
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
      AND NOT EXISTS (SELECT 1 FROM followup_deliveries f WHERE f.tg_user_id = e.tg_user_id)
      AND NOT EXISTS (SELECT 1 FROM payments p WHERE p.tg_user_id = e.tg_user_id
                      AND (p.finalized_at IS NOT NULL OR p.status = 'succeeded'))
      AND NOT EXISTS (SELECT 1 FROM subscriptions s WHERE s.tg_user_id = e.tg_user_id
                      AND s.status = 'active' AND s.access_until > ?)
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
