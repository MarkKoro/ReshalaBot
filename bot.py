#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Telegram-бот платного доступа к закрытому каналу.

Старый сценарий DeepSeek/GPT/photo/credits убран из пользовательского UI.
Бот работает в режиме одного актуального сообщения: при переходах он редактирует
текущее сообщение, а если тип экрана меняется между текстом и медиа — удаляет
старое и отправляет новое с inline-кнопками только под актуальным сообщением.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

import aiosqlite
import httpx
from dotenv import load_dotenv
from telegram import InlineKeyboardButton as Btn
from telegram import InlineKeyboardMarkup as Mk
from telegram import InputMediaPhoto, InputMediaVideo, Update
from telegram.error import BadRequest, Forbidden, TelegramError
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

# ── Конфигурация ─────────────────────────────────────
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_IDS = {
    int(admin_id.strip())
    for admin_id in os.getenv("ADMIN_IDS", "").split(",")
    if admin_id.strip()
}
YOOKASSA_SHOP_ID = os.getenv("YOOKASSA_SHOP_ID", "")
YOOKASSA_SECRET_KEY = os.getenv("YOOKASSA_SECRET_KEY", "")
PROJECT_KEY = os.getenv("PROJECT_KEY", "reshalo_channel_access")
CHANNEL_ID = int(os.getenv("CHANNEL_ID", "-1002468345259"))
ENV_OFFER_URL = os.getenv("OFFER_URL", "").strip()
YOOKASSA_RETURN_URL = os.getenv("YOOKASSA_RETURN_URL", "").strip()
BOT_USERNAME = os.getenv("BOT_USERNAME", "").lstrip("@").strip()
DB_PATH = Path(os.getenv("DB_PATH", "credits.db"))
YOOKASSA_API_URL = "https://api.yookassa.ru/v3/payments"

ScreenKind = Literal["text", "video", "photo"]


@dataclass(frozen=True)
class Tariff:
    code: str
    title: str
    amount_rub: Decimal
    days: int
    first_purchase_only: bool = False


TARIFFS: dict[str, Tariff] = {
    "m1_first": Tariff("m1_first", "1 месяц — первая покупка", Decimal("1000.00"), 30, True),
    "m1": Tariff("m1", "1 месяц", Decimal("1500.00"), 30),
    "m3": Tariff("m3", "3 месяца", Decimal("4000.00"), 90),
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("reshalo_channel_bot")

# ── SQLite ───────────────────────────────────────────

async def init_db(app: Application) -> None:
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    await reset_legacy_tables(db)
    await db.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            offer_accepted_at INTEGER,
            has_successful_payment INTEGER NOT NULL DEFAULT 0,
            ui_message_id INTEGER NOT NULL DEFAULT 0,
            ui_kind TEXT NOT NULL DEFAULT '',
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS review_photos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_id TEXT NOT NULL,
            created_at INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS subscriptions (
            user_id INTEGER PRIMARY KEY,
            access_until INTEGER NOT NULL DEFAULT 0,
            removed_after_expire INTEGER NOT NULL DEFAULT 0,
            updated_at INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS payments (
            payment_id TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            tariff_code TEXT NOT NULL,
            amount_rub TEXT NOT NULL,
            days INTEGER NOT NULL,
            status TEXT NOT NULL,
            confirmation_url TEXT NOT NULL DEFAULT '',
            paid INTEGER NOT NULL DEFAULT 0,
            processed INTEGER NOT NULL DEFAULT 0,
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL
        );
        """
    )
    await ensure_column(db, "users", "ui_message_id", "INTEGER NOT NULL DEFAULT 0")
    await ensure_column(db, "users", "ui_kind", "TEXT NOT NULL DEFAULT ''")
    await ensure_column(db, "payments", "confirmation_url", "TEXT NOT NULL DEFAULT ''")
    if ENV_OFFER_URL:
        await set_setting(db, "offer_url", ENV_OFFER_URL)
    await db.commit()
    app.bot_data["db"] = db
    logger.info("database initialized at %s", DB_PATH)


async def reset_legacy_tables(db: aiosqlite.Connection) -> None:
    """Replace legacy credits/payment tables; current old data is intentionally not preserved."""
    for table_name, required_column in (("users", "user_id"), ("payments", "payment_id")):
        async with db.execute(f"PRAGMA table_info({table_name})") as cur:
            columns = {row[1] for row in await cur.fetchall()}
        if columns and required_column not in columns:
            logger.warning("dropping legacy table %s before channel-access migration", table_name)
            await db.execute(f"DROP TABLE IF EXISTS {table_name}")
    await db.commit()


async def ensure_column(db: aiosqlite.Connection, table: str, column: str, definition: str) -> None:
    async with db.execute(f"PRAGMA table_info({table})") as cur:
        columns = {row[1] for row in await cur.fetchall()}
    if column not in columns:
        await db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


async def close_db(app: Application) -> None:
    db = app.bot_data.get("db")
    if db:
        await db.close()


def now_ts() -> int:
    return int(datetime.now(timezone.utc).timestamp())


def format_dt(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%d.%m.%Y %H:%M UTC")


async def ensure_user(db: aiosqlite.Connection, user_id: int) -> None:
    ts = now_ts()
    await db.execute(
        """
        INSERT INTO users(user_id, created_at, updated_at)
        VALUES(?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET updated_at=excluded.updated_at
        """,
        (user_id, ts, ts),
    )
    await db.commit()


async def set_setting(db: aiosqlite.Connection, key: str, value: str) -> None:
    await db.execute(
        "INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


async def get_setting(db: aiosqlite.Connection, key: str, default: str = "") -> str:
    async with db.execute("SELECT value FROM settings WHERE key = ?", (key,)) as cur:
        row = await cur.fetchone()
    return str(row["value"]) if row else default


async def accepted_offer(db: aiosqlite.Connection, user_id: int) -> bool:
    async with db.execute("SELECT offer_accepted_at FROM users WHERE user_id = ?", (user_id,)) as cur:
        row = await cur.fetchone()
    return bool(row and row["offer_accepted_at"])


async def has_successful_payment(db: aiosqlite.Connection, user_id: int) -> bool:
    async with db.execute("SELECT has_successful_payment FROM users WHERE user_id = ?", (user_id,)) as cur:
        row = await cur.fetchone()
    return bool(row and row["has_successful_payment"])


async def available_tariffs(db: aiosqlite.Connection, user_id: int) -> list[Tariff]:
    had_payment = await has_successful_payment(db, user_id)
    return [tariff for tariff in TARIFFS.values() if not tariff.first_purchase_only or not had_payment]


async def get_access_until(db: aiosqlite.Connection, user_id: int) -> int:
    async with db.execute("SELECT access_until FROM subscriptions WHERE user_id = ?", (user_id,)) as cur:
        row = await cur.fetchone()
    return int(row["access_until"]) if row else 0

# ── Один актуальный экран ────────────────────────────

async def get_ui_state(db: aiosqlite.Connection, user_id: int) -> tuple[int, str]:
    async with db.execute("SELECT ui_message_id, ui_kind FROM users WHERE user_id = ?", (user_id,)) as cur:
        row = await cur.fetchone()
    if not row:
        return 0, ""
    return int(row["ui_message_id"] or 0), str(row["ui_kind"] or "")


async def save_ui_state(db: aiosqlite.Connection, user_id: int, message_id: int, kind: str) -> None:
    await db.execute(
        "UPDATE users SET ui_message_id = ?, ui_kind = ?, updated_at = ? WHERE user_id = ?",
        (message_id, kind, now_ts(), user_id),
    )
    await db.commit()


async def forget_ui_state(db: aiosqlite.Connection, user_id: int) -> None:
    await save_ui_state(db, user_id, 0, "")


async def safe_delete_message(ctx: ContextTypes.DEFAULT_TYPE, chat_id: int, message_id: int) -> None:
    try:
        await ctx.bot.delete_message(chat_id=chat_id, message_id=message_id)
    except TelegramError:
        logger.debug("message delete skipped chat_id=%s message_id=%s", chat_id, message_id, exc_info=True)


async def delete_user_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat and update.effective_message:
        await safe_delete_message(ctx, update.effective_chat.id, update.effective_message.message_id)


async def render_screen(
    chat_id: int,
    user_id: int,
    ctx: ContextTypes.DEFAULT_TYPE,
    *,
    text: str,
    reply_markup: Mk | None = None,
    kind: ScreenKind = "text",
    file_id: str | None = None,
) -> None:
    """Show exactly one bot message for this user, moving inline buttons to it."""
    db = ctx.bot_data["db"]
    await ensure_user(db, user_id)
    old_message_id, old_kind = await get_ui_state(db, user_id)

    if old_message_id and old_kind == kind:
        try:
            if kind == "text":
                await ctx.bot.edit_message_text(chat_id=chat_id, message_id=old_message_id, text=text, reply_markup=reply_markup)
            elif kind == "video":
                await ctx.bot.edit_message_media(
                    chat_id=chat_id,
                    message_id=old_message_id,
                    media=InputMediaVideo(media=file_id, caption=text),
                    reply_markup=reply_markup,
                )
            else:
                await ctx.bot.edit_message_media(
                    chat_id=chat_id,
                    message_id=old_message_id,
                    media=InputMediaPhoto(media=file_id, caption=text),
                    reply_markup=reply_markup,
                )
            return
        except BadRequest as exc:
            if "message is not modified" in str(exc).lower():
                return
            logger.info("screen edit failed, recreating message: %s", exc)
        except TelegramError:
            logger.exception("screen edit failed, recreating message")

    if old_message_id:
        await safe_delete_message(ctx, chat_id, old_message_id)
        await forget_ui_state(db, user_id)

    if kind == "video":
        msg = await ctx.bot.send_video(chat_id=chat_id, video=file_id, caption=text, reply_markup=reply_markup)
    elif kind == "photo":
        msg = await ctx.bot.send_photo(chat_id=chat_id, photo=file_id, caption=text, reply_markup=reply_markup)
    else:
        msg = await ctx.bot.send_message(chat_id=chat_id, text=text, reply_markup=reply_markup)
    await save_ui_state(db, user_id, msg.message_id, kind)

# ── Пользовательские клавиатуры и экраны ─────────────

def offer_keyboard(offer_url: str) -> Mk:
    rows: list[list[Btn]] = []
    if offer_url:
        rows.append([Btn("📄 Оферта", url=offer_url)])
    else:
        rows.append([Btn("📄 Оферта не задана", callback_data="noop_offer")])
    rows.append([Btn("✅ Принимаю оферту", callback_data="user:accept_offer")])
    return Mk(rows)


def next_keyboard(callback_data: str, label: str = "Далее ➡️") -> Mk:
    return Mk([[Btn(label, callback_data=callback_data)]])


def tariffs_keyboard(tariffs: list[Tariff]) -> Mk:
    rows = [[Btn(f"{tariff.title} — {int(tariff.amount_rub)} ₽", callback_data=f"user:tariff:{tariff.code}")] for tariff in tariffs]
    rows.append([Btn("🔄 Обновить", callback_data="user:tariffs")])
    return Mk(rows)


def payment_keyboard(payment_id: str, confirmation_url: str) -> Mk:
    return Mk(
        [
            [Btn("💳 Оплатить", url=confirmation_url)],
            [Btn("🔁 Проверить оплату", callback_data=f"user:check_payment:{payment_id}")],
            [Btn("◀️ К тарифам", callback_data="user:tariffs")],
        ]
    )


async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    db = ctx.bot_data["db"]
    await ensure_user(db, user_id)
    await delete_user_message(update, ctx)

    if await accepted_offer(db, user_id):
        await show_intro_or_reviews_or_tariffs(chat_id, user_id, ctx)
        return

    offer_url = await get_setting(db, "offer_url", ENV_OFFER_URL)
    await render_screen(
        chat_id,
        user_id,
        ctx,
        text=(
            "Привет! Это бот доступа к закрытому Telegram-каналу.\n\n"
            "1. Откройте оферту.\n"
            "2. Нажмите «✅ Принимаю оферту».\n"
            "3. После этого откроются материалы и тарифы.\n\n"
            "До принятия оферты оплатить доступ нельзя."
        ),
        reply_markup=offer_keyboard(offer_url),
    )


async def show_intro_or_reviews_or_tariffs(chat_id: int, user_id: int, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    intro_video_file_id = await get_setting(ctx.bot_data["db"], "intro_video_file_id")
    if intro_video_file_id:
        await render_screen(
            chat_id,
            user_id,
            ctx,
            kind="video",
            file_id=intro_video_file_id,
            text="Короткое превью закрытого канала",
            reply_markup=next_keyboard("user:after_intro"),
        )
        return
    await show_first_review_or_tariffs(chat_id, user_id, ctx)


async def show_first_review_or_tariffs(chat_id: int, user_id: int, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    db = ctx.bot_data["db"]
    async with db.execute("SELECT id FROM review_photos ORDER BY id LIMIT 1") as cur:
        row = await cur.fetchone()
    if row:
        await show_review(chat_id, user_id, int(row["id"]), ctx)
        return
    await show_tariffs(chat_id, user_id, ctx)


async def show_review(chat_id: int, user_id: int, review_id: int, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    db = ctx.bot_data["db"]
    async with db.execute("SELECT id, file_id FROM review_photos WHERE id >= ? ORDER BY id LIMIT 1", (review_id,)) as cur:
        row = await cur.fetchone()
    if not row:
        await show_tariffs(chat_id, user_id, ctx)
        return
    async with db.execute("SELECT id FROM review_photos WHERE id > ? ORDER BY id LIMIT 1", (int(row["id"]),)) as cur:
        next_row = await cur.fetchone()
    keyboard = next_keyboard(f"user:review:{int(next_row['id'])}" if next_row else "user:tariffs")
    await render_screen(chat_id, user_id, ctx, kind="photo", file_id=row["file_id"], text="Отзыв о закрытом канале", reply_markup=keyboard)


async def show_tariffs(chat_id: int, user_id: int, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    db = ctx.bot_data["db"]
    tariffs = await available_tariffs(db, user_id)
    access_until = await get_access_until(db, user_id)
    active_line = f"\n\nВаш текущий доступ активен до {format_dt(access_until)}." if access_until > now_ts() else ""
    await render_screen(
        chat_id,
        user_id,
        ctx,
        text="Выберите срок доступа к закрытому каналу:" + active_line,
        reply_markup=tariffs_keyboard(tariffs),
    )


async def user_router(query, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = query.from_user.id
    chat_id = query.message.chat_id
    data = query.data or ""
    db = ctx.bot_data["db"]
    await ensure_user(db, user_id)

    if data == "user:accept_offer":
        await db.execute(
            "UPDATE users SET offer_accepted_at = COALESCE(offer_accepted_at, ?), updated_at = ? WHERE user_id = ?",
            (now_ts(), now_ts(), user_id),
        )
        await db.commit()
        await show_intro_or_reviews_or_tariffs(chat_id, user_id, ctx)
        return

    if data == "user:after_intro":
        await show_first_review_or_tariffs(chat_id, user_id, ctx)
        return

    if data.startswith("user:review:"):
        await show_review(chat_id, user_id, int(data.rsplit(":", 1)[1]), ctx)
        return

    if data == "user:tariffs":
        if not await accepted_offer(db, user_id):
            await start_offer_again(chat_id, user_id, ctx)
            return
        await show_tariffs(chat_id, user_id, ctx)
        return

    if data.startswith("user:tariff:"):
        if not await accepted_offer(db, user_id):
            await start_offer_again(chat_id, user_id, ctx)
            return
        tariff_code = data.rsplit(":", 1)[1]
        tariff = TARIFFS.get(tariff_code)
        if not tariff:
            await render_screen(chat_id, user_id, ctx, text="Тариф не найден. Выберите тариф заново.", reply_markup=next_keyboard("user:tariffs", "К тарифам"))
            return
        if tariff.first_purchase_only and await has_successful_payment(db, user_id):
            await render_screen(chat_id, user_id, ctx, text="Тариф первой покупки уже недоступен.", reply_markup=next_keyboard("user:tariffs", "К тарифам"))
            return
        await create_payment_and_render(chat_id, user_id, tariff, ctx)
        return

    if data.startswith("user:check_payment:"):
        payment_id = data.split(":", 2)[2]
        await check_payment_and_grant(chat_id, user_id, payment_id, ctx)
        return


async def start_offer_again(chat_id: int, user_id: int, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    offer_url = await get_setting(ctx.bot_data["db"], "offer_url", ENV_OFFER_URL)
    await render_screen(chat_id, user_id, ctx, text="Сначала нужно принять оферту, затем откроется оплата.", reply_markup=offer_keyboard(offer_url))

# ── YooKassa и доступ ────────────────────────────────

async def create_payment_and_render(chat_id: int, user_id: int, tariff: Tariff, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    db = ctx.bot_data["db"]
    if not YOOKASSA_SHOP_ID or not YOOKASSA_SECRET_KEY:
        await render_screen(chat_id, user_id, ctx, text="Оплата временно недоступна: YooKassa не настроена.", reply_markup=next_keyboard("user:tariffs", "К тарифам"))
        logger.error("payment creation blocked: empty YooKassa credentials")
        return

    return_url = YOOKASSA_RETURN_URL or (f"https://t.me/{BOT_USERNAME}" if BOT_USERNAME else "https://t.me/")
    payload = {
        "amount": {"value": f"{tariff.amount_rub:.2f}", "currency": "RUB"},
        "capture": True,
        "confirmation": {"type": "redirect", "return_url": return_url},
        "description": f"Доступ к закрытому каналу: {tariff.title}",
        "metadata": {
            "project_key": PROJECT_KEY,
            "user_id": str(user_id),
            "kind": "channel_access",
            "tariff_code": tariff.code,
            "days": str(tariff.days),
        },
    }
    logger.info("creating YooKassa payment user_id=%s tariff=%s amount=%s", user_id, tariff.code, tariff.amount_rub)
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                YOOKASSA_API_URL,
                json=payload,
                auth=(YOOKASSA_SHOP_ID, YOOKASSA_SECRET_KEY),
                headers={"Idempotence-Key": str(uuid.uuid4())},
            )
            response.raise_for_status()
            payment = response.json()
    except httpx.HTTPError:
        logger.exception("failed to create YooKassa payment user_id=%s tariff=%s", user_id, tariff.code)
        await render_screen(chat_id, user_id, ctx, text="Не получилось создать платёж. Попробуйте позже.", reply_markup=next_keyboard("user:tariffs", "К тарифам"))
        return

    payment_id = payment.get("id")
    confirmation_url = payment.get("confirmation", {}).get("confirmation_url")
    if not payment_id or not confirmation_url:
        logger.error("YooKassa returned invalid payment response: %s", payment)
        await render_screen(chat_id, user_id, ctx, text="YooKassa не вернула ссылку на оплату. Попробуйте позже.", reply_markup=next_keyboard("user:tariffs", "К тарифам"))
        return

    ts = now_ts()
    await db.execute(
        """
        INSERT INTO payments(payment_id, user_id, tariff_code, amount_rub, days, status, confirmation_url, paid, created_at, updated_at)
        VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(payment_id) DO NOTHING
        """,
        (payment_id, user_id, tariff.code, f"{tariff.amount_rub:.2f}", tariff.days, payment.get("status", "pending"), confirmation_url, int(bool(payment.get("paid"))), ts, ts),
    )
    await db.commit()
    logger.info("created YooKassa payment payment_id=%s user_id=%s", payment_id, user_id)

    await render_screen(
        chat_id,
        user_id,
        ctx,
        text=(
            f"Счёт создан: {tariff.title} за {int(tariff.amount_rub)} ₽.\n\n"
            "Нажмите «Оплатить», затем вернитесь в бот и нажмите «Проверить оплату»."
        ),
        reply_markup=payment_keyboard(payment_id, confirmation_url),
    )


async def check_payment_and_grant(chat_id: int, user_id: int, payment_id: str, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    db = ctx.bot_data["db"]
    async with db.execute("SELECT * FROM payments WHERE payment_id = ?", (payment_id,)) as cur:
        payment_row = await cur.fetchone()
    if not payment_row or int(payment_row["user_id"]) != user_id:
        await render_screen(chat_id, user_id, ctx, text="Платёж не найден для вашего аккаунта. Создайте новый счёт.", reply_markup=next_keyboard("user:tariffs", "К тарифам"))
        return
    if int(payment_row["processed"]):
        await send_existing_access(chat_id, user_id, ctx)
        return

    logger.info("checking YooKassa payment payment_id=%s user_id=%s", payment_id, user_id)
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.get(f"{YOOKASSA_API_URL}/{payment_id}", auth=(YOOKASSA_SHOP_ID, YOOKASSA_SECRET_KEY))
            response.raise_for_status()
            payment = response.json()
    except httpx.HTTPError:
        logger.exception("failed to check YooKassa payment payment_id=%s user_id=%s", payment_id, user_id)
        confirmation_url = payment_row["confirmation_url"] or "https://t.me/"
        await render_screen(chat_id, user_id, ctx, text="Не получилось проверить платёж. Попробуйте ещё раз через минуту.", reply_markup=payment_keyboard(payment_id, confirmation_url))
        return

    status = str(payment.get("status", ""))
    paid = bool(payment.get("paid"))
    metadata = payment.get("metadata") or {}
    await db.execute("UPDATE payments SET status = ?, paid = ?, updated_at = ? WHERE payment_id = ?", (status, int(paid), now_ts(), payment_id))
    await db.commit()
    logger.info("YooKassa payment state payment_id=%s status=%s paid=%s", payment_id, status, paid)

    if not (status == "succeeded" or paid):
        confirmation_url = payment_row["confirmation_url"] or "https://t.me/"
        await render_screen(chat_id, user_id, ctx, text="Оплата пока не подтверждена. Если вы уже оплатили, попробуйте чуть позже.", reply_markup=payment_keyboard(payment_id, confirmation_url))
        return

    if not valid_metadata(metadata, user_id):
        logger.error("payment metadata mismatch payment_id=%s user_id=%s metadata=%s", payment_id, user_id, metadata)
        await render_screen(chat_id, user_id, ctx, text="Платёж не прошёл внутреннюю проверку проекта. Напишите администратору.")
        return

    await process_successful_payment(chat_id, user_id, payment_id, int(payment_row["days"]), ctx)


def valid_metadata(metadata: dict[str, Any], user_id: int) -> bool:
    return metadata.get("project_key") == PROJECT_KEY and str(metadata.get("user_id")) == str(user_id) and metadata.get("kind") == "channel_access"


async def process_successful_payment(chat_id: int, user_id: int, payment_id: str, days: int, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    db = ctx.bot_data["db"]
    async with db.execute("SELECT processed FROM payments WHERE payment_id = ?", (payment_id,)) as cur:
        row = await cur.fetchone()
    if not row or int(row["processed"]):
        await send_existing_access(chat_id, user_id, ctx)
        return

    current_until = await get_access_until(db, user_id)
    new_until = max(current_until, now_ts()) + days * 24 * 60 * 60
    ts = now_ts()
    await db.execute("BEGIN IMMEDIATE")
    await db.execute("UPDATE payments SET processed = 1, status = 'succeeded', paid = 1, updated_at = ? WHERE payment_id = ? AND processed = 0", (ts, payment_id))
    await db.execute("UPDATE users SET has_successful_payment = 1, updated_at = ? WHERE user_id = ?", (ts, user_id))
    await db.execute(
        """
        INSERT INTO subscriptions(user_id, access_until, removed_after_expire, updated_at)
        VALUES(?, ?, 0, ?)
        ON CONFLICT(user_id) DO UPDATE SET access_until = excluded.access_until,
            removed_after_expire = 0,
            updated_at = excluded.updated_at
        """,
        (user_id, new_until, ts),
    )
    await db.commit()

    invite_link = await create_invite_link(user_id, ctx)
    logger.info("granted access user_id=%s payment_id=%s access_until=%s", user_id, payment_id, new_until)
    await render_screen(
        chat_id,
        user_id,
        ctx,
        text=f"✅ Оплата подтверждена. Доступ активен до {format_dt(new_until)}.\n\nВаша одноразовая ссылка для входа в канал:\n{invite_link}",
        reply_markup=next_keyboard("user:tariffs", "К тарифам"),
    )


async def create_invite_link(user_id: int, ctx: ContextTypes.DEFAULT_TYPE) -> str:
    invite = await ctx.bot.create_chat_invite_link(chat_id=CHANNEL_ID, name=f"access_{user_id}_{now_ts()}", member_limit=1)
    logger.info("created invite link user_id=%s channel_id=%s", user_id, CHANNEL_ID)
    return invite.invite_link


async def send_existing_access(chat_id: int, user_id: int, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    access_until = await get_access_until(ctx.bot_data["db"], user_id)
    if access_until > now_ts():
        invite_link = await create_invite_link(user_id, ctx)
        text = f"Платёж уже был обработан. Доступ активен до {format_dt(access_until)}.\n\nНовая одноразовая ссылка:\n{invite_link}"
    else:
        text = "Этот платёж уже обработан, но активный доступ не найден. Напишите администратору."
    await render_screen(chat_id, user_id, ctx, text=text, reply_markup=next_keyboard("user:tariffs", "К тарифам"))

# ── Удаление истёкших подписок ───────────────────────

async def expire_subscriptions_job(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    db = ctx.bot_data["db"]
    ts = now_ts()
    async with db.execute(
        "SELECT user_id, access_until FROM subscriptions WHERE access_until <= ? AND access_until > 0 AND removed_after_expire = 0",
        (ts,),
    ) as cur:
        rows = await cur.fetchall()
    for row in rows:
        user_id = int(row["user_id"])
        await remove_user_from_channel(ctx, user_id, reason="expired")
        await db.execute("UPDATE subscriptions SET removed_after_expire = 1, updated_at = ? WHERE user_id = ?", (ts, user_id))
        await db.commit()
        logger.info("expired subscription removed user_id=%s access_until=%s", user_id, row["access_until"])


async def remove_user_from_channel(ctx: ContextTypes.DEFAULT_TYPE, user_id: int, reason: str) -> None:
    try:
        await ctx.bot.ban_chat_member(CHANNEL_ID, user_id)
        await ctx.bot.unban_chat_member(CHANNEL_ID, user_id, only_if_banned=True)
        logger.info("removed user from channel user_id=%s channel_id=%s reason=%s", user_id, CHANNEL_ID, reason)
    except (BadRequest, Forbidden, TelegramError):
        logger.exception("failed to remove user from channel user_id=%s reason=%s", user_id, reason)

# ── Админ-панель на inline-кнопках ───────────────────

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def admin_only(func):
    async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not update.effective_user or not is_admin(update.effective_user.id):
            await delete_user_message(update, ctx)
            return
        return await func(update, ctx)
    return wrapper


def admin_menu_keyboard() -> Mk:
    return Mk(
        [
            [Btn("📄 Оферта", callback_data="admin:offer"), Btn("🎬 Видео", callback_data="admin:intro")],
            [Btn("🖼 Отзывы", callback_data="admin:reviews"), Btn("🎁 Выдать доступ", callback_data="admin:grant")],
            [Btn("🚫 Отозвать доступ", callback_data="admin:revoke"), Btn("📊 Статистика", callback_data="admin:stats")],
            [Btn("👤 Пользовательский экран", callback_data="admin:user_start")],
        ]
    )


def admin_intro_keyboard(has_video: bool) -> Mk:
    rows = [[Btn("➕ Загрузить/заменить видео", callback_data="admin:intro_set")]]
    if has_video:
        rows.append([Btn("🗑 Удалить видео", callback_data="admin:intro_clear")])
    rows.append([Btn("◀️ В админ-панель", callback_data="admin:menu")])
    return Mk(rows)


def admin_reviews_keyboard(has_reviews: bool) -> Mk:
    rows = [[Btn("➕ Добавить фото-отзыв", callback_data="admin:review_add")]]
    if has_reviews:
        rows.append([Btn("👀 Показать отзывы", callback_data="admin:review_show:0")])
        rows.append([Btn("🗑 Очистить отзывы", callback_data="admin:reviews_clear")])
    rows.append([Btn("◀️ В админ-панель", callback_data="admin:menu")])
    return Mk(rows)


async def admin(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not is_admin(update.effective_user.id):
        await delete_user_message(update, ctx)
        return
    await delete_user_message(update, ctx)
    await show_admin_menu(update.effective_chat.id, update.effective_user.id, ctx)


async def show_admin_menu(chat_id: int, user_id: int, ctx: ContextTypes.DEFAULT_TYPE, note: str = "") -> None:
    ctx.user_data.pop("admin_waiting_for", None)
    text = "Админ-панель\n\nВыберите действие кнопками ниже. Команды больше не нужны для повседневного управления."
    if note:
        text = f"{note}\n\n{text}"
    await render_screen(chat_id, user_id, ctx, text=text, reply_markup=admin_menu_keyboard())


async def admin_router(query, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = query.from_user.id
    chat_id = query.message.chat_id
    if not is_admin(user_id):
        return
    data = query.data or ""
    db = ctx.bot_data["db"]

    if data == "admin:menu":
        await show_admin_menu(chat_id, user_id, ctx)
        return
    if data == "admin:user_start":
        await start_offer_again(chat_id, user_id, ctx)
        return
    if data == "admin:offer":
        offer_url = await get_setting(db, "offer_url", ENV_OFFER_URL)
        ctx.user_data["admin_waiting_for"] = "offer_url"
        await render_screen(chat_id, user_id, ctx, text=f"Текущая оферта:\n{offer_url or 'не задана'}\n\nОтправьте новой строкой ссылку на оферту.", reply_markup=Mk([[Btn("◀️ Отмена", callback_data="admin:menu")]]))
        return
    if data == "admin:intro":
        has_video = bool(await get_setting(db, "intro_video_file_id"))
        await render_screen(chat_id, user_id, ctx, text="Управление превью-видео.", reply_markup=admin_intro_keyboard(has_video))
        return
    if data == "admin:intro_set":
        ctx.user_data["admin_waiting_for"] = "intro_video"
        await render_screen(chat_id, user_id, ctx, text="Отправьте следующим сообщением видео-файл для превью. Старое видео будет заменено.", reply_markup=Mk([[Btn("◀️ Отмена", callback_data="admin:intro")]]))
        return
    if data == "admin:intro_clear":
        await db.execute("DELETE FROM settings WHERE key = 'intro_video_file_id'")
        await db.commit()
        await render_screen(chat_id, user_id, ctx, text="✅ Превью-видео удалено.", reply_markup=admin_intro_keyboard(False))
        return
    if data == "admin:reviews":
        async with db.execute("SELECT COUNT(*) AS cnt FROM review_photos") as cur:
            count = (await cur.fetchone())["cnt"]
        await render_screen(chat_id, user_id, ctx, text=f"Управление отзывами. Сейчас отзывов: {count}.", reply_markup=admin_reviews_keyboard(count > 0))
        return
    if data == "admin:review_add":
        ctx.user_data["admin_waiting_for"] = "review_photo"
        await render_screen(chat_id, user_id, ctx, text="Отправьте следующим сообщением фото-отзыв.", reply_markup=Mk([[Btn("◀️ Отмена", callback_data="admin:reviews")]]))
        return
    if data == "admin:reviews_clear":
        await db.execute("DELETE FROM review_photos")
        await db.commit()
        await render_screen(chat_id, user_id, ctx, text="✅ Все отзывы удалены.", reply_markup=admin_reviews_keyboard(False))
        return
    if data.startswith("admin:review_show:"):
        offset = int(data.rsplit(":", 1)[1])
        await show_admin_review(chat_id, user_id, offset, ctx)
        return
    if data == "admin:grant":
        ctx.user_data["admin_waiting_for"] = "grant"
        await render_screen(chat_id, user_id, ctx, text="Отправьте: <telegram_id> <days>\nНапример: 123456789 30", reply_markup=Mk([[Btn("◀️ Отмена", callback_data="admin:menu")]]))
        return
    if data == "admin:revoke":
        ctx.user_data["admin_waiting_for"] = "revoke"
        await render_screen(chat_id, user_id, ctx, text="Отправьте telegram_id пользователя, у которого нужно отозвать доступ.", reply_markup=Mk([[Btn("◀️ Отмена", callback_data="admin:menu")]]))
        return
    if data == "admin:stats":
        await show_stats(chat_id, user_id, ctx)
        return


async def show_admin_review(chat_id: int, user_id: int, offset: int, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    db = ctx.bot_data["db"]
    async with db.execute("SELECT COUNT(*) AS cnt FROM review_photos") as cur:
        total = int((await cur.fetchone())["cnt"])
    if total == 0:
        await render_screen(chat_id, user_id, ctx, text="Отзывы не добавлены.", reply_markup=admin_reviews_keyboard(False))
        return
    offset = max(0, min(offset, total - 1))
    async with db.execute("SELECT id, file_id, created_at FROM review_photos ORDER BY id LIMIT 1 OFFSET ?", (offset,)) as cur:
        row = await cur.fetchone()
    rows = []
    nav = []
    if offset > 0:
        nav.append(Btn("⬅️", callback_data=f"admin:review_show:{offset - 1}"))
    if offset < total - 1:
        nav.append(Btn("➡️", callback_data=f"admin:review_show:{offset + 1}"))
    if nav:
        rows.append(nav)
    rows.append([Btn("◀️ К отзывам", callback_data="admin:reviews")])
    await render_screen(
        chat_id,
        user_id,
        ctx,
        kind="photo",
        file_id=row["file_id"],
        text=f"Отзыв {offset + 1}/{total}\nID: {row['id']}\nДобавлен: {format_dt(int(row['created_at']))}",
        reply_markup=Mk(rows),
    )


async def show_stats(chat_id: int, user_id: int, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    db = ctx.bot_data["db"]
    async with db.execute("SELECT COUNT(*) AS cnt FROM users") as cur:
        users_count = (await cur.fetchone())["cnt"]
    async with db.execute("SELECT COUNT(*) AS cnt FROM users WHERE offer_accepted_at IS NOT NULL") as cur:
        accepted_count = (await cur.fetchone())["cnt"]
    async with db.execute("SELECT COUNT(*) AS cnt FROM payments WHERE processed = 1") as cur:
        paid_count = (await cur.fetchone())["cnt"]
    async with db.execute("SELECT COALESCE(SUM(CAST(amount_rub AS REAL)), 0) AS total FROM payments WHERE processed = 1") as cur:
        revenue = (await cur.fetchone())["total"]
    async with db.execute("SELECT COUNT(*) AS cnt FROM subscriptions WHERE access_until > ?", (now_ts(),)) as cur:
        active_count = (await cur.fetchone())["cnt"]
    text = (
        "📊 Статистика\n\n"
        f"Пользователей: {users_count}\n"
        f"Приняли оферту: {accepted_count}\n"
        f"Успешных оплат: {paid_count}\n"
        f"Выручка: {revenue:.2f} ₽\n"
        f"Активных подписок: {active_count}"
    )
    await render_screen(chat_id, user_id, ctx, text=text, reply_markup=Mk([[Btn("◀️ В админ-панель", callback_data="admin:menu")]]))


async def handle_admin_input(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> bool:
    if not update.effective_user or not is_admin(update.effective_user.id):
        return False
    waiting_for = ctx.user_data.get("admin_waiting_for")
    if not waiting_for:
        return False

    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    db = ctx.bot_data["db"]

    if waiting_for == "offer_url":
        if not update.effective_message.text:
            await delete_user_message(update, ctx)
            await render_screen(chat_id, user_id, ctx, text="Нужно отправить ссылку текстом.", reply_markup=Mk([[Btn("◀️ Отмена", callback_data="admin:menu")]]))
            return True
        url = update.effective_message.text.strip()
        await set_setting(db, "offer_url", url)
        await db.commit()
        ctx.user_data.pop("admin_waiting_for", None)
        await delete_user_message(update, ctx)
        await show_admin_menu(chat_id, user_id, ctx, f"✅ Оферта обновлена:\n{url}")
        return True

    if waiting_for == "intro_video":
        if not update.effective_message.video:
            await delete_user_message(update, ctx)
            await render_screen(chat_id, user_id, ctx, text="Нужно отправить именно видео-файл.", reply_markup=Mk([[Btn("◀️ Отмена", callback_data="admin:intro")]]))
            return True
        await set_setting(db, "intro_video_file_id", update.effective_message.video.file_id)
        await db.commit()
        ctx.user_data.pop("admin_waiting_for", None)
        await delete_user_message(update, ctx)
        await render_screen(chat_id, user_id, ctx, text="✅ Превью-видео сохранено.", reply_markup=admin_intro_keyboard(True))
        return True

    if waiting_for == "review_photo":
        if not update.effective_message.photo:
            await delete_user_message(update, ctx)
            await render_screen(chat_id, user_id, ctx, text="Нужно отправить именно фото.", reply_markup=Mk([[Btn("◀️ Отмена", callback_data="admin:reviews")]]))
            return True
        file_id = update.effective_message.photo[-1].file_id
        await db.execute("INSERT INTO review_photos(file_id, created_at) VALUES(?, ?)", (file_id, now_ts()))
        await db.commit()
        ctx.user_data.pop("admin_waiting_for", None)
        await delete_user_message(update, ctx)
        await render_screen(chat_id, user_id, ctx, text="✅ Фото-отзыв добавлен.", reply_markup=admin_reviews_keyboard(True))
        return True

    if waiting_for == "grant":
        raw = (update.effective_message.text or "").split()
        await delete_user_message(update, ctx)
        if len(raw) != 2:
            await render_screen(chat_id, user_id, ctx, text="Нужно отправить два числа: <telegram_id> <days>.", reply_markup=Mk([[Btn("◀️ Отмена", callback_data="admin:menu")]]))
            return True
        try:
            target_user_id = int(raw[0])
            days = int(raw[1])
        except ValueError:
            await render_screen(chat_id, user_id, ctx, text="telegram_id и days должны быть числами.", reply_markup=Mk([[Btn("◀️ Отмена", callback_data="admin:menu")]]))
            return True
        ctx.user_data.pop("admin_waiting_for", None)
        new_until = await grant_access(target_user_id, days, ctx)
        try:
            invite_link = await create_invite_link(target_user_id, ctx)
            await ctx.bot.send_message(target_user_id, f"✅ Администратор выдал доступ до {format_dt(new_until)}.\n{invite_link}")
        except TelegramError:
            logger.exception("failed to notify granted user_id=%s", target_user_id)
        await show_admin_menu(chat_id, user_id, ctx, f"✅ Доступ для {target_user_id} активен до {format_dt(new_until)}.")
        return True

    if waiting_for == "revoke":
        await delete_user_message(update, ctx)
        try:
            target_user_id = int((update.effective_message.text or "").strip())
        except ValueError:
            await render_screen(chat_id, user_id, ctx, text="telegram_id должен быть числом.", reply_markup=Mk([[Btn("◀️ Отмена", callback_data="admin:menu")]]))
            return True
        ctx.user_data.pop("admin_waiting_for", None)
        await revoke_access(target_user_id, ctx)
        await show_admin_menu(chat_id, user_id, ctx, f"✅ Доступ для {target_user_id} отозван.")
        return True

    return False


async def grant_access(user_id: int, days: int, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    db = ctx.bot_data["db"]
    await ensure_user(db, user_id)
    new_until = max(await get_access_until(db, user_id), now_ts()) + days * 24 * 60 * 60
    await db.execute(
        """
        INSERT INTO subscriptions(user_id, access_until, removed_after_expire, updated_at)
        VALUES(?, ?, 0, ?)
        ON CONFLICT(user_id) DO UPDATE SET access_until = excluded.access_until,
            removed_after_expire = 0,
            updated_at = excluded.updated_at
        """,
        (user_id, new_until, now_ts()),
    )
    await db.commit()
    return new_until


async def revoke_access(user_id: int, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    db = ctx.bot_data["db"]
    await db.execute(
        """
        INSERT INTO subscriptions(user_id, access_until, removed_after_expire, updated_at)
        VALUES(?, 0, 1, ?)
        ON CONFLICT(user_id) DO UPDATE SET access_until = 0,
            removed_after_expire = 1,
            updated_at = excluded.updated_at
        """,
        (user_id, now_ts()),
    )
    await db.commit()
    await remove_user_from_channel(ctx, user_id, reason="revoke")

# ── Router/fallback ──────────────────────────────────

async def router(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    data = query.data or ""
    if data == "noop_offer":
        await render_screen(query.message.chat_id, query.from_user.id, ctx, text="Оферта пока не задана. Попробуйте позже.")
        return
    if data.startswith("admin:"):
        await admin_router(query, ctx)
        return
    if data.startswith("user:"):
        await user_router(query, ctx)
        return


async def fallback_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if await handle_admin_input(update, ctx):
        return
    if update.effective_message:
        await delete_user_message(update, ctx)
    if update.effective_chat and update.effective_user:
        await start_offer_again(update.effective_chat.id, update.effective_user.id, ctx)

# ── Command shortcuts: open the same inline UI ───────

@admin_only
async def set_offer_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await delete_user_message(update, ctx)
    ctx.user_data["admin_waiting_for"] = "offer_url"
    await render_screen(update.effective_chat.id, update.effective_user.id, ctx, text="Отправьте новой строкой ссылку на оферту.", reply_markup=Mk([[Btn("◀️ Отмена", callback_data="admin:menu")]]))


@admin_only
async def set_intro_video_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await delete_user_message(update, ctx)
    ctx.user_data["admin_waiting_for"] = "intro_video"
    await render_screen(update.effective_chat.id, update.effective_user.id, ctx, text="Отправьте следующим сообщением видео-файл для превью.", reply_markup=Mk([[Btn("◀️ Отмена", callback_data="admin:intro")]]))


@admin_only
async def add_review_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await delete_user_message(update, ctx)
    ctx.user_data["admin_waiting_for"] = "review_photo"
    await render_screen(update.effective_chat.id, update.effective_user.id, ctx, text="Отправьте следующим сообщением фото-отзыв.", reply_markup=Mk([[Btn("◀️ Отмена", callback_data="admin:reviews")]]))


@admin_only
async def simple_admin_screen_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await delete_user_message(update, ctx)
    command = (update.effective_message.text or "").split()[0]
    if command == "/clear_intro_video":
        await ctx.bot_data["db"].execute("DELETE FROM settings WHERE key = 'intro_video_file_id'")
        await ctx.bot_data["db"].commit()
        await render_screen(update.effective_chat.id, update.effective_user.id, ctx, text="✅ Превью-видео удалено.", reply_markup=admin_intro_keyboard(False))
    elif command == "/clear_reviews":
        await ctx.bot_data["db"].execute("DELETE FROM review_photos")
        await ctx.bot_data["db"].commit()
        await render_screen(update.effective_chat.id, update.effective_user.id, ctx, text="✅ Все отзывы удалены.", reply_markup=admin_reviews_keyboard(False))
    elif command == "/reviews":
        db = ctx.bot_data["db"]
        async with db.execute("SELECT COUNT(*) AS cnt FROM review_photos") as cur:
            count = (await cur.fetchone())["cnt"]
        await render_screen(update.effective_chat.id, update.effective_user.id, ctx, text=f"Управление отзывами. Сейчас отзывов: {count}.", reply_markup=admin_reviews_keyboard(count > 0))
    elif command == "/stats":
        await show_stats(update.effective_chat.id, update.effective_user.id, ctx)
    elif command == "/grant":
        ctx.user_data["admin_waiting_for"] = "grant"
        await render_screen(update.effective_chat.id, update.effective_user.id, ctx, text="Отправьте: <telegram_id> <days>", reply_markup=Mk([[Btn("◀️ Отмена", callback_data="admin:menu")]]))
    elif command == "/revoke":
        ctx.user_data["admin_waiting_for"] = "revoke"
        await render_screen(update.effective_chat.id, update.effective_user.id, ctx, text="Отправьте telegram_id пользователя.", reply_markup=Mk([[Btn("◀️ Отмена", callback_data="admin:menu")]]))
    else:
        await show_admin_menu(update.effective_chat.id, update.effective_user.id, ctx)

# ── Запуск ───────────────────────────────────────────

def build_application() -> Application:
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is required")
    app = Application.builder().token(BOT_TOKEN).post_init(init_db).post_shutdown(close_db).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("admin", admin))
    app.add_handler(CommandHandler("set_offer", set_offer_cmd))
    app.add_handler(CommandHandler("set_intro_video", set_intro_video_cmd))
    app.add_handler(CommandHandler("add_review", add_review_cmd))
    app.add_handler(CommandHandler(["clear_intro_video", "reviews", "clear_reviews", "grant", "revoke", "stats"], simple_admin_screen_cmd))
    app.add_handler(CallbackQueryHandler(router))
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, fallback_message))

    if app.job_queue is None:
        raise RuntimeError("JobQueue is unavailable. Install python-telegram-bot[job-queue].")
    app.job_queue.run_repeating(expire_subscriptions_job, interval=3600, first=60, name="expire_subscriptions")
    return app


def main() -> None:
    app = build_application()
    logger.info("starting polling bot project_key=%s channel_id=%s", PROJECT_KEY, CHANNEL_ID)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
