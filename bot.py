#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Telegram-бот платного доступа к закрытому каналу."""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from decimal import Decimal
from types import SimpleNamespace
from urllib.parse import urlsplit, parse_qs
import os
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal

import httpx
from dotenv import load_dotenv
from telegram import InlineKeyboardButton as Btn
from telegram import InlineKeyboardMarkup as Mk
from telegram import InputMediaPhoto, InputMediaVideo, Update, User
from telegram.error import BadRequest, Forbidden, TelegramError
from telegram.ext import Application, CallbackQueryHandler, ChatMemberHandler, CommandHandler, ContextTypes, MessageHandler, filters

import db
import texts

try:
    from telegram import LinkPreviewOptions
except ImportError:  # PTB 20.0–20.7
    LinkPreviewOptions = None

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
YOOKASSA_SHOP_ID = os.getenv("YOOKASSA_SHOP_ID", "")
YOOKASSA_SECRET_KEY = os.getenv("YOOKASSA_SECRET_KEY", "")
PROJECT_KEY = os.getenv("PROJECT_KEY", "creative_brain")
OFFER_VERSION = os.getenv("OFFER_VERSION", "1")
FOLLOWUP_ENABLED = os.getenv("FOLLOWUP_ENABLED", "true").lower() in {"1", "true", "yes", "on"}
CHANNEL_ID = int(os.getenv("CHANNEL_ID", "-1002468345259"))
ENV_OFFER_URL = os.getenv("OFFER_URL", "").strip()
YOOKASSA_RETURN_URL = os.getenv("YOOKASSA_RETURN_URL", "").strip()
BOT_USERNAME = os.getenv("BOT_USERNAME", "").lstrip("@").strip()
YOOKASSA_API_URL = "https://api.yookassa.ru/v3/payments"

ScreenKind = Literal["text", "video", "photo"]


@dataclass(frozen=True)
class Tariff:
    code: str
    title: str
    amount_rub: int
    days: int


CURRENT_TARIFF = Tariff("creative_30", "Креативный мозг — 30 дней", 400, 30)
# Read-only compatibility for invoices already stored in SQLite; never exposed in the UI.
PAYMENT_PLANS = {
    CURRENT_TARIFF.code: CURRENT_TARIFF,
    "m1_first": Tariff("m1_first", "Архивный доступ", 399, 30),
    "m1": Tariff("m1", "Архивный доступ", 500, 30),
    "m3": Tariff("m3", "Архивный доступ", 1200, 90),
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("creative_brain_bot")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


def parse_admin_ids() -> set[int]:
    # The first non-empty setting is authoritative; never merge extra legacy IDs.
    raw = next((os.getenv(key, "").strip() for key in ("ADMIN_ID", "ADMIN_IDS", "ADMINS")
                if os.getenv(key, "").strip()), "")
    admin_ids: set[int] = set()
    for part in re.split(r"[,\s]+", raw.strip()):
        if not part:
            continue
        try:
            admin_id = int(part)
            if admin_id <= 0:
                raise ValueError
            admin_ids.add(admin_id)
        except ValueError:
            logger.warning("invalid admin id skipped: %s", part)
    if not admin_ids:
        logger.warning("no admin ids configured; /admin will be unavailable")
    return admin_ids


ADMIN_IDS = parse_admin_ids()
ADMIN_MENU_LABEL = "⚙️ Админ-меню"


def with_admin_menu(tg_user_id: int, reply_markup: Mk | None = None) -> Mk | None:
    """Expose navigation only to configured admins; callbacks still check permissions."""
    if not is_admin(tg_user_id):
        return reply_markup
    rows = [list(row) for row in reply_markup.inline_keyboard] if reply_markup else []
    if not any(button.callback_data == "admin:menu" for row in rows for button in row):
        rows.append([Btn(ADMIN_MENU_LABEL, callback_data="admin:menu")])
    return Mk(rows)


async def init_storage(app: Application) -> None:
    await db.init_db()
    if ENV_OFFER_URL and not await db.get_offer_url():
        await db.set_offer_url(ENV_OFFER_URL)
    logger.info("database initialized at %s", db.DB_PATH)


async def close_storage(app: Application) -> None:
    await db.close_db()


def user_full_name(user: User) -> str:
    return " ".join(part for part in (user.first_name, user.last_name) if part)


async def remember_user(user: User) -> None:
    await db.upsert_user(user.id, user.username, user_full_name(user))


def parse_dt(value: str | None) -> datetime | None:
    return db.parse_dt(value)


def format_dt(value: str | None) -> str:
    parsed = parse_dt(value)
    if not parsed:
        return "неизвестно"
    return parsed.strftime("%d.%m.%Y %H:%M UTC")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def safe_delete_message(ctx: ContextTypes.DEFAULT_TYPE, chat_id: int, message_id: int) -> None:
    if not chat_id or not message_id:
        return
    try:
        await ctx.bot.delete_message(chat_id=chat_id, message_id=message_id)
    except TelegramError:
        logger.debug("message delete skipped chat_id=%s message_id=%s", chat_id, message_id, exc_info=True)


async def delete_incoming(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat and update.effective_message:
        await safe_delete_message(ctx, update.effective_chat.id, update.effective_message.message_id)


async def render_screen(
    chat_id: int,
    tg_user_id: int,
    ctx: ContextTypes.DEFAULT_TYPE,
    *,
    text: str,
    reply_markup: Mk | None = None,
    kind: ScreenKind = "text",
    file_id: str | None = None,
    include_admin_shortcut: bool = True,
) -> None:
    """Render one actual bot screen and keep its coordinates in users.current_* fields."""
    if include_admin_shortcut:
        reply_markup = with_admin_menu(tg_user_id, reply_markup)
    current = await db.get_current_message(tg_user_id) or {}
    old_chat_id = int(current.get("current_chat_id") or 0)
    old_message_id = int(current.get("current_message_id") or 0)
    old_kind = str(current.get("current_message_kind") or "")

    if old_message_id and old_chat_id == chat_id and old_kind == kind:
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

    if old_chat_id and old_message_id:
        await safe_delete_message(ctx, old_chat_id, old_message_id)
        await db.clear_current_message(tg_user_id)

    if kind == "video":
        msg = await ctx.bot.send_video(chat_id=chat_id, video=file_id, caption=text, reply_markup=reply_markup)
    elif kind == "photo":
        msg = await ctx.bot.send_photo(chat_id=chat_id, photo=file_id, caption=text, reply_markup=reply_markup)
    else:
        msg = await ctx.bot.send_message(chat_id=chat_id, text=text, reply_markup=reply_markup)
    await db.set_current_message(tg_user_id, chat_id, msg.message_id, kind)


def offer_keyboard(offer_url: str | None) -> Mk:
    if not valid_http_url(offer_url):
        return next_keyboard("user:product", "🧠 Креативный мозг")
    return Mk([
        [Btn("📄 Открыть оферту", url=offer_url)],
        [Btn("✅ Принимаю, идём дальше", callback_data="user:accept_offer_and_pay")],
    ])


def next_keyboard(callback_data: str, label: str = "Далее ➡️") -> Mk:
    return Mk([[Btn(label, callback_data=callback_data)]])


def payment_keyboard(payment_id: str, confirmation_url: str | None, retry: bool = False) -> Mk:
    check = Btn("✅ Проверить ещё раз" if retry else "✅ Проверить оплату",
                callback_data=f"user:check_payment:{payment_id}")
    if retry:
        rows = [[check]]
        if confirmation_url:
            rows.append([Btn("💳 Открыть оплату", url=confirmation_url)])
    else:
        rows = [[Btn("💳 Оплатить 400 ₽", url=confirmation_url)], [check]]
    return Mk(rows)


def active_subscription_until(sub: dict | None) -> str | None:
    if not sub or sub.get("status") != "active":
        return None
    access_until = sub.get("access_until")
    parsed_until = db.parse_dt(access_until)
    if not parsed_until or parsed_until <= datetime.now(timezone.utc):
        return None
    return access_until


async def show_active_access(chat_id: int, tg_user_id: int, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    access_until = active_subscription_until(await db.get_subscription(tg_user_id))
    if not access_until:
        await send_lead_magnet(chat_id, tg_user_id, ctx)
        return
    await render_screen(chat_id, tg_user_id, ctx,
                        text=texts.ACTIVE_ACCESS.format(date=format_dt(access_until)),
                        reply_markup=next_keyboard("user:buy", "Продлить доступ →"))


async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    user, chat = update.effective_user, update.effective_chat
    if not user or not chat or chat.type != "private":
        return
    await remember_user(user)
    await db.clear_admin_state(user.id)
    source = ctx.args[0] if ctx.args and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", ctx.args[0]) else "direct"
    await db.record_start(user.id, source)
    if active_subscription_until(await db.get_subscription(user.id)):
        await show_active_access(chat.id, user.id, ctx)
    else:
        await send_lead_magnet(chat.id, user.id, ctx)


async def start_offer_again(chat_id: int, tg_user_id: int, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    offer_url = await db.get_offer_url()
    if not valid_http_url(offer_url):
        await render_screen(chat_id, tg_user_id, ctx,
                            text="Оферта пока не настроена. Оплата временно недоступна. Попробуй позже.",
                            reply_markup=next_keyboard("user:product", "🧠 Креативный мозг"))
        return
    ctx.user_data["offered_terms"] = (offer_url, OFFER_VERSION)
    await render_screen(chat_id, tg_user_id, ctx, text=texts.OFFER, reply_markup=offer_keyboard(offer_url))


async def user_router(query, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    tg_user_id = query.from_user.id
    chat_id = query.message.chat_id
    await remember_user(query.from_user)
    data = query.data or ""
    if data in {"user:lead_magnet", "user:start_intro"}:
        if active_subscription_until(await db.get_subscription(tg_user_id)):
            await show_active_access(chat_id, tg_user_id, ctx)
        else:
            await send_lead_magnet(chat_id, tg_user_id, ctx, welcome=False)
    elif (data in {"user:product", "user:after_intro", "user:show_reviews", "user:tariffs"}
          or data.startswith(("user:review:", "user:tariff:"))):
        # Historical tariff buttons never create a new legacy payment.
        await show_product(chat_id, tg_user_id, ctx)
    elif data == "user:buy":
        await db.record_event(tg_user_id, "buy_clicked")
        if active_subscription_until(await db.get_subscription(tg_user_id)):
            await db.record_event(tg_user_id, "renewal_started")
        await buy_access(chat_id, tg_user_id, ctx)
    elif data == "user:accept_offer_and_pay":
        terms = (await db.get_offer_url(), OFFER_VERSION)
        if not valid_http_url(terms[0]) or ctx.user_data.get("offered_terms") != terms:
            await start_offer_again(chat_id, tg_user_id, ctx)
            return
        if not await db.has_accepted_offer(tg_user_id, OFFER_VERSION):
            await db.accept_offer(tg_user_id, OFFER_VERSION)
            await db.record_event(tg_user_id, "offer_accepted", {"version": OFFER_VERSION})
        await create_payment_and_render(chat_id, tg_user_id, CURRENT_TARIFF, ctx)
    elif data == "user:accept_offer":
        # An old button did not disclose the new terms or authorize payment.
        await start_offer_again(chat_id, tg_user_id, ctx)
    elif data.startswith("user:check_payment:"):
        await check_payment_and_grant(chat_id, tg_user_id, data.split(":", 2)[2], ctx)
    else:
        await show_product(chat_id, tg_user_id, ctx)


async def create_payment_and_render(chat_id: int, tg_user_id: int, tariff: Tariff, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if tariff != CURRENT_TARIFF:
        raise ValueError("Only the current tariff may be purchased")
    if not await db.has_accepted_offer(tg_user_id, OFFER_VERSION):
        await start_offer_again(chat_id, tg_user_id, ctx)
        return
    back = next_keyboard("user:buy", "Попробовать ещё раз →")
    if not YOOKASSA_SHOP_ID or not YOOKASSA_SECRET_KEY:
        await render_screen(chat_id, tg_user_id, ctx, text="Оплата временно недоступна: YooKassa не настроена.", reply_markup=back)
        logger.error("payment creation blocked: empty YooKassa credentials")
        return
    pending = await db.get_latest_pending_payment(tg_user_id)
    if (pending and pending["plan_id"] == CURRENT_TARIFF.code and pending.get("confirmation_url")
            and parse_dt(pending["created_at"]) > datetime.now(timezone.utc) - timedelta(hours=24)):
        await render_screen(chat_id, tg_user_id, ctx, text=texts.PAYMENT_CREATED,
                            reply_markup=payment_keyboard(pending["payment_id"], pending["confirmation_url"]))
        return
    return_url = YOOKASSA_RETURN_URL or (f"https://t.me/{BOT_USERNAME}" if BOT_USERNAME else "https://t.me/")
    metadata = {
        "project_key": PROJECT_KEY, "user_id": str(tg_user_id), "kind": "channel_access",
        "tariff_code": tariff.code, "days": str(tariff.days),
    }
    payload = {
        "amount": {"value": f"{tariff.amount_rub:.2f}", "currency": "RUB"},
        "capture": True,
        "confirmation": {"type": "redirect", "return_url": return_url},
        "description": "Доступ к «Креативному мозгу» на 30 дней",
        "metadata": metadata,
    }
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(YOOKASSA_API_URL, json=payload,
                                         auth=(YOOKASSA_SHOP_ID, YOOKASSA_SECRET_KEY),
                                         headers={"Idempotence-Key": str(uuid.uuid4())})
            response.raise_for_status()
            payment = response.json()
        payment_id = payment.get("id")
        confirmation_url = (payment.get("confirmation") or {}).get("confirmation_url")
        if not payment_id or not valid_http_url(confirmation_url):
            raise ValueError("Missing payment id or confirmation URL")
    except (httpx.HTTPError, ValueError, TypeError, AttributeError, ArithmeticError) as exc:
        logger.error("payment creation failed user_id=%s error_type=%s", tg_user_id, type(exc).__name__)
        await render_screen(chat_id, tg_user_id, ctx, text="Не получилось создать платёж. Попробуй позже.", reply_markup=back)
        return
    await db.create_payment(tg_user_id, payment_id, tariff.code, tariff.amount_rub, confirmation_url, metadata, payment)
    await db.record_event(tg_user_id, "payment_created", {"payment_id": payment_id, "amount_rub": tariff.amount_rub},
                          event_key=f"created:{payment_id}")
    await render_screen(chat_id, tg_user_id, ctx, text=texts.PAYMENT_CREATED,
                        reply_markup=payment_keyboard(payment_id, confirmation_url))


async def check_payment_and_grant(chat_id: int, tg_user_id: int, payment_id: str, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    payment_row = await db.get_payment(payment_id)
    if not payment_row or int(payment_row["tg_user_id"]) != tg_user_id:
        await render_screen(chat_id, tg_user_id, ctx, text="Платёж не найден для твоего аккаунта.",
                            reply_markup=next_keyboard("user:product", "🧠 Креативный мозг"))
        return
    if payment_row.get("finalized_at"):
        await send_existing_access(chat_id, tg_user_id, payment_id, ctx)
        return
    try:
        payment = await refresh_payment(payment_row)
    except (httpx.HTTPError, ValueError, TypeError, AttributeError, ArithmeticError) as exc:
        logger.error("payment check failed payment_id=%s error_type=%s", payment_id, type(exc).__name__)
        await render_screen(chat_id, tg_user_id, ctx, text="Не получилось проверить платёж. Попробуй ещё раз через минуту.",
                            reply_markup=payment_keyboard(payment_id, payment_row.get("confirmation_url"), retry=True))
        return
    if payment["status"] != "succeeded":
        if payment["status"] == "canceled":
            await render_screen(chat_id, tg_user_id, ctx, text="Этот платёж отменён. Можно создать новый счёт.",
                                reply_markup=next_keyboard("user:buy", "Поехали →"))
        else:
            await render_screen(chat_id, tg_user_id, ctx, text=texts.PAYMENT_PENDING,
                                reply_markup=payment_keyboard(payment_id, payment_row.get("confirmation_url"), retry=True))
        return
    tariff = PAYMENT_PLANS.get(payment_row["plan_id"])
    if not tariff:
        await render_screen(chat_id, tg_user_id, ctx, text="Неизвестный план платежа. Напиши администратору.")
        return
    # Compare to the immutable local binding, so pre-rebrand invoices still finalize.
    metadata = json.loads(payment_row["metadata_json"])
    result = await db.finalize_payment_once(payment_id, tg_user_id, metadata["project_key"],
                                            "channel_access", tariff.code, tariff.days)
    if not result.get("ok"):
        logger.error("payment finalize failed payment_id=%s error=%s", payment_id, result.get("error"))
        await render_screen(chat_id, tg_user_id, ctx, text="Платёж не прошёл внутреннюю проверку. Напиши администратору.")
        return
    await send_access_link(chat_id, tg_user_id, payment_id, result["access_until"], ctx,
                           already_finalized=result.get("already_finalized", False))


async def create_invite_link(tg_user_id: int, ctx: ContextTypes.DEFAULT_TYPE) -> tuple[str, str]:
    expire_at = datetime.now(timezone.utc) + timedelta(hours=1)
    invite = await ctx.bot.create_chat_invite_link(
        chat_id=CHANNEL_ID,
        name=f"access_{tg_user_id}_{int(datetime.now().timestamp())}",
        member_limit=1,
        expire_date=expire_at,
    )
    logger.info("created invite link user_id=%s channel_id=%s expire_at=%s", tg_user_id, CHANNEL_ID, expire_at.isoformat())
    return invite.invite_link, expire_at.isoformat()


async def send_access_link(chat_id: int, tg_user_id: int, payment_id: str | None, access_until: str | None,
                           ctx: ContextTypes.DEFAULT_TYPE, already_finalized: bool = False) -> None:
    existing = await db.get_invite_link_by_payment(payment_id) if payment_id else None
    try:
        if existing and not existing["used"] and parse_dt(existing["expire_at"]) and parse_dt(existing["expire_at"]) > datetime.now(timezone.utc):
            invite_link = existing["invite_link"]
        else:
            # Retire still-valid links before replacing one; never extend access here.
            for row in await db.list_open_invites(tg_user_id):
                await ctx.bot.revoke_chat_invite_link(CHANNEL_ID, row["invite_link"])
                await db.mark_invite_used(row["invite_link"])
            await ctx.bot.unban_chat_member(CHANNEL_ID, tg_user_id, only_if_banned=True)
            invite_link, expire_at = await create_invite_link(tg_user_id, ctx)
            await db.save_invite_link(tg_user_id, invite_link, payment_id=payment_id, expire_at=expire_at)
        await render_screen(chat_id, tg_user_id, ctx,
                            text=texts.PAYMENT_SUCCEEDED.format(date=format_dt(access_until)),
                            reply_markup=Mk([[Btn("🧠 Войти в «Креативный мозг»", url=invite_link)]]))
        # Keep the entry button available in history even if /start is called before joining.
        await db.clear_current_message(tg_user_id)
    except TelegramError as exc:
        logger.error("invite delivery failed user_id=%s channel_id=%s error_type=%s", tg_user_id, CHANNEL_ID, type(exc).__name__)
        await render_screen(chat_id, tg_user_id, ctx,
                            text=f"Оплата сохранена. Доступ активен до {format_dt(access_until)}. Не удалось выдать ссылку. Попробуй проверить оплату ещё раз или напиши администратору.",
                            reply_markup=next_keyboard(f"user:check_payment:{payment_id}", "✅ Повторить выдачу ссылки") if payment_id else None)


async def send_existing_access(chat_id: int, tg_user_id: int, payment_id: str | None, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    access_until = active_subscription_until(await db.get_subscription(tg_user_id))
    if access_until:
        await send_access_link(chat_id, tg_user_id, payment_id, access_until, ctx, already_finalized=True)
        return
    await render_screen(chat_id, tg_user_id, ctx, text="Этот платёж уже обработан. Текущий доступ закончился или был отозван.",
                        reply_markup=next_keyboard("user:buy", "Вернуться в канал →"))


async def send_subscription_renewal_notice(tg_user_id: int, access_until: str | None, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await ctx.bot.send_message(tg_user_id, texts.RENEWAL_NOTICE.format(date=format_dt(access_until)),
                               reply_markup=with_admin_menu(tg_user_id, next_keyboard("user:buy", "Продолжить с нами →")))


async def send_subscription_expired_notice(tg_user_id: int, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await ctx.bot.send_message(tg_user_id, texts.EXPIRED,
                               reply_markup=with_admin_menu(tg_user_id, next_keyboard("user:buy", "Вернуться в канал →")))


async def renewal_notices_job(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    for sub in await db.list_subscriptions_due_for_renewal_notice(now_iso(), days_before=3):
        tg_user_id = int(sub["tg_user_id"])
        async with user_lock(tg_user_id):
            current = await db.get_subscription(tg_user_id)
            until = active_subscription_until(current)
            if (not until or current["renewal_notice_sent_at"]
                    or parse_dt(until) > datetime.now(timezone.utc) + timedelta(days=3)):
                continue
            try:
                await send_subscription_renewal_notice(tg_user_id, until, ctx)
                await db.mark_renewal_notice_sent(tg_user_id)
            except TelegramError:
                logger.warning("renewal notification failed user_id=%s", tg_user_id)


async def expire_subscriptions_job(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    for sub in await db.list_expired_subscriptions(now_iso()):
        tg_user_id = int(sub["tg_user_id"])
        async with user_lock(tg_user_id):
            current = await db.get_subscription(tg_user_id)
            if not current or current["status"] != "active" or active_subscription_until(current):
                continue
            if not await remove_user_from_channel(ctx, tg_user_id, reason="expired"):
                continue  # Retry next job run if permissions or Telegram are unavailable.
            await db.mark_subscription_expired(tg_user_id)
            await db.record_event(tg_user_id, "expired", {"access_until": current["access_until"]},
                                  event_key=f"expired:{tg_user_id}:{current['access_until']}")
            try:
                await send_subscription_expired_notice(tg_user_id, ctx)
            except TelegramError:
                logger.warning("expiration notification failed user_id=%s", tg_user_id)


async def remove_user_from_channel(ctx: ContextTypes.DEFAULT_TYPE, tg_user_id: int, reason: str) -> bool:
    try:
        for row in await db.list_open_invites(tg_user_id):
            await ctx.bot.revoke_chat_invite_link(CHANNEL_ID, row["invite_link"])
            await db.mark_invite_used(row["invite_link"])
        await ctx.bot.ban_chat_member(CHANNEL_ID, tg_user_id)
        await ctx.bot.unban_chat_member(CHANNEL_ID, tg_user_id, only_if_banned=True)
        logger.info("removed user_id=%s channel_id=%s reason=%s", tg_user_id, CHANNEL_ID, reason)
        return True
    except TelegramError as exc:
        logger.error("channel removal failed user_id=%s reason=%s error_type=%s", tg_user_id, reason, type(exc).__name__)
        return False


# ── Админ-панель ─────────────────────────────────────

def is_admin(tg_user_id: int) -> bool:
    return tg_user_id in ADMIN_IDS


def admin_menu_keyboard() -> Mk:
    return Mk([
        [Btn("🎬 Лид-магнит", callback_data="admin:lead_magnet")],
        [Btn("📄 Оферта", callback_data="admin:offer")],
        [Btn("🎁 Выдать доступ", callback_data="admin:grant")],
        [Btn("🚫 Отозвать доступ", callback_data="admin:revoke")],
        [Btn("📊 Воронка / статистика", callback_data="admin:stats")],
        [Btn("👤 Посмотреть путь пользователя", callback_data="admin:user_path")],
    ])


async def admin(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    chat = update.effective_chat
    if not user or not chat or chat.type != "private" or not is_admin(user.id):
        return
    await remember_user(user)
    await delete_incoming(update, ctx)
    await show_admin_menu(chat.id, user.id, ctx)


async def show_admin_menu(chat_id: int, tg_user_id: int, ctx: ContextTypes.DEFAULT_TYPE, note: str = "") -> None:
    if chat_id != tg_user_id or not is_admin(tg_user_id):
        return
    await db.clear_admin_state(tg_user_id)
    text = "Админ-панель\n\nВыберите действие кнопками ниже."
    if note:
        text = f"{note}\n\n{text}"
    # An admin shortcut can belong to an old lesson or a newer standalone notice.
    # Open the menu at the bottom of the chat instead of editing an unrelated screen.
    await db.clear_current_message(tg_user_id)
    await render_screen(chat_id, tg_user_id, ctx, text=text, reply_markup=admin_menu_keyboard(),
                        include_admin_shortcut=False)


async def admin_router(query, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    tg_user_id, chat_id = query.from_user.id, query.message.chat_id
    if chat_id != tg_user_id or not is_admin(tg_user_id):
        return
    await remember_user(query.from_user)
    data = query.data or ""
    await db.clear_admin_state(tg_user_id)
    if data == "admin:offer":
        await db.set_admin_state(tg_user_id, "offer_url")
        await render_screen(chat_id, tg_user_id, ctx,
                            text=f"Текущая оферта:\n{await db.get_offer_url() or 'не задана'}\n\nОтправьте ссылку на оферту.",
                            reply_markup=admin_cancel_keyboard())
    elif data in {"admin:lead_magnet", "admin:intro"}:
        await show_admin_lead_magnet(chat_id, tg_user_id, ctx)
    elif data in {"admin:lead_set", "admin:intro_set"}:
        await db.set_admin_state(tg_user_id, "lead_magnet_url")
        await render_screen(chat_id, tg_user_id, ctx, text="Отправьте YouTube URL урока: https://youtube.com/watch?v=… или https://youtu.be/…",
                            reply_markup=admin_cancel_keyboard())
    elif data in {"admin:lead_clear", "admin:intro_clear"}:
        await db.clear_lead_magnet_url()
        await show_admin_lead_magnet(chat_id, tg_user_id, ctx)
    elif data in {"admin:lead_test", "admin:user_start"}:
        await send_lead_magnet(chat_id, tg_user_id, ctx, track=False)
    elif data in {"admin:grant", "admin:revoke", "admin:user_path"}:
        state = data.split(":")[1]
        await db.set_admin_state(tg_user_id, state)
        prompt = "Отправьте: <telegram_id> <days>\nНапример: 123456789 30" if state == "grant" else "Отправьте telegram_id пользователя."
        await render_screen(chat_id, tg_user_id, ctx, text=prompt, reply_markup=admin_cancel_keyboard())
    elif data == "admin:stats":
        await show_stats(chat_id, tg_user_id, ctx)
    else:
        await show_admin_menu(chat_id, tg_user_id, ctx)


async def show_stats(chat_id: int, tg_user_id: int, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if chat_id != tg_user_id or not is_admin(tg_user_id):
        return
    s = await db.funnel_stats()
    events = s["events"]
    def count(event, field="users"):
        return events.get(event, {}).get(field, 0)
    def conversion(event):
        denominator = count(event)
        return f"{100 * s['conversions'][event] / denominator:.1f}%" if denominator else "0.0%"
    lines = [
        "📊 Воронка «Креативный мозг»",
        f"Всего Start: {count('start', 'total')} (людей: {count('start')})",
        f"Получили лид-магнит: {count('lead_magnet_sent')}",
        f"Открыли оффер: {count('product_opened')}",
        f"Создали платёж: {count('payment_created')} чел. / {count('payment_created', 'total')} счетов",
        f"Успешные оплаты: {s['paid_count']} (в новой воронке: {count('payment_succeeded', 'total')})",
        f"Start → Payment: {conversion('start')}",
        f"Product open → Payment: {conversion('product_opened')}",
        f"Активных подписок: {s['active_subscriptions_count']}",
        f"Общая выручка: {s['revenue_rub']} ₽",
        f"Всего пользователей: {s['users_count']}",
        f"Приняли оферту (включая историю): {s['accepted_offer_count']}",
        "",
        "Source: Start / Paid (уникальные люди, первый источник)",
    ]
    lines.extend(f"{row['source']}: {row['starts']} / {row['paid']}" for row in s["sources"])
    # Paginate long source lists without exceeding Telegram's 4096-character limit.
    pages, page = [], ""
    for line in lines:
        if len(page) + len(line) + 1 > 3500:
            pages.append(page)
            page = ""
        page += line + "\n"
    pages.append(page)
    for page in pages[:-1]:
        await ctx.bot.send_message(chat_id, page, reply_markup=with_admin_menu(tg_user_id))
    await render_screen(chat_id, tg_user_id, ctx, text=pages[-1], reply_markup=admin_cancel_keyboard())


async def handle_admin_input(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> bool:
    user, chat, message = update.effective_user, update.effective_chat, update.effective_message
    if not user or not chat or not message or chat.id != user.id or not is_admin(user.id):
        return False
    state = await db.get_admin_state(user.id)
    if not state:
        return False
    await remember_user(user)
    name, raw = state["state"], (message.text or "").strip()
    if name in {"lead_magnet_url", "intro_video", "offer_url"}:
        valid = valid_http_url(raw) if name == "offer_url" else valid_youtube_url(raw)
        if not valid:
            await render_screen(chat.id, user.id, ctx,
                                text="Нужна корректная http/https ссылка." if name == "offer_url" else "Нужна ссылка на видео YouTube (youtube.com или youtu.be).",
                                reply_markup=admin_cancel_keyboard())
            return True
        if name == "offer_url":
            await db.set_offer_url(raw)
        else:
            await db.set_lead_magnet_url(raw)
        await db.clear_admin_state(user.id)
        await delete_incoming(update, ctx)
        if name == "offer_url":
            await show_admin_menu(chat.id, user.id, ctx, "✅ Оферта обновлена.")
        else:
            await show_admin_lead_magnet(chat.id, user.id, ctx)
        return True
    if name == "user_path":
        if not raw.isdigit() or int(raw) <= 0:
            await render_screen(chat.id, user.id, ctx, text="telegram_id должен быть положительным числом.", reply_markup=admin_cancel_keyboard())
            return True
        rows = await db.get_user_path(int(raw))
        lines = [f"Путь пользователя {raw} — последние 25 событий"]
        lines += [f"{format_dt(row['created_at'])} · {row['event_name']} · {row['source']}" for row in rows]
        if not rows:
            lines.append("Событий пока нет.")
        await db.clear_admin_state(user.id)
        await render_screen(chat.id, user.id, ctx, text="\n".join(lines), reply_markup=admin_cancel_keyboard())
        return True
    if name in {"grant", "revoke"}:
        parts = raw.split()
        try:
            if len(parts) != (2 if name == "grant" else 1):
                raise ValueError
            target = int(parts[0])
            days = int(parts[1]) if name == "grant" else 0
            if target <= 0 or (name == "grant" and not 1 <= days <= 36500):
                raise ValueError
        except ValueError:
            await render_screen(chat.id, user.id, ctx,
                                text="Нужны положительный telegram_id и срок 1–36500 дней." if name == "grant" else "Нужен положительный telegram_id.",
                                reply_markup=admin_cancel_keyboard())
            return True
        async with user_lock(target):
            if name == "grant":
                sub = await db.grant_access(target, days)
                note = f"✅ Доступ для {target} активен до {format_dt(sub['access_until'])}."
                try:
                    await ctx.bot.unban_chat_member(CHANNEL_ID, target, only_if_banned=True)
                    invite, expires = await create_invite_link(target, ctx)
                    await db.save_invite_link(target, invite, expire_at=expires)
                    await ctx.bot.send_message(target, f"✅ Администратор выдал доступ к «Креативному мозгу» до {format_dt(sub['access_until'])}. Ссылка действует 1 час. Начни с закреплённого сообщения и Урока 1.",
                                               reply_markup=with_admin_menu(target, Mk([[Btn("🧠 Войти в «Креативный мозг»", url=invite)]])))
                except TelegramError:
                    note += "\nНе удалось доставить ссылку. Проверьте права бота и доступность личного чата; срок уже записан."
                    logger.warning("admin grant delivery failed user_id=%s", target)
            else:
                if not await remove_user_from_channel(ctx, target, reason="revoke"):
                    await render_screen(chat.id, user.id, ctx, text="Не удалось удалить пользователя из канала. Проверьте права бота и повторите telegram_id.", reply_markup=admin_cancel_keyboard())
                    return True
                await db.revoke_access(target)
                note = f"✅ Доступ для {target} отозван."
        await db.clear_admin_state(user.id)
        await delete_incoming(update, ctx)
        await show_admin_menu(chat.id, user.id, ctx, note)
        return True
    await db.clear_admin_state(user.id)
    await show_admin_menu(chat.id, user.id, ctx, "Это действие больше не используется.")
    return True


async def router(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.message or query.message.chat.type != "private":
        return
    try:
        await query.answer()
    except TelegramError:
        logger.debug("callback answer unavailable")
    if (query.data or "").startswith("admin:"):
        await admin_router(query, ctx)
    else:
        async with user_lock(query.from_user.id):
            await user_router(query, ctx)


async def fallback_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    user, chat = update.effective_user, update.effective_chat
    if not user or not chat or chat.type != "private":
        return
    if await handle_admin_input(update, ctx):
        return
    await remember_user(user)
    if active_subscription_until(await db.get_subscription(user.id)):
        await show_active_access(chat.id, user.id, ctx)
    else:
        await render_screen(chat.id, user.id, ctx, text=texts.FALLBACK, reply_markup=Mk([
            [Btn("▶️ Бесплатный урок", callback_data="user:lead_magnet")],
            [Btn("🧠 Креативный мозг", callback_data="user:product")],
        ]))


async def admin_shortcut(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    user, chat = update.effective_user, update.effective_chat
    if not user or not chat or chat.type != "private" or not is_admin(user.id):
        return
    await remember_user(user)
    command = (update.effective_message.text or "").split()[0].split("@")[0]
    routes = {
        "/set_offer": "admin:offer", "/set_lead_magnet": "admin:lead_set",
        "/set_intro_video": "admin:lead_set", "/clear_lead_magnet": "admin:lead_clear",
        "/clear_intro_video": "admin:lead_clear", "/grant": "admin:grant",
        "/revoke": "admin:revoke", "/stats": "admin:stats", "/user_path": "admin:user_path",
    }
    query = SimpleNamespace(from_user=user, message=SimpleNamespace(chat_id=chat.id),
                            data=routes.get(command, "admin:menu"))
    await admin_router(query, ctx)


_user_locks: dict[int, asyncio.Lock] = {}


@asynccontextmanager
async def user_lock(tg_user_id: int):
    lock = _user_locks.setdefault(tg_user_id, asyncio.Lock())
    async with lock:
        yield


def valid_http_url(url: str | None) -> bool:
    if not isinstance(url, str) or not url or len(url) > 2048 or any(c.isspace() or ord(c) < 32 for c in url):
        return False
    try:
        parsed = urlsplit(url)
        return bool(parsed.scheme in {"http", "https"} and parsed.hostname
                    and not parsed.username and not parsed.password
                    and (parsed.port is None or 1 <= parsed.port <= 65535))
    except ValueError:
        return False


def valid_youtube_url(url: str | None) -> bool:
    if not valid_http_url(url):
        return False
    parsed = urlsplit(url)
    host = parsed.hostname.lower()
    if host in {"youtu.be", "www.youtu.be"}:
        video_id = parsed.path.strip("/")
    elif host in {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"}:
        if parsed.path == "/watch":
            video_id = parse_qs(parsed.query).get("v", [""])[0]
        else:
            parts = parsed.path.strip("/").split("/")
            video_id = parts[1] if len(parts) == 2 and parts[0] in {"shorts", "live", "embed"} else ""
    else:
        return False
    return bool(re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id))


async def send_lead_magnet(chat_id: int, tg_user_id: int, ctx: ContextTypes.DEFAULT_TYPE,
                           welcome: bool = True, track: bool = True) -> None:
    url = await db.get_lead_magnet_url()
    if not valid_youtube_url(url):
        await ctx.bot.send_message(chat_id, "Урок скоро появится здесь. А пока можно заглянуть в «Креативный мозг» 🧠",
                                   reply_markup=with_admin_menu(tg_user_id, next_keyboard("user:product", "Что внутри? →")))
        return
    text = (texts.WELCOME if welcome else texts.LEAD_CAPTION + "\n\n👇 Сам урок:") + "\n" + url
    label = "🧠 Что внутри «Креативного мозга»?"
    kwargs = {"chat_id": chat_id, "text": text,
              "reply_markup": with_admin_menu(tg_user_id, next_keyboard("user:product", label))}
    if LinkPreviewOptions is not None:
        kwargs["link_preview_options"] = LinkPreviewOptions(
            url=url, is_disabled=False, prefer_large_media=True, show_above_text=True)
    else:
        kwargs["disable_web_page_preview"] = False
    try:
        await ctx.bot.send_message(**kwargs)
    except BadRequest:
        if "link_preview_options" not in kwargs:
            raise
        kwargs.pop("link_preview_options")
        await ctx.bot.send_message(**kwargs, disable_web_page_preview=False)
    # Never store the lesson as an editable/deletable current screen.
    await db.clear_current_message(tg_user_id)
    if track:
        await db.record_event(tg_user_id, "lead_magnet_sent")


async def show_product(chat_id: int, tg_user_id: int, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await render_screen(chat_id, tg_user_id, ctx, text=texts.PRODUCT,
                        reply_markup=next_keyboard("user:buy", "Поехали →"))
    await db.record_event(tg_user_id, "product_opened")


async def buy_access(chat_id: int, tg_user_id: int, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not await db.has_accepted_offer(tg_user_id, OFFER_VERSION):
        await start_offer_again(chat_id, tg_user_id, ctx)
    else:
        await create_payment_and_render(chat_id, tg_user_id, CURRENT_TARIFF, ctx)


async def refresh_payment(row: dict) -> dict:
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.get(f"{YOOKASSA_API_URL}/{row['payment_id']}",
                                    auth=(YOOKASSA_SHOP_ID, YOOKASSA_SECRET_KEY))
        response.raise_for_status()
        payment = response.json()
    expected = json.loads(row["metadata_json"])
    metadata = payment.get("metadata") or {}
    amount = payment.get("amount") or {}
    if (payment.get("id") != row["payment_id"] or not expected.get("project_key")
            or metadata.get("project_key") != expected["project_key"]
            or str(metadata.get("user_id")) != str(row["tg_user_id"])
            or metadata.get("kind") != "channel_access"
            or metadata.get("tariff_code") != row["plan_id"]
            or metadata.get("days") != expected.get("days")
            or amount.get("currency") != "RUB"
            or Decimal(str(amount.get("value"))) != Decimal(str(row["amount_rub"]))):
        raise ValueError("Payment identity, metadata or amount mismatch")
    status = payment.get("status")
    if status not in {"pending", "waiting_for_capture", "succeeded", "canceled"}:
        raise ValueError("Unknown payment status")
    if status == "succeeded" and payment.get("paid") is not True:
        raise ValueError("Succeeded payment is not paid")
    await db.update_payment_status(row["payment_id"], status, payment)
    return payment


def admin_cancel_keyboard() -> Mk:
    return next_keyboard("admin:menu", "◀️ В админ-панель")


async def show_admin_lead_magnet(chat_id: int, tg_user_id: int, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if chat_id != tg_user_id or not is_admin(tg_user_id):
        return
    url = await db.get_lead_magnet_url()
    rows = [[Btn("🔗 Установить / заменить ссылку", callback_data="admin:lead_set")],
            [Btn("👀 Проверить как пользователь", callback_data="admin:lead_test")]]
    if url:
        rows.append([Btn("🗑 Удалить ссылку", callback_data="admin:lead_clear")])
    rows.append([Btn("◀️ В админ-панель", callback_data="admin:menu")])
    await render_screen(chat_id, tg_user_id, ctx, text=f"🎬 Лид-магнит\n\nТекущая YouTube-ссылка:\n{url or 'не задана'}",
                        reply_markup=Mk(rows))


async def followup_job(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not FOLLOWUP_ENABLED:
        return
    for tg_user_id in await db.followup_candidates():
        async with user_lock(tg_user_id):
            # A buyer may have paid at YooKassa without pressing Check in Telegram.
            try:
                for pending in await db.list_unfinalized_payments(tg_user_id):
                    await refresh_payment(pending)
            except (httpx.HTTPError, ValueError, TypeError, AttributeError, ArithmeticError):
                logger.warning("followup skipped: payment state unavailable user_id=%s", tg_user_id)
                continue
            if not await db.claim_followup(tg_user_id):
                continue
            try:
                await ctx.bot.send_message(tg_user_id, texts.FOLLOWUP,
                                           reply_markup=with_admin_menu(tg_user_id, next_keyboard("user:product", "Посмотреть «Креативный мозг»")))
                await db.mark_followup_sent(tg_user_id)
            except TelegramError:
                # An ambiguous timeout must not cause a second marketing message.
                logger.warning("followup delivery unconfirmed; no retry user_id=%s", tg_user_id)


async def channel_member_updated(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    event = update.chat_member
    if not event or event.chat.id != CHANNEL_ID or not event.invite_link:
        return
    if event.new_chat_member.status not in {"member", "administrator", "creator"}:
        return
    invite = await db.get_invite_link(event.invite_link.invite_link)
    if not invite:
        return
    target = event.new_chat_member.user.id
    async with user_lock(target):
        valid = target == invite["tg_user_id"] and active_subscription_until(await db.get_subscription(target))
        if not valid:
            await remove_user_from_channel(ctx, target, reason="invalid_invite")
        # member_limit=1 limits concurrent members; revocation enforces one entry.
        try:
            await ctx.bot.revoke_chat_invite_link(CHANNEL_ID, invite["invite_link"])
            await db.mark_invite_used(invite["invite_link"])
        except TelegramError:
            logger.warning("invite revocation failed user_id=%s", target)


async def error_handler(update: object, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    # No update/payment payloads or exception strings containing request credentials.
    logger.error("handler failed error_type=%s", type(ctx.error).__name__)
    if isinstance(update, Update) and update.effective_chat and update.effective_chat.type == "private":
        try:
            await ctx.bot.send_message(update.effective_chat.id, "Не удалось выполнить действие. Попробуй ещё раз чуть позже.",
                                       reply_markup=with_admin_menu(update.effective_chat.id))
        except TelegramError:
            pass


def build_application() -> Application:
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is required")
    app = Application.builder().token(BOT_TOKEN).post_init(init_storage).post_shutdown(close_storage).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("admin", admin))
    app.add_handler(CommandHandler(["set_offer", "set_lead_magnet", "clear_lead_magnet", "set_intro_video", "clear_intro_video", "grant", "revoke", "stats", "user_path"], admin_shortcut))
    app.add_handler(CallbackQueryHandler(router))
    app.add_handler(ChatMemberHandler(channel_member_updated, ChatMemberHandler.CHAT_MEMBER))
    app.add_error_handler(error_handler)
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, fallback_message))
    if app.job_queue is None:
        raise RuntimeError("JobQueue is unavailable. Install python-telegram-bot[job-queue].")
    app.job_queue.run_repeating(renewal_notices_job, interval=3600, first=60, name="renewal_notices")
    app.job_queue.run_repeating(expire_subscriptions_job, interval=3600, first=120, name="expire_subscriptions")
    app.job_queue.run_repeating(followup_job, interval=3600, first=180, name="lead_followup")
    return app


def main() -> None:
    app = build_application()
    logger.info("starting polling bot project_key=%s channel_id=%s", PROJECT_KEY, CHANNEL_ID)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
