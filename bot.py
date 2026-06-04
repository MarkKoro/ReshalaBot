#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Telegram-бот платного доступа к закрытому каналу."""

from __future__ import annotations

import logging
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

import httpx
from dotenv import load_dotenv
from telegram import InlineKeyboardButton as Btn
from telegram import InlineKeyboardMarkup as Mk
from telegram import InputMediaPhoto, InputMediaVideo, Update, User
from telegram.error import BadRequest, Forbidden, TelegramError
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

import db

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_IDS = {int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()}
YOOKASSA_SHOP_ID = os.getenv("YOOKASSA_SHOP_ID", "")
YOOKASSA_SECRET_KEY = os.getenv("YOOKASSA_SECRET_KEY", "")
PROJECT_KEY = os.getenv("PROJECT_KEY", "reshalo_channel_access")
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
    first_purchase_only: bool = False


TARIFFS: dict[str, Tariff] = {
    "m1_first": Tariff("m1_first", "1 месяц — первая покупка", 1000, 30, True),
    "m1": Tariff("m1", "1 месяц", 1500, 30),
    "m3": Tariff("m3", "3 месяца", 4000, 90),
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("reshalo_channel_bot")


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
) -> None:
    """Render one actual bot screen and keep its coordinates in users.current_* fields."""
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
    rows = [[Btn(f"{tariff.title} — {tariff.amount_rub} ₽", callback_data=f"user:tariff:{tariff.code}")] for tariff in tariffs]
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


async def available_tariffs(tg_user_id: int) -> list[Tariff]:
    had_successful_payment = await db.has_successful_payment(tg_user_id)
    return [tariff for tariff in TARIFFS.values() if not tariff.first_purchase_only or not had_successful_payment]


async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    chat = update.effective_chat
    if not user or not chat:
        return
    await remember_user(user)
    await delete_incoming(update, ctx)

    if await db.has_accepted_offer(user.id):
        await show_intro_or_reviews_or_tariffs(chat.id, user.id, ctx)
        return

    await render_screen(
        chat.id,
        user.id,
        ctx,
        text=(
            "Привет! Это бот доступа к закрытому Telegram-каналу.\n\n"
            "1. Откройте оферту.\n"
            "2. Нажмите «✅ Принимаю оферту».\n"
            "3. После этого откроются материалы и тарифы.\n\n"
            "До принятия оферты оплатить доступ нельзя."
        ),
        reply_markup=offer_keyboard(await db.get_offer_url()),
    )


async def show_intro_or_reviews_or_tariffs(chat_id: int, tg_user_id: int, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    intro_video_file_id = await db.get_intro_video_file_id()
    if intro_video_file_id:
        await render_screen(
            chat_id,
            tg_user_id,
            ctx,
            kind="video",
            file_id=intro_video_file_id,
            text="Короткое превью закрытого канала",
            reply_markup=next_keyboard("user:after_intro"),
        )
        return
    await show_first_review_or_tariffs(chat_id, tg_user_id, ctx)


async def show_first_review_or_tariffs(chat_id: int, tg_user_id: int, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    reviews = await db.list_review_photos()
    if reviews:
        await show_review(chat_id, tg_user_id, 0, ctx)
        return
    await show_tariffs(chat_id, tg_user_id, ctx)


async def show_review(chat_id: int, tg_user_id: int, offset: int, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    reviews = await db.list_review_photos()
    if not reviews or offset >= len(reviews):
        await show_tariffs(chat_id, tg_user_id, ctx)
        return
    offset = max(0, offset)
    callback = f"user:review:{offset + 1}" if offset < len(reviews) - 1 else "user:tariffs"
    await render_screen(
        chat_id,
        tg_user_id,
        ctx,
        kind="photo",
        file_id=reviews[offset]["file_id"],
        text=f"Отзыв о закрытом канале ({offset + 1}/{len(reviews)})",
        reply_markup=next_keyboard(callback),
    )


async def show_tariffs(chat_id: int, tg_user_id: int, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    sub = await db.get_subscription(tg_user_id)
    active_line = ""
    if sub and sub.get("status") == "active" and db.parse_dt(sub.get("access_until")) and db.parse_dt(sub.get("access_until")) > datetime.now(timezone.utc):
        active_line = f"\n\nВаш текущий доступ активен до {format_dt(sub.get('access_until'))}."
    await render_screen(
        chat_id,
        tg_user_id,
        ctx,
        text="Выберите срок доступа к закрытому каналу:" + active_line,
        reply_markup=tariffs_keyboard(await available_tariffs(tg_user_id)),
    )


async def start_offer_again(chat_id: int, tg_user_id: int, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await render_screen(chat_id, tg_user_id, ctx, text="Сначала нужно принять оферту, затем откроется оплата.", reply_markup=offer_keyboard(await db.get_offer_url()))


async def user_router(query, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    tg_user_id = query.from_user.id
    chat_id = query.message.chat_id
    await remember_user(query.from_user)
    data = query.data or ""

    if data == "user:accept_offer":
        await db.accept_offer(tg_user_id)
        await show_intro_or_reviews_or_tariffs(chat_id, tg_user_id, ctx)
        return
    if data == "user:after_intro":
        await show_first_review_or_tariffs(chat_id, tg_user_id, ctx)
        return
    if data.startswith("user:review:"):
        await show_review(chat_id, tg_user_id, int(data.rsplit(":", 1)[1]), ctx)
        return
    if data == "user:tariffs":
        if not await db.has_accepted_offer(tg_user_id):
            await start_offer_again(chat_id, tg_user_id, ctx)
            return
        await show_tariffs(chat_id, tg_user_id, ctx)
        return
    if data.startswith("user:tariff:"):
        if not await db.has_accepted_offer(tg_user_id):
            await start_offer_again(chat_id, tg_user_id, ctx)
            return
        tariff = TARIFFS.get(data.rsplit(":", 1)[1])
        if not tariff:
            await render_screen(chat_id, tg_user_id, ctx, text="Тариф не найден. Выберите тариф заново.", reply_markup=next_keyboard("user:tariffs", "К тарифам"))
            return
        if tariff.first_purchase_only and tariff not in await available_tariffs(tg_user_id):
            await render_screen(chat_id, tg_user_id, ctx, text="Тариф первой покупки уже недоступен.", reply_markup=next_keyboard("user:tariffs", "К тарифам"))
            return
        await create_payment_and_render(chat_id, tg_user_id, tariff, ctx)
        return
    if data.startswith("user:check_payment:"):
        await check_payment_and_grant(chat_id, tg_user_id, data.split(":", 2)[2], ctx)
        return


async def create_payment_and_render(chat_id: int, tg_user_id: int, tariff: Tariff, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not YOOKASSA_SHOP_ID or not YOOKASSA_SECRET_KEY:
        await render_screen(chat_id, tg_user_id, ctx, text="Оплата временно недоступна: YooKassa не настроена.", reply_markup=next_keyboard("user:tariffs", "К тарифам"))
        logger.error("payment creation blocked: empty YooKassa credentials")
        return

    return_url = YOOKASSA_RETURN_URL or (f"https://t.me/{BOT_USERNAME}" if BOT_USERNAME else "https://t.me/")
    metadata = {
        "project_key": PROJECT_KEY,
        "user_id": str(tg_user_id),
        "kind": "channel_access",
        "tariff_code": tariff.code,
        "days": str(tariff.days),
    }
    payload = {
        "amount": {"value": f"{tariff.amount_rub:.2f}", "currency": "RUB"},
        "capture": True,
        "confirmation": {"type": "redirect", "return_url": return_url},
        "description": f"Доступ к закрытому каналу: {tariff.title}",
        "metadata": metadata,
    }
    logger.info("creating YooKassa payment user_id=%s tariff=%s amount=%s", tg_user_id, tariff.code, tariff.amount_rub)
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
        logger.exception("failed to create YooKassa payment user_id=%s tariff=%s", tg_user_id, tariff.code)
        await render_screen(chat_id, tg_user_id, ctx, text="Не получилось создать платёж. Попробуйте позже.", reply_markup=next_keyboard("user:tariffs", "К тарифам"))
        return

    payment_id = payment.get("id")
    confirmation_url = payment.get("confirmation", {}).get("confirmation_url")
    if not payment_id or not confirmation_url:
        logger.error("YooKassa returned invalid payment response: %s", payment)
        await render_screen(chat_id, tg_user_id, ctx, text="YooKassa не вернула ссылку на оплату. Попробуйте позже.", reply_markup=next_keyboard("user:tariffs", "К тарифам"))
        return

    await db.create_payment(tg_user_id, payment_id, tariff.code, tariff.amount_rub, confirmation_url, metadata, payment)
    logger.info("created YooKassa payment payment_id=%s user_id=%s", payment_id, tg_user_id)
    await render_screen(
        chat_id,
        tg_user_id,
        ctx,
        text=f"Счёт создан: {tariff.title} за {tariff.amount_rub} ₽.\n\nНажмите «Оплатить», затем вернитесь в бот и нажмите «Проверить оплату».",
        reply_markup=payment_keyboard(payment_id, confirmation_url),
    )


async def check_payment_and_grant(chat_id: int, tg_user_id: int, payment_id: str, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    payment_row = await db.get_payment(payment_id)
    if not payment_row or int(payment_row["tg_user_id"]) != tg_user_id:
        await render_screen(chat_id, tg_user_id, ctx, text="Платёж не найден для вашего аккаунта. Создайте новый счёт.", reply_markup=next_keyboard("user:tariffs", "К тарифам"))
        return
    if payment_row.get("finalized_at"):
        await send_existing_access(chat_id, tg_user_id, payment_id, ctx)
        return

    logger.info("checking YooKassa payment payment_id=%s user_id=%s", payment_id, tg_user_id)
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.get(f"{YOOKASSA_API_URL}/{payment_id}", auth=(YOOKASSA_SHOP_ID, YOOKASSA_SECRET_KEY))
            response.raise_for_status()
            payment = response.json()
    except httpx.HTTPError:
        logger.exception("failed to check YooKassa payment payment_id=%s user_id=%s", payment_id, tg_user_id)
        await render_screen(chat_id, tg_user_id, ctx, text="Не получилось проверить платёж. Попробуйте ещё раз через минуту.", reply_markup=payment_keyboard(payment_id, payment_row.get("confirmation_url") or "https://t.me/"))
        return

    status = str(payment.get("status") or "")
    paid = bool(payment.get("paid"))
    await db.update_payment_status(payment_id, status, payment)
    logger.info("YooKassa payment state payment_id=%s status=%s paid=%s", payment_id, status, paid)
    if not (status == "succeeded" or paid):
        await render_screen(chat_id, tg_user_id, ctx, text="Оплата пока не подтверждена. Если вы уже оплатили, попробуйте чуть позже.", reply_markup=payment_keyboard(payment_id, payment_row.get("confirmation_url") or "https://t.me/"))
        return

    tariff = TARIFFS.get(payment_row["plan_id"])
    if not tariff:
        await render_screen(chat_id, tg_user_id, ctx, text="Неизвестный тариф платежа. Напишите администратору.")
        return
    result = await db.finalize_payment_once(payment_id, tg_user_id, PROJECT_KEY, "channel_access", tariff.code, tariff.days)
    if not result.get("ok"):
        logger.error("payment finalize failed payment_id=%s result=%s", payment_id, result)
        await render_screen(chat_id, tg_user_id, ctx, text="Платёж не прошёл внутреннюю проверку проекта. Напишите администратору.")
        return
    await send_access_link(chat_id, tg_user_id, payment_id, result["access_until"], ctx, already_finalized=result.get("already_finalized", False))


async def create_invite_link(tg_user_id: int, ctx: ContextTypes.DEFAULT_TYPE) -> str:
    invite = await ctx.bot.create_chat_invite_link(chat_id=CHANNEL_ID, name=f"access_{tg_user_id}_{int(datetime.now().timestamp())}", member_limit=1)
    logger.info("created invite link user_id=%s channel_id=%s", tg_user_id, CHANNEL_ID)
    return invite.invite_link


async def send_access_link(chat_id: int, tg_user_id: int, payment_id: str | None, access_until: str | None, ctx: ContextTypes.DEFAULT_TYPE, already_finalized: bool = False) -> None:
    invite_link = await create_invite_link(tg_user_id, ctx)
    await db.save_invite_link(tg_user_id, invite_link, payment_id=payment_id)
    prefix = "Платёж уже был обработан." if already_finalized else "✅ Оплата подтверждена."
    await render_screen(
        chat_id,
        tg_user_id,
        ctx,
        text=f"{prefix} Доступ активен до {format_dt(access_until)}.\n\nВаша одноразовая ссылка для входа в канал:\n{invite_link}",
        reply_markup=next_keyboard("user:tariffs", "К тарифам"),
    )


async def send_existing_access(chat_id: int, tg_user_id: int, payment_id: str | None, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    sub = await db.get_subscription(tg_user_id)
    if sub and sub.get("status") == "active":
        await send_access_link(chat_id, tg_user_id, payment_id, sub.get("access_until"), ctx, already_finalized=True)
        return
    await render_screen(chat_id, tg_user_id, ctx, text="Этот платёж уже обработан, но активный доступ не найден. Напишите администратору.", reply_markup=next_keyboard("user:tariffs", "К тарифам"))


async def expire_subscriptions_job(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    for sub in await db.list_expired_subscriptions(now_iso()):
        tg_user_id = int(sub["tg_user_id"])
        await remove_user_from_channel(ctx, tg_user_id, reason="expired")
        await db.mark_subscription_expired(tg_user_id)
        logger.info("expired subscription removed user_id=%s access_until=%s", tg_user_id, sub["access_until"])


async def remove_user_from_channel(ctx: ContextTypes.DEFAULT_TYPE, tg_user_id: int, reason: str) -> None:
    try:
        await ctx.bot.ban_chat_member(CHANNEL_ID, tg_user_id)
        await ctx.bot.unban_chat_member(CHANNEL_ID, tg_user_id, only_if_banned=True)
        logger.info("removed user from channel user_id=%s channel_id=%s reason=%s", tg_user_id, CHANNEL_ID, reason)
    except (BadRequest, Forbidden, TelegramError):
        logger.exception("failed to remove user from channel user_id=%s reason=%s", tg_user_id, reason)


# ── Админ-панель ─────────────────────────────────────

def is_admin(tg_user_id: int) -> bool:
    return tg_user_id in ADMIN_IDS


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
    user = update.effective_user
    chat = update.effective_chat
    if not user or not chat or not is_admin(user.id):
        await delete_incoming(update, ctx)
        return
    await remember_user(user)
    await delete_incoming(update, ctx)
    await show_admin_menu(chat.id, user.id, ctx)


async def show_admin_menu(chat_id: int, tg_user_id: int, ctx: ContextTypes.DEFAULT_TYPE, note: str = "") -> None:
    await db.clear_admin_state(tg_user_id)
    text = "Админ-панель\n\nВыберите действие кнопками ниже."
    if note:
        text = f"{note}\n\n{text}"
    await render_screen(chat_id, tg_user_id, ctx, text=text, reply_markup=admin_menu_keyboard())


async def admin_router(query, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    tg_user_id = query.from_user.id
    chat_id = query.message.chat_id
    if not is_admin(tg_user_id):
        return
    await remember_user(query.from_user)
    data = query.data or ""
    if data == "admin:menu":
        await show_admin_menu(chat_id, tg_user_id, ctx)
        return
    if data == "admin:user_start":
        await db.clear_admin_state(tg_user_id)
        await start_offer_again(chat_id, tg_user_id, ctx)
        return
    if data == "admin:offer":
        await db.set_admin_state(tg_user_id, "offer_url")
        await render_screen(chat_id, tg_user_id, ctx, text=f"Текущая оферта:\n{await db.get_offer_url() or 'не задана'}\n\nОтправьте новой строкой ссылку на оферту.", reply_markup=Mk([[Btn("◀️ Отмена", callback_data="admin:menu")]]))
        return
    if data == "admin:intro":
        await db.clear_admin_state(tg_user_id)
        await render_screen(chat_id, tg_user_id, ctx, text="Управление превью-видео.", reply_markup=admin_intro_keyboard(bool(await db.get_intro_video_file_id())))
        return
    if data == "admin:intro_set":
        await db.set_admin_state(tg_user_id, "intro_video")
        await render_screen(chat_id, tg_user_id, ctx, text="Отправьте следующим сообщением видео-файл для превью. Старое видео будет заменено.", reply_markup=Mk([[Btn("◀️ Отмена", callback_data="admin:intro")]]))
        return
    if data == "admin:intro_clear":
        await db.clear_intro_video_file_id()
        await render_screen(chat_id, tg_user_id, ctx, text="✅ Превью-видео удалено.", reply_markup=admin_intro_keyboard(False))
        return
    if data == "admin:reviews":
        await db.clear_admin_state(tg_user_id)
        reviews = await db.list_review_photos()
        await render_screen(chat_id, tg_user_id, ctx, text=f"Управление отзывами. Сейчас отзывов: {len(reviews)}.", reply_markup=admin_reviews_keyboard(bool(reviews)))
        return
    if data == "admin:review_add":
        await db.set_admin_state(tg_user_id, "review_photo")
        await render_screen(chat_id, tg_user_id, ctx, text="Отправьте следующим сообщением фото-отзыв.", reply_markup=Mk([[Btn("◀️ Отмена", callback_data="admin:reviews")]]))
        return
    if data == "admin:reviews_clear":
        await db.clear_review_photos()
        await render_screen(chat_id, tg_user_id, ctx, text="✅ Все отзывы удалены.", reply_markup=admin_reviews_keyboard(False))
        return
    if data.startswith("admin:review_show:"):
        await show_admin_review(chat_id, tg_user_id, int(data.rsplit(":", 1)[1]), ctx)
        return
    if data == "admin:grant":
        await db.set_admin_state(tg_user_id, "grant")
        await render_screen(chat_id, tg_user_id, ctx, text="Отправьте: <telegram_id> <days>\nНапример: 123456789 30", reply_markup=Mk([[Btn("◀️ Отмена", callback_data="admin:menu")]]))
        return
    if data == "admin:revoke":
        await db.set_admin_state(tg_user_id, "revoke")
        await render_screen(chat_id, tg_user_id, ctx, text="Отправьте telegram_id пользователя, у которого нужно отозвать доступ.", reply_markup=Mk([[Btn("◀️ Отмена", callback_data="admin:menu")]]))
        return
    if data == "admin:stats":
        await db.clear_admin_state(tg_user_id)
        await show_stats(chat_id, tg_user_id, ctx)
        return


async def show_admin_review(chat_id: int, tg_user_id: int, offset: int, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    reviews = await db.list_review_photos()
    if not reviews:
        await render_screen(chat_id, tg_user_id, ctx, text="Отзывы не добавлены.", reply_markup=admin_reviews_keyboard(False))
        return
    offset = max(0, min(offset, len(reviews) - 1))
    rows: list[list[Btn]] = []
    nav: list[Btn] = []
    if offset > 0:
        nav.append(Btn("⬅️", callback_data=f"admin:review_show:{offset - 1}"))
    if offset < len(reviews) - 1:
        nav.append(Btn("➡️", callback_data=f"admin:review_show:{offset + 1}"))
    if nav:
        rows.append(nav)
    rows.append([Btn("◀️ К отзывам", callback_data="admin:reviews")])
    row = reviews[offset]
    await render_screen(chat_id, tg_user_id, ctx, kind="photo", file_id=row["file_id"], text=f"Отзыв {offset + 1}/{len(reviews)}\nID: {row['id']}\nДобавлен: {format_dt(row['created_at'])}", reply_markup=Mk(rows))


async def show_stats(chat_id: int, tg_user_id: int, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    s = await db.stats()
    text = (
        "📊 Статистика\n\n"
        f"Пользователей: {s['users_count']}\n"
        f"Приняли оферту: {s['accepted_offer_count']}\n"
        f"Успешных оплат: {s['paid_count']}\n"
        f"Выручка: {s['revenue_rub']} ₽\n"
        f"Активных подписок: {s['active_subscriptions_count']}"
    )
    await render_screen(chat_id, tg_user_id, ctx, text=text, reply_markup=Mk([[Btn("◀️ В админ-панель", callback_data="admin:menu")]]))


async def handle_admin_input(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> bool:
    user = update.effective_user
    chat = update.effective_chat
    message = update.effective_message
    if not user or not chat or not message or not is_admin(user.id):
        return False
    state = await db.get_admin_state(user.id)
    if not state:
        return False
    state_name = state["state"]
    await remember_user(user)

    if state_name == "offer_url":
        if not message.text:
            await delete_incoming(update, ctx)
            await render_screen(chat.id, user.id, ctx, text="Нужно отправить ссылку текстом.", reply_markup=Mk([[Btn("◀️ Отмена", callback_data="admin:menu")]]))
            return True
        url = message.text.strip()
        await db.set_offer_url(url)
        await db.clear_admin_state(user.id)
        await delete_incoming(update, ctx)
        await show_admin_menu(chat.id, user.id, ctx, f"✅ Оферта обновлена:\n{url}")
        return True

    if state_name == "intro_video":
        if not message.video:
            await delete_incoming(update, ctx)
            await render_screen(chat.id, user.id, ctx, text="Нужно отправить именно видео-файл.", reply_markup=Mk([[Btn("◀️ Отмена", callback_data="admin:intro")]]))
            return True
        await db.set_intro_video_file_id(message.video.file_id)
        await db.clear_admin_state(user.id)
        await delete_incoming(update, ctx)
        await render_screen(chat.id, user.id, ctx, text="✅ Превью-видео сохранено.", reply_markup=admin_intro_keyboard(True))
        return True

    if state_name == "review_photo":
        if not message.photo:
            await delete_incoming(update, ctx)
            await render_screen(chat.id, user.id, ctx, text="Нужно отправить именно фото.", reply_markup=Mk([[Btn("◀️ Отмена", callback_data="admin:reviews")]]))
            return True
        await db.add_review_photo(message.photo[-1].file_id)
        await db.clear_admin_state(user.id)
        await delete_incoming(update, ctx)
        await render_screen(chat.id, user.id, ctx, text="✅ Фото-отзыв добавлен.", reply_markup=admin_reviews_keyboard(True))
        return True

    if state_name == "grant":
        raw = (message.text or "").split()
        await delete_incoming(update, ctx)
        if len(raw) != 2:
            await render_screen(chat.id, user.id, ctx, text="Нужно отправить два числа: <telegram_id> <days>.", reply_markup=Mk([[Btn("◀️ Отмена", callback_data="admin:menu")]]))
            return True
        try:
            target_user_id = int(raw[0])
            days = int(raw[1])
        except ValueError:
            await render_screen(chat.id, user.id, ctx, text="telegram_id и days должны быть числами.", reply_markup=Mk([[Btn("◀️ Отмена", callback_data="admin:menu")]]))
            return True
        await db.clear_admin_state(user.id)
        sub = await db.grant_access(target_user_id, days)
        try:
            invite_link = await create_invite_link(target_user_id, ctx)
            await db.save_invite_link(target_user_id, invite_link)
            await ctx.bot.send_message(target_user_id, f"✅ Администратор выдал доступ до {format_dt(sub['access_until'])}.\n{invite_link}")
        except TelegramError:
            logger.exception("failed to notify granted user_id=%s", target_user_id)
        await show_admin_menu(chat.id, user.id, ctx, f"✅ Доступ для {target_user_id} активен до {format_dt(sub['access_until'])}.")
        return True

    if state_name == "revoke":
        await delete_incoming(update, ctx)
        try:
            target_user_id = int((message.text or "").strip())
        except ValueError:
            await render_screen(chat.id, user.id, ctx, text="telegram_id должен быть числом.", reply_markup=Mk([[Btn("◀️ Отмена", callback_data="admin:menu")]]))
            return True
        await db.clear_admin_state(user.id)
        await db.revoke_access(target_user_id)
        await remove_user_from_channel(ctx, target_user_id, reason="revoke")
        await show_admin_menu(chat.id, user.id, ctx, f"✅ Доступ для {target_user_id} отозван.")
        return True

    return False


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


async def fallback_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if await handle_admin_input(update, ctx):
        return
    user = update.effective_user
    chat = update.effective_chat
    if not user or not chat:
        return
    await remember_user(user)
    await delete_incoming(update, ctx)
    await start_offer_again(chat.id, user.id, ctx)


async def admin_shortcut(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    chat = update.effective_chat
    if not user or not chat or not is_admin(user.id):
        await delete_incoming(update, ctx)
        return
    await remember_user(user)
    await delete_incoming(update, ctx)
    command = (update.effective_message.text or "").split()[0]
    if command == "/set_offer":
        await db.set_admin_state(user.id, "offer_url")
        await render_screen(chat.id, user.id, ctx, text="Отправьте новой строкой ссылку на оферту.", reply_markup=Mk([[Btn("◀️ Отмена", callback_data="admin:menu")]]))
    elif command == "/set_intro_video":
        await db.set_admin_state(user.id, "intro_video")
        await render_screen(chat.id, user.id, ctx, text="Отправьте следующим сообщением видео-файл для превью.", reply_markup=Mk([[Btn("◀️ Отмена", callback_data="admin:intro")]]))
    elif command == "/clear_intro_video":
        await db.clear_intro_video_file_id()
        await render_screen(chat.id, user.id, ctx, text="✅ Превью-видео удалено.", reply_markup=admin_intro_keyboard(False))
    elif command == "/add_review":
        await db.set_admin_state(user.id, "review_photo")
        await render_screen(chat.id, user.id, ctx, text="Отправьте следующим сообщением фото-отзыв.", reply_markup=Mk([[Btn("◀️ Отмена", callback_data="admin:reviews")]]))
    elif command == "/reviews":
        reviews = await db.list_review_photos()
        await render_screen(chat.id, user.id, ctx, text=f"Управление отзывами. Сейчас отзывов: {len(reviews)}.", reply_markup=admin_reviews_keyboard(bool(reviews)))
    elif command == "/clear_reviews":
        await db.clear_review_photos()
        await render_screen(chat.id, user.id, ctx, text="✅ Все отзывы удалены.", reply_markup=admin_reviews_keyboard(False))
    elif command == "/grant":
        await db.set_admin_state(user.id, "grant")
        await render_screen(chat.id, user.id, ctx, text="Отправьте: <telegram_id> <days>", reply_markup=Mk([[Btn("◀️ Отмена", callback_data="admin:menu")]]))
    elif command == "/revoke":
        await db.set_admin_state(user.id, "revoke")
        await render_screen(chat.id, user.id, ctx, text="Отправьте telegram_id пользователя.", reply_markup=Mk([[Btn("◀️ Отмена", callback_data="admin:menu")]]))
    elif command == "/stats":
        await show_stats(chat.id, user.id, ctx)
    else:
        await show_admin_menu(chat.id, user.id, ctx)


def build_application() -> Application:
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is required")
    app = Application.builder().token(BOT_TOKEN).post_init(init_storage).post_shutdown(close_storage).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("admin", admin))
    app.add_handler(CommandHandler(["set_offer", "set_intro_video", "clear_intro_video", "add_review", "reviews", "clear_reviews", "grant", "revoke", "stats"], admin_shortcut))
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
