#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Telegram-бот платного доступа к закрытому каналу."""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from contextvars import ContextVar
from functools import wraps
from types import SimpleNamespace
from urllib.parse import urlsplit, parse_qs
import os
import re
import uuid
from dataclasses import dataclass
from string import Formatter
from datetime import datetime, timedelta, timezone
from typing import Literal

import httpx
from dotenv import load_dotenv
from telegram import InlineKeyboardButton as Btn
from telegram import InlineKeyboardMarkup as Mk
from telegram import InputMediaPhoto, InputMediaVideo, Update, User
from telegram.error import BadRequest, TelegramError
from telegram.ext import Application, CallbackQueryHandler, ChatMemberHandler, ChatJoinRequestHandler, CommandHandler, ContextTypes, MessageHandler, filters

import db
import texts
import billing

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
_callback_query: ContextVar = ContextVar("callback_query", default=None)
_command_screen: ContextVar = ContextVar("command_screen", default=None)


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
    for key, value in {'followup_enabled': str(int(FOLLOWUP_ENABLED)), 'auto_renew_enabled': '0',
                       'reminder_days': '3', 'activity_chat_id': os.getenv('ACTIVITY_CHAT_ID','0')}.items():
        if await db.get_setting(key) is None:
            await db.set_setting(key,value)
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


async def safe_delete_message(ctx: ContextTypes.DEFAULT_TYPE, chat_id: int, message_id: int) -> bool:
    if not chat_id or not message_id:
        return True
    try:
        await ctx.bot.delete_message(chat_id=chat_id, message_id=message_id)
        return True
    except TelegramError:
        logger.debug("message delete skipped chat_id=%s message_id=%s", chat_id, message_id, exc_info=True)
        return False


async def delete_incoming(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat and update.effective_message:
        await safe_delete_message(ctx, update.effective_chat.id, update.effective_message.message_id)


def serialized_command(handler):
    @wraps(handler)
    async def wrapped(update, ctx):
        if not update.effective_user:
            return
        state = {'user_id': update.effective_user.id,
                 'chat_id': update.effective_chat.id if update.effective_chat else None,
                 'fresh': True}
        token = _command_screen.set(state)
        try:
            async with user_lock(update.effective_user.id):
                result = await handler(update, ctx)
                if not state['fresh']:
                    await delete_incoming(update, ctx)
                return result
        finally:
            _command_screen.reset(token)
    return wrapped


def navigation_keyboard(reply_markup: Mk | None, back_callback: str) -> Mk:
    rows = [list(row) for row in reply_markup.inline_keyboard] if reply_markup else []
    if not any('назад' in button.text.lower() or 'админ-панель' in button.text.lower()
               or button.text in {'Отмена', 'Все тарифы'} for row in rows for button in row):
        rows.append([Btn('◀️ Назад', callback_data=back_callback)])
    return Mk(rows)


async def is_lesson_message(message, tg_user_id: int) -> bool:
    lesson = await db.get_lesson_message(tg_user_id) or {}
    if (lesson.get('lesson_chat_id'), lesson.get('lesson_message_id')) == (message.chat_id, message.message_id):
        return True
    if getattr(message, 'video', None) or getattr(message, 'photo', None):
        return True
    # Recognize lessons sent before lesson coordinates were stored, including old URLs.
    text = getattr(message, 'text', None) or getattr(message, 'caption', None) or ''
    caption = await content('LEAD_CAPTION')
    url = text.rsplit('\n\n', 1)[-1]
    markup = getattr(message, 'reply_markup', None)
    if markup and any(button.url == url for row in markup.inline_keyboard for button in row):
        # The lesson navigation screen links to the video; it is still an editable menu.
        return False
    preview = getattr(message, 'link_preview_options', None)
    if preview and preview.is_disabled:
        return False
    return text.startswith((caption + '\n\n', texts.LEAD_CAPTION + '\n\n')) and valid_http_url(url)


async def retire_menu(chat_id: int, message_id: int, tg_user_id: int, ctx) -> None:
    lesson = await db.get_lesson_message(tg_user_id) or {}
    if (chat_id, message_id) == (lesson.get('lesson_chat_id'), lesson.get('lesson_message_id')):
        return
    if not await safe_delete_message(ctx, chat_id, message_id):
        try:
            await ctx.bot.edit_message_reply_markup(chat_id=chat_id, message_id=message_id, reply_markup=None)
        except TelegramError:
            logger.debug('old menu retirement unavailable chat_id=%s message_id=%s', chat_id, message_id)


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
    back_callback: str | None = None,
    force_new: bool = False,
) -> None:
    """Edit the pressed menu (or the saved menu for commands/jobs), preserving the lesson."""
    if back_callback is None:
        admin_screen = reply_markup and any((button.callback_data or '').startswith('admin:')
                                           for row in reply_markup.inline_keyboard for button in row)
        back_callback = 'admin:menu' if admin_screen else 'user:home'
    reply_markup = navigation_keyboard(reply_markup, back_callback)
    if include_admin_shortcut:
        reply_markup = with_admin_menu(tg_user_id, reply_markup)
    current = await db.get_current_message(tg_user_id) or {}
    old_chat_id = int(current.get("current_chat_id") or 0)
    old_message_id = int(current.get("current_message_id") or 0)
    old_kind = str(current.get("current_message_kind") or "")
    target_id, target_kind = (old_message_id, old_kind) if old_chat_id == chat_id else (0, '')
    protected_source_id = 0
    query = _callback_query.get()
    message = query.message if query and query.from_user.id == tg_user_id else None
    command = _command_screen.get()
    own_command = command and command['user_id'] == tg_user_id and command['chat_id'] == chat_id
    if own_command and command['fresh'] and not message:
        # An edited old message can remain hidden after the user clears chat history.
        force_new = True
    if message and message.chat_id == chat_id and getattr(message, 'message_id', None):
        if await is_lesson_message(message, tg_user_id):
            protected_source_id = message.message_id
            lesson = await db.get_lesson_message(tg_user_id) or {}
            legacy_text = getattr(message, 'text', None) or ''
            legacy_url = legacy_text.rsplit('\n\n', 1)[-1]
            if not lesson.get('lesson_message_id') and valid_http_url(legacy_url):
                await db.set_lesson_message(tg_user_id, chat_id, message.message_id, legacy_url)
            # The first menu belongs below the preserved video, never in the greeting above it.
            if target_id <= message.message_id:
                target_id = 0
        else:
            target_id = message.message_id
            target_kind = 'text'
    if force_new:
        target_id = 0
    lesson = await db.get_lesson_message(tg_user_id) or {}
    if (chat_id, target_id) == (lesson.get('lesson_chat_id'), lesson.get('lesson_message_id')):
        target_id = 0
    if not message and lesson.get('lesson_chat_id') == chat_id and target_id < lesson.get('lesson_message_id', 0):
        target_id = 0

    async def remember_screen(message_id):
        await db.set_current_message(tg_user_id, chat_id, message_id, kind)
        if own_command:
            command['fresh'] = False
        obsolete = {(old_chat_id, old_message_id), (chat_id, target_id)}
        obsolete.discard((chat_id, message_id))
        obsolete.discard((chat_id, protected_source_id))
        for previous_chat, previous_id in obsolete:
            if previous_chat and previous_id:
                await retire_menu(previous_chat, previous_id, tg_user_id, ctx)

    if target_id and target_kind == kind:
        try:
            if kind == "text":
                await ctx.bot.edit_message_text(chat_id=chat_id, message_id=target_id, text=text,
                                                reply_markup=reply_markup, disable_web_page_preview=True)
            elif kind == "video":
                await ctx.bot.edit_message_media(
                    chat_id=chat_id,
                    message_id=target_id,
                    media=InputMediaVideo(media=file_id, caption=text),
                    reply_markup=reply_markup,
                )
            else:
                await ctx.bot.edit_message_media(
                    chat_id=chat_id,
                    message_id=target_id,
                    media=InputMediaPhoto(media=file_id, caption=text),
                    reply_markup=reply_markup,
                )
        except BadRequest as exc:
            if "message is not modified" in str(exc).lower():
                await remember_screen(target_id)
                return
            logger.info("screen edit failed, recreating message: %s", exc)
        else:
            await remember_screen(target_id)
            return
        # Transport failures have an unknown outcome. Do not clone the menu on a timeout.

    if kind == "video":
        msg = await ctx.bot.send_video(chat_id=chat_id, video=file_id, caption=text, reply_markup=reply_markup)
    elif kind == "photo":
        msg = await ctx.bot.send_photo(chat_id=chat_id, photo=file_id, caption=text, reply_markup=reply_markup)
    else:
        msg = await ctx.bot.send_message(chat_id=chat_id, text=text, reply_markup=reply_markup,
                                         disable_web_page_preview=True)
    await remember_screen(msg.message_id)


def offer_keyboard(offer_url: str | None) -> Mk:
    if not valid_http_url(offer_url):
        return next_keyboard("user:product", "🧠 Креативный мозг")
    return Mk([
        [Btn("📄 Открыть оферту", url=offer_url)],
        [Btn("✅ Принимаю, идём дальше", callback_data="user:accept_offer_and_pay")],
    ])


def next_keyboard(callback_data: str, label: str = "Далее ➡️") -> Mk:
    return Mk([[Btn(label, callback_data=callback_data)]])


def payment_keyboard(payment_id: str, confirmation_url: str | None, retry: bool = False, amount_rub: int = 400) -> Mk:
    check = Btn("✅ Проверить ещё раз" if retry else "✅ Проверить оплату",
                callback_data=f"user:check_payment:{payment_id}")
    if retry:
        rows = [[check]]
        if confirmation_url:
            rows.append([Btn("💳 Открыть оплату", url=confirmation_url)])
    else:
        rows = ([[Btn(f"💳 Оплатить {amount_rub} ₽", url=confirmation_url)]] if valid_http_url(confirmation_url) else []) + [[check]]
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
        await send_lead_magnet(chat_id,tg_user_id,ctx)
        return
    contract = await db.get_renewal(tg_user_id)
    auto = bool(contract and contract['enabled'])
    text = await content('ACTIVE_ACCESS',date=format_dt(access_until))
    text += ('\n\nАвтопродление: включено.' if auto and await auto_available() else
             '\n\nАвтопродление приостановлено администратором.' if auto else '\n\nАвтопродление: выключено.')
    rows = [[Btn('🧠 Получить ссылку входа',callback_data='user:access')],
            [Btn('Продлить доступ →',callback_data='user:buy')]]
    if auto or await auto_available():
        rows.append([Btn('Отключить автопродление' if auto else 'Настроить автопродление',callback_data='user:auto_off' if auto else 'user:auto_setup')])
    rows.append([Btn('Моя активность',callback_data='user:activity')])
    await render_screen(chat_id,tg_user_id,ctx,text=text,reply_markup=Mk(rows),back_callback='user:product')


@serialized_command
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
        await send_lead_magnet(chat.id, user.id, ctx, refresh_lesson=True)


async def start_offer_again(chat_id: int, tg_user_id: int, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    offer_url = await db.get_offer_url()
    tariff = await selected_tariff(ctx)
    if not tariff:
        await show_tariffs(chat_id,tg_user_id,ctx)
        return
    if not valid_http_url(offer_url):
        await render_screen(chat_id,tg_user_id,ctx,text='Условия оплаты пока не настроены. Можно вернуться к бесплатному уроку.',reply_markup=next_keyboard('user:lead_magnet','Открыть урок'))
        return
    ctx.user_data['offered_terms'] = (offer_url,OFFER_VERSION,tariff.code,tariff.amount_rub,tariff.days)
    if not await auto_available() or tariff.days < 3:
        await create_payment_and_render(chat_id,tg_user_id,tariff,ctx,direct_offer=(offer_url,OFFER_VERSION))
        return
    text = await content('OFFER')
    text += f'\n\n{tariff.title}\n{tariff.amount_rub} ₽ · {tariff.days} дней.'
    rows = [[Btn('📄 Условия',url=offer_url)]]
    rows.append([Btn('Принимаю · оплатить разово',callback_data='user:accept_offer_and_pay')])
    if await auto_available() and tariff.days >= 3:
        text += f'\n\nМожно выбрать автопродление: {tariff.amount_rub} ₽ за каждые {tariff.days} дней, списание за сутки до окончания. Перед списанием предупредим. Отключить можно здесь в любой момент. Если способ оплаты не сохранится, останется разовая покупка.'
        rows.append([Btn('Принимаю · с автопродлением',callback_data='user:accept_offer_auto')])
    await render_screen(chat_id,tg_user_id,ctx,text=text,reply_markup=Mk(rows),back_callback='user:product')


async def user_router(query, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    tg_user_id, chat_id = query.from_user.id, query.message.chat_id
    await remember_user(query.from_user)
    data = query.data or ''
    if data == 'user:welcome':
        await render_screen(chat_id,tg_user_id,ctx,text=await content('WELCOME'),
                            reply_markup=next_keyboard('user:lead_magnet','▶️ Бесплатный урок'))
    elif data == 'user:home':
        if active_subscription_until(await db.get_subscription(tg_user_id)):
            await show_active_access(chat_id,tg_user_id,ctx)
        else:
            await show_product(chat_id,tg_user_id,ctx)
    elif data in {'user:lead_magnet','user:start_intro','user:lesson'}:
        await send_lead_magnet(chat_id,tg_user_id,ctx,welcome=False)
    elif data=='user:access':
        sub=await db.get_subscription(tg_user_id)
        await send_existing_access(chat_id,tg_user_id,(sub or {}).get('last_payment_id'),ctx)
    elif data=='user:activity':
        await show_user_activity(chat_id,tg_user_id,ctx)
    elif data=='user:quiet':
        await db.set_marketing_opt_out(tg_user_id,True)
        await render_screen(chat_id,tg_user_id,ctx,text='Напоминания о бесплатном уроке отключены. Сообщения об оплате и сроке доступа останутся.',reply_markup=next_keyboard('user:product','Посмотреть, что внутри'),back_callback='user:lesson')
    elif data=='user:auto_off':
        await disable_auto_and_render(chat_id,tg_user_id,ctx)
    elif data=='user:status':
        await show_active_access(chat_id,tg_user_id,ctx)
    elif data=='user:tariffs':
        await buy_access(chat_id,tg_user_id,ctx)
    elif data.startswith('user:choose:'):
        ctx.user_data['selected_tariff']=data.split(':',2)[2]
        tariff=await selected_tariff(ctx)
        if not tariff:
            await show_tariffs(chat_id,tg_user_id,ctx)
            return
        await db.record_event(tg_user_id,'tariff_selected',{'code':tariff.code})
        await db.record_event(tg_user_id,'buy_clicked')
        await start_offer_again(chat_id,tg_user_id,ctx)
    elif data in {'user:buy','user:auto_setup'}:
        await db.record_event(tg_user_id,'buy_clicked')
        if active_subscription_until(await db.get_subscription(tg_user_id)):
            await db.record_event(tg_user_id,'renewal_started')
        await buy_access(chat_id,tg_user_id,ctx)
    elif data in {'user:accept_offer_and_pay','user:accept_offer_auto'}:
        tariff=await selected_tariff(ctx)
        terms=(await db.get_offer_url(),OFFER_VERSION,tariff.code,tariff.amount_rub,tariff.days) if tariff else None
        if not terms or not valid_http_url(terms[0]) or ctx.user_data.get('offered_terms')!=terms:
            await start_offer_again(chat_id,tg_user_id,ctx)
            return
        auto=data.endswith('_auto')
        if auto and (not await auto_available() or tariff.days < 3):
            await start_offer_again(chat_id,tg_user_id,ctx)
            return
        if not await db.has_accepted_offer(tg_user_id,OFFER_VERSION):
            await db.accept_offer(tg_user_id,OFFER_VERSION)
            await db.record_event(tg_user_id,'offer_accepted',{'version':OFFER_VERSION})
        await create_payment_and_render(chat_id,tg_user_id,tariff,ctx,auto_opt_in=auto)
    elif data=='user:accept_offer':
        await start_offer_again(chat_id,tg_user_id,ctx)
    elif data.startswith('user:check_payment:'):
        await check_payment_and_grant(chat_id,tg_user_id,data.split(':',2)[2],ctx)
    else:
        await show_product(chat_id,tg_user_id,ctx)


async def create_payment_and_render(chat_id: int, tg_user_id: int, tariff: Tariff, ctx: ContextTypes.DEFAULT_TYPE, *, auto_opt_in=False, direct_offer=None) -> None:
    published={r['code']:r for r in await db.list_tariffs()}
    current=published.get(tariff.code)
    if not current or (current['amount_rub'],current['days'])!=(tariff.amount_rub,tariff.days):
        await show_tariffs(chat_id,tg_user_id,ctx)
        return
    if not direct_offer and not await db.has_accepted_offer(tg_user_id,OFFER_VERSION):
        await start_offer_again(chat_id,tg_user_id,ctx)
        return
    back=next_keyboard('user:buy','Попробовать ещё раз')
    if not YOOKASSA_SHOP_ID or not YOOKASSA_SECRET_KEY:
        await render_screen(chat_id,tg_user_id,ctx,text='Оплата временно недоступна. Попробуй позже или открой помощь через /paysupport.',reply_markup=back)
        return
    order=await db.pending_checkout(tg_user_id)
    # Older installations could leave several invoices. Every unresolved invoice
    # must settle before a new one is created.
    for pending in await db.list_unfinalized_payments(tg_user_id):
        try:
            remote=await refresh_payment(pending)
            if remote['status']=='succeeded':
                result=await billing.finalize(pending)
                if result.get('ok'):
                    await send_access_link(chat_id,tg_user_id,pending['payment_id'],result['access_until'],ctx)
                    return
            elif remote['status']!='canceled':
                await render_payment(chat_id,tg_user_id,pending,ctx)
                return
            order=await db.pending_checkout(tg_user_id)
        except (httpx.HTTPError,ValueError,TypeError,AttributeError,ArithmeticError):
            await render_payment(chat_id,tg_user_id,pending,ctx)
            return
    if not order:
        payload=payment_payload(tg_user_id,tariff,auto_opt_in=auto_opt_in)
        order=await db.prepare_checkout(tg_user_id,tariff,payload,auto_opt_in=auto_opt_in,direct_offer=direct_offer)
    try:
        row=await billing.submit_checkout(order,YOOKASSA_SHOP_ID,YOOKASSA_SECRET_KEY)
    except billing.PaymentRejected:
        if order['auto_opt_in']:
            # A definitive API rejection created no payment. Offer a new manual quote.
            await db.disable_renewal(tg_user_id,'provider_rejected_setup')
            await render_screen(chat_id,tg_user_id,ctx,text='ЮKassa отклонила создание счёта с автопродлением. Можно попробовать разовую оплату; перед окончанием пришлю напоминание.',reply_markup=next_keyboard('user:accept_offer_and_pay','Оформить разовую оплату'))
        else:
            await render_screen(chat_id,tg_user_id,ctx,text='ЮKassa отклонила создание счёта. Попробуй позже или открой /paysupport.',reply_markup=back)
        return
    except (httpx.HTTPError,ValueError,TypeError,AttributeError,ArithmeticError):
        logger.warning('checkout outcome unresolved user_id=%s order=%s',tg_user_id,order['id'])
        await render_screen(chat_id,tg_user_id,ctx,text='Проверяю результат запроса к ЮKassa. Повторно оплачивать не нужно. Я продолжу проверку автоматически; повторная кнопка использует тот же счёт.',reply_markup=back)
        return
    if row['status']=='succeeded':
        result=await billing.finalize(row)
        if result.get('ok'):
            await send_access_link(chat_id,tg_user_id,row['payment_id'],result['access_until'],ctx)
            return
    await render_payment(chat_id,tg_user_id,row,ctx)


async def check_payment_and_grant(chat_id: int, tg_user_id: int, payment_id: str, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    row=await db.get_payment(payment_id)
    if not row or row['tg_user_id']!=tg_user_id:
        await render_screen(chat_id,tg_user_id,ctx,text='Платёж не найден для твоего аккаунта.',reply_markup=next_keyboard('user:product','Посмотреть, что внутри'))
        return
    if row['finalized_at']:
        await send_existing_access(chat_id,tg_user_id,payment_id,ctx)
        return
    try:
        payment=await refresh_payment(row)
        if payment['status']=='succeeded':
            result=await billing.finalize(row)
            if not result.get('ok'):
                raise ValueError('Finalization failed')
            await send_access_link(chat_id,tg_user_id,payment_id,result['access_until'],ctx)
        elif payment['status']=='canceled':
            await handle_auto_failure(row,ctx)
            await render_screen(chat_id,tg_user_id,ctx,text='Этот платёж отменён. Можно выбрать способ оплаты заново.',reply_markup=next_keyboard('user:buy','К оплате →'))
        else:
            await render_screen(chat_id,tg_user_id,ctx,text=texts.PAYMENT_PENDING,reply_markup=payment_keyboard(payment_id,row['confirmation_url'],retry=True,amount_rub=row['amount_rub']))
    except (httpx.HTTPError,ValueError,TypeError,AttributeError,ArithmeticError):
        logger.warning('payment check deferred payment_id=%s',payment_id)
        await render_screen(chat_id,tg_user_id,ctx,text='Подтверждение пока недоступно. Я продолжу проверку; повторно оплачивать не нужно.',reply_markup=payment_keyboard(payment_id,row['confirmation_url'],retry=True,amount_rub=row['amount_rub']))


async def create_invite_link(tg_user_id: int, ctx: ContextTypes.DEFAULT_TYPE) -> tuple[str, str]:
    expire_at = datetime.now(timezone.utc) + timedelta(hours=1)
    invite = await ctx.bot.create_chat_invite_link(
        chat_id=CHANNEL_ID,
        name=f"access_{tg_user_id}_{int(datetime.now().timestamp())}",
        creates_join_request=True,
        expire_date=expire_at,
    )
    logger.info("created invite link user_id=%s channel_id=%s expire_at=%s", tg_user_id, CHANNEL_ID, expire_at.isoformat())
    return invite.invite_link, expire_at.isoformat()


async def send_access_link(chat_id: int, tg_user_id: int, payment_id: str | None, access_until: str | None,
                           ctx: ContextTypes.DEFAULT_TYPE, already_finalized: bool = False, *, force_new: bool = False) -> None:
    existing = await db.get_invite_link_by_payment(payment_id) if payment_id else None
    try:
        if existing and existing.get('requires_approval') and not existing["used"] and parse_dt(existing["expire_at"]) and parse_dt(existing["expire_at"]) > datetime.now(timezone.utc):
            invite_link = existing["invite_link"]
        else:
            # Retire still-valid links before replacing one; never extend access here.
            for row in await db.list_open_invites(tg_user_id):
                expires=parse_dt(row["expire_at"])
                if not expires or expires>db.utcnow():
                    await ctx.bot.revoke_chat_invite_link(CHANNEL_ID, row["invite_link"])
                await db.mark_invite_used(row["invite_link"])
            await ctx.bot.unban_chat_member(CHANNEL_ID, tg_user_id, only_if_banned=True)
            invite_link, expire_at = await create_invite_link(tg_user_id, ctx)
            await db.save_invite_link(tg_user_id, invite_link, payment_id=payment_id, expire_at=expire_at)
        paid = await db.get_payment(payment_id) if payment_id else None
        message = await content('PAYMENT_SUCCEEDED',date=format_dt(access_until)) if paid else f'Доступ открыт до {format_dt(access_until)}. Нажми кнопку и отправь заявку — бот подтвердит вход для твоего аккаунта.'
        contract = await db.get_renewal(tg_user_id)
        if contract and contract['enabled'] and await auto_available():
            message += f"\n\nАвтопродление включено: {contract['amount_rub']} ₽ / {contract['days']} дней. Отключение — /cancel_subscription."
        elif contract and contract.get('disabled_reason')=='method_not_saved':
            message += '\n\nСпособ оплаты не сохранился: автопродление не включено. Перед окончанием пришлю напоминание.'
        await render_screen(chat_id, tg_user_id, ctx,
                            text=message,
                            reply_markup=Mk([[Btn("🧠 Войти в «Креативный мозг»", url=invite_link)]]),
                            back_callback='user:status',force_new=force_new)
        if paid:
            await db.mark_access_notified(payment_id)
    except TelegramError as exc:
        logger.error("invite delivery failed user_id=%s channel_id=%s error_type=%s", tg_user_id, CHANNEL_ID, type(exc).__name__)
        await render_screen(chat_id, tg_user_id, ctx,
                                text=f"Доступ сохранён до {format_dt(access_until)}. Не удалось выдать ссылку. Повтори получение ссылки или открой /paysupport.",
                            reply_markup=next_keyboard(f"user:check_payment:{payment_id}", "✅ Повторить выдачу ссылки") if payment_id else None)


async def send_existing_access(chat_id: int, tg_user_id: int, payment_id: str | None, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    access_until = active_subscription_until(await db.get_subscription(tg_user_id))
    if access_until:
        await send_access_link(chat_id, tg_user_id, payment_id, access_until, ctx, already_finalized=True)
        return
    await render_screen(chat_id, tg_user_id, ctx, text="Этот платёж уже обработан. Текущий доступ закончился или был отозван.",
                        reply_markup=next_keyboard("user:buy", "Вернуться в канал →"))


async def send_subscription_renewal_notice(tg_user_id: int, access_until: str | None, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    stats=await db.activity_summary(tg_user_id)
    activity=activity_phrase(stats)
    if stats['messages']:
        activity='За последние 30 дней в нашей группе:\n'+activity+'\nСпасибо, что участвуешь!'
    contract=await db.get_renewal(tg_user_id)
    if contract and contract['enabled'] and await auto_available():
        due=parse_dt(access_until)-timedelta(days=1)
        text=f"Доступ к «Креативному мозгу» действует до {format_dt(access_until)}.\n\n{activity}\n\nАвтопродление: {contract['amount_rub']} ₽ за {contract['days']} дней. Списание запланировано на {format_dt(due.isoformat())}, но не раньше чем через 12 часов после этого предупреждения. При отказе пришлю кнопку ручной оплаты."
        rows=[[Btn('Отключить автопродление',callback_data='user:auto_off')],[Btn('Мой доступ',callback_data='user:status')]]
    else:
        text=await content('RENEWAL_NOTICE',date=format_dt(access_until),activity=activity)
        rows=[[Btn('Продолжить с нами →',callback_data='user:buy')]]
    await render_screen(tg_user_id,tg_user_id,ctx,text=text,reply_markup=Mk(rows),force_new=True)
    if contract and contract['enabled'] and await auto_available():
        await db.mark_auto_warning(tg_user_id,access_until)


async def send_subscription_expired_notice(tg_user_id: int, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await render_screen(tg_user_id,tg_user_id,ctx,text=await content('EXPIRED'),
                        reply_markup=next_keyboard('user:buy','Вернуться в канал →'),force_new=True)


async def renewal_notices_job(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    days=int(await db.get_setting('reminder_days','3'))
    for sub in await db.list_subscriptions_due_for_renewal_notice(now_iso(),days_before=days):
        uid=sub['tg_user_id']
        async with user_lock(uid):
            current=await db.get_subscription(uid)
            until=active_subscription_until(current)
            if not until or current['renewal_notice_sent_at'] or parse_dt(until)>db.utcnow()+timedelta(days=days):
                continue
            key='renewal:'+str(uid)+':'+until
            if not await db.claim_notification(key,uid):
                continue
            try:
                await send_subscription_renewal_notice(uid,until,ctx)
                await db.mark_renewal_notice_sent(uid)
                await db.mark_notification_sent(key)
            except TelegramError:
                # An uncertain warning must not authorize an unseen automatic charge.
                logger.warning('renewal warning unconfirmed user_id=%s',uid)


async def expire_subscriptions_job(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    for sub in await db.list_expired_subscriptions(now_iso()):
        uid=sub['tg_user_id']
        async with user_lock(uid):
            current=await db.get_subscription(uid)
            if not current or current['status']!='active' or active_subscription_until(current):
                continue
            if await db.list_unfinalized_payments(uid) or await db.pending_checkout(uid):
                known=await reconcile_user(uid,ctx)
                current=await db.get_subscription(uid)
                if active_subscription_until(current):
                    continue
                if not known and parse_dt(current['access_until'])>db.utcnow()-timedelta(hours=24):
                    continue  # Bounded grace while the already initiated payment is unresolved.
            if not await remove_user_from_channel(ctx,uid,reason='expired'):
                continue
            await db.mark_subscription_expired(uid)
            await db.disable_renewal(uid,'expired')
            await db.record_event(uid,'expired',{'access_until':current['access_until']},event_key=f"expired:{uid}:{current['access_until']}")
            try:
                await send_subscription_expired_notice(uid,ctx)
            except TelegramError:
                logger.warning('expiration notification failed user_id=%s',uid)


async def remove_user_from_channel(ctx: ContextTypes.DEFAULT_TYPE, tg_user_id: int, reason: str) -> bool:
    if is_admin(tg_user_id):
        logger.warning('configured administrator removal blocked user_id=%s',tg_user_id)
        return False
    links_ok=True
    for row in await db.list_open_invites(tg_user_id):
        try:
            expires=parse_dt(row['expire_at'])
            if not expires or expires>db.utcnow():
                await ctx.bot.revoke_chat_invite_link(CHANNEL_ID,row['invite_link'])
            await db.mark_invite_used(row['invite_link'])
        except TelegramError:
            links_ok=False
            logger.warning('invite revocation deferred user_id=%s',tg_user_id)
    try:
        await ctx.bot.ban_chat_member(CHANNEL_ID,tg_user_id)
        # Keep the ban until a verified new purchase/grant; otherwise old links allow re-entry.
        return links_ok
    except TelegramError:
        logger.warning('channel removal deferred user_id=%s reason=%s',tg_user_id,reason)
        return False


# ── Админ-панель ─────────────────────────────────────

def is_admin(tg_user_id: int) -> bool:
    return tg_user_id in ADMIN_IDS


def admin_menu_keyboard() -> Mk:
    return Mk([
        [Btn("🎬 Лид-магнит", callback_data="admin:lead_magnet")],
        [Btn("💰 Тарифы и сроки", callback_data="admin:tariffs")],
        [Btn("✍️ Тексты бота", callback_data="admin:texts")],
        [Btn("⚙️ Напоминания и настройки", callback_data="admin:settings")],
        [Btn("📄 Оферта", callback_data="admin:offer")],
        [Btn("🎁 Выдать доступ", callback_data="admin:grant")],
        [Btn("🚫 Отозвать доступ", callback_data="admin:revoke")],
        [Btn("📊 Воронка / статистика", callback_data="admin:stats")],
        [Btn("👤 Посмотреть путь пользователя", callback_data="admin:user_path")],
        [Btn("🔎 Проверить подключение", callback_data="admin:diagnostics")],
    ])


@serialized_command
async def admin(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    chat = update.effective_chat
    if not user or not chat or chat.type != "private" or not is_admin(user.id):
        return
    await remember_user(user)
    await show_admin_menu(chat.id, user.id, ctx)


async def show_admin_menu(chat_id: int, tg_user_id: int, ctx: ContextTypes.DEFAULT_TYPE, note: str = "") -> None:
    if chat_id != tg_user_id or not is_admin(tg_user_id):
        return
    await db.clear_admin_state(tg_user_id)
    text = "Админ-панель\n\nВыберите действие кнопками ниже."
    if note:
        text = f"{note}\n\n{text}"
    await render_screen(chat_id, tg_user_id, ctx, text=text, reply_markup=admin_menu_keyboard(),
                        include_admin_shortcut=False,back_callback='user:home')


async def admin_router(query, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    tg_user_id, chat_id = query.from_user.id, query.message.chat_id
    if chat_id != tg_user_id or not is_admin(tg_user_id):
        return
    await remember_user(query.from_user)
    data = query.data or ""
    await db.clear_admin_state(tg_user_id)
    if await extended_admin_route(data,chat_id,tg_user_id,ctx):
        return
    if data == "admin:offer":
        await db.set_admin_state(tg_user_id, "offer_url")
        await render_screen(chat_id, tg_user_id, ctx,
                            text=f"Текущая оферта:\n{await db.get_offer_url() or 'не задана'}\n\nОтправьте ссылку на оферту.",
                            reply_markup=admin_cancel_keyboard())
    elif data in {"admin:lead_magnet", "admin:intro"}:
        await show_admin_lead_magnet(chat_id, tg_user_id, ctx)
    elif data in {"admin:lead_set", "admin:intro_set"}:
        await db.set_admin_state(tg_user_id, "lead_magnet_url")
        await render_screen(chat_id, tg_user_id, ctx, text="Отправьте публичную ссылку на урок (https://…). Бот покажет её отдельным сообщением после приветствия.",
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


async def show_stats(chat_id: int, tg_user_id: int, ctx: ContextTypes.DEFAULT_TYPE, days=30, page_index=0) -> None:
    if chat_id!=tg_user_id or not is_admin(tg_user_id):
        return
    s=await db.funnel_stats()
    f=await db.funnel_breakdown(days)
    activity=await db.activity_summary()
    labels={'start':'Start: знакомство','lead_magnet_sent':'Получили ссылку урока','product_opened':'Открыли программу',
            'tariffs_opened':'Посмотрели тарифы','buy_clicked':'Начали оформление','offer_accepted':'Приняли условия',
            'payment_created':'Получили счёт','payment_succeeded':'Оплатили','channel_joined':'Вступили в канал'}
    paid=f['reached']['payment_succeeded']
    conversion=100*paid/f['cohort'] if f['cohort'] else 0
    lines=['📊 Воронка «Креативный мозг»',f"Первые запуски: {'за '+str(days)+' дней' if days else 'за всё время'} — {f['cohort']} чел.",
           f'От первого запуска до оплаты: {conversion:.1f}%',
           'Этап: дошли / пока не пошли дальше']
    lines += [f"{label}: {f['reached'][event]} / {f['stopped'][event]}" for event,label in labels.items()]
    lines += ['','Отправка ссылки ≠ просмотр видео. Последний этап показывает максимум достигнутого; причина остановки не угадывается.',
              f"\nАктивных подписок: {s['active_subscriptions_count']}",f"Оплат за всё время: {s['paid_count']}",
              f"Выручка за всё время: {s['revenue_rub']} ₽",f"Старых запросов с неизвестным результатом: {f['unknown_payments']}",
              f"\nГруппа за 30 дней: {activity['people']} участников · {activity['messages']} сообщений · {activity['files']} файлов/медиа",
              '\nПервый источник: люди / покупатели (всё время)']
    lines += [f"{r['source']}: {r['starts']} / {r['paid']}" for r in s['sources']]
    pages=[]; page=''
    for line in lines:
        if len(page)+len(line)>3500:
            pages.append(page); page=''
        page+=line+'\n'
    pages.append(page)
    page_index = max(0, min(page_index, len(pages)-1))
    rows=[[Btn('7 дней',callback_data='admin:stats:7'),Btn('30 дней',callback_data='admin:stats:30'),Btn('Всё время',callback_data='admin:stats:0')],
          [Btn('Назад',callback_data='admin:menu')]]
    if len(pages)>1:
        navigation=[]
        if page_index:
            navigation.append(Btn('← Страница',callback_data=f'admin:stats:{days}:{page_index-1}'))
        if page_index<len(pages)-1:
            navigation.append(Btn('Страница →',callback_data=f'admin:stats:{days}:{page_index+1}'))
        rows.insert(0,navigation)
    await render_screen(chat_id,tg_user_id,ctx,text=pages[page_index],reply_markup=Mk(rows))


async def handle_admin_input(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> bool:
    user, chat, message = update.effective_user, update.effective_chat, update.effective_message
    if not user or not chat or not message or chat.id != user.id or not is_admin(user.id):
        return False
    state = await db.get_admin_state(user.id)
    if not state:
        return False
    await remember_user(user)
    name, raw = state["state"], (message.text or "").strip()
    if await extended_admin_input(name,raw,state,update,ctx):
        return True
    if name in {"lead_magnet_url", "intro_video", "offer_url"}:
        valid = valid_http_url(raw)
        if not valid:
            await render_screen(chat.id, user.id, ctx,
                                text="Нужна корректная публичная http/https ссылка.",
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
        if not raw.isdigit() or not 0 < int(raw) < 2**63:
            await render_screen(chat.id, user.id, ctx, text="telegram_id должен быть положительным числом.", reply_markup=admin_cancel_keyboard())
            return True
        rows = await db.get_user_path(int(raw))
        lines = [f"Путь пользователя {raw} — последние 25 событий"]
        lines += [f"{format_dt(row['created_at'])} · {row['event_name']} · {row['source']}" for row in rows]
        if not rows:
            lines.append("Событий пока нет.")
        activity = await db.activity_summary(int(raw))
        lines.append('\nАктивность за 30 дней:\n'+activity_phrase(activity))
        renewal = await db.get_renewal(int(raw))
        lines.append('Автопродление: '+('включено' if renewal and renewal['enabled'] else 'выключено'))
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
            if not 0 < target < 2**63 or (name == "grant" and not 1 <= days <= 36500):
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
                    await render_screen(target,target,ctx,text=f"✅ Администратор выдал доступ к «Креативному мозгу» до {format_dt(sub['access_until'])}. Ссылка действует 1 час. Начни с закреплённого сообщения и Урока 1.",
                                        reply_markup=Mk([[Btn("🧠 Войти в «Креативный мозг»", url=invite)]]),back_callback='user:status',force_new=True)
                except TelegramError:
                    note += "\nНе удалось доставить ссылку. Проверьте права бота и доступность личного чата; срок уже записан."
                    logger.warning("admin grant delivery failed user_id=%s", target)
            else:
                await db.disable_renewal(target,'admin_revoke')
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
    token = _callback_query.set(query)
    try:
        async with user_lock(query.from_user.id):
            if (query.data or "").startswith("admin:"):
                await admin_router(query, ctx)
            else:
                await user_router(query, ctx)
    finally:
        _callback_query.reset(token)


async def fallback_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    user, chat = update.effective_user, update.effective_chat
    if not user or not chat or chat.type != "private":
        return
    if await handle_admin_input(update, ctx):
        return
    async with user_lock(user.id):
        await remember_user(user)
        if active_subscription_until(await db.get_subscription(user.id)):
            await show_active_access(chat.id, user.id, ctx)
        else:
            await render_screen(chat.id, user.id, ctx, text=texts.FALLBACK, reply_markup=Mk([
                [Btn("▶️ Бесплатный урок", callback_data="user:lead_magnet")],
                [Btn("🧠 Креативный мозг", callback_data="user:product")],
            ]))


@serialized_command
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
    if not isinstance(url, str) or not url or telegram_length(url) > 2048 or any(c.isspace() or ord(c) < 32 for c in url):
        return False
    try:
        parsed = urlsplit(url)
        return bool(parsed.scheme in {"http", "https"} and parsed.hostname
                    and not parsed.username and not parsed.password
                    and (parsed.port is None or 1 <= parsed.port <= 65535))
    except ValueError:
        return False


def telegram_length(value: str) -> int:
    return len(value.encode('utf-16-le')) // 2


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
                           welcome: bool = True, track: bool = True, refresh_lesson: bool = False) -> None:
    url=await db.get_lead_magnet_url()
    lesson=await db.get_lesson_message(tg_user_id) or {}
    query=_callback_query.get()
    if (query and query.from_user.id==tg_user_id and getattr(query.message,'message_id',None)
            and await is_lesson_message(query.message,tg_user_id) and not lesson.get('lesson_message_id')):
        text=getattr(query.message,'text',None) or ''
        if url and text.endswith('\n\n'+url):
            await db.set_lesson_message(tg_user_id,chat_id,query.message.message_id,url)
            lesson=await db.get_lesson_message(tg_user_id)
    if welcome:
        await render_screen(chat_id,tg_user_id,ctx,text=await content('WELCOME'),
                            reply_markup=next_keyboard('user:lead_magnet','▶️ Бесплатный урок'))
    if not valid_http_url(url):
        await render_screen(chat_id,tg_user_id,ctx,text='Урок пока готовится. Когда ссылка появится, здесь можно будет открыть его бесплатно.',
                            reply_markup=next_keyboard('user:product','Что будет дальше? →'),back_callback='user:welcome')
        return
    if not refresh_lesson and lesson.get('lesson_message_id') and lesson.get('lesson_chat_id')==chat_id and lesson.get('lesson_url')==url:
        if not welcome:
            await render_screen(chat_id,tg_user_id,ctx,text=await content('LEAD_CAPTION')+'\n\n'+url,
                                reply_markup=Mk([[Btn('▶️ Открыть урок',url=url)],
                                                 [Btn('Хочу продолжить →',callback_data='user:product')]]),
                                back_callback='user:welcome')
        return
    kwargs={'chat_id':chat_id,'text':await content('LEAD_CAPTION')+'\n\n'+url,
            'reply_markup':with_admin_menu(tg_user_id,navigation_keyboard(next_keyboard('user:product','Хочу продолжить →'),'user:welcome'))}
    if LinkPreviewOptions is not None:
        kwargs['link_preview_options']=LinkPreviewOptions(url=url,is_disabled=False,prefer_large_media=True,show_above_text=False)
    else:
        kwargs['disable_web_page_preview']=False
    try:
        message=await ctx.bot.send_message(**kwargs)
    except BadRequest:
        if 'link_preview_options' not in kwargs:
            raise
        kwargs.pop('link_preview_options')
        message=await ctx.bot.send_message(**kwargs,disable_web_page_preview=False)
    await db.set_lesson_message(tg_user_id,chat_id,message.message_id,url)
    if refresh_lesson and lesson.get('lesson_chat_id') == chat_id and lesson.get('lesson_message_id'):
        # Replace the old copy only after a new visible video/link has been delivered.
        await retire_menu(chat_id, lesson['lesson_message_id'], tg_user_id, ctx)
    if track:
        await db.record_event(tg_user_id,'lead_magnet_sent')


async def show_product(chat_id: int, tg_user_id: int, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    tariffs=await db.list_tariffs()
    markup=tariff_keyboard(tariffs) if len(tariffs)>1 else next_keyboard('user:buy','Я хочу')
    await render_screen(chat_id,tg_user_id,ctx,text=await content('PRODUCT'),
                        reply_markup=markup,back_callback='user:lesson')
    await db.record_event(tg_user_id,'product_opened')
    if len(tariffs)>1:
        await db.record_event(tg_user_id,'tariffs_opened')


async def buy_access(chat_id: int, tg_user_id: int, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    # A new purchase/renewal must offer all currently published plans.
    ctx.user_data.pop('selected_tariff',None)
    await start_offer_again(chat_id,tg_user_id,ctx)


async def refresh_payment(row: dict) -> dict:
    return await billing.refresh_payment(row,YOOKASSA_SHOP_ID,YOOKASSA_SECRET_KEY)


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
    await render_screen(chat_id, tg_user_id, ctx, text=f"🎬 Лид-магнит\n\nТекущая ссылка:\n{url or 'не задана'}",
                        reply_markup=Mk(rows))


async def followup_job(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if await db.get_setting('followup_enabled',str(int(FOLLOWUP_ENABLED)))!='1':
        return
    for uid in await db.followup_candidates():
        async with user_lock(uid):
            if not await reconcile_user(uid,ctx):
                continue
            if not await db.claim_followup(uid):
                continue
            try:
                await render_screen(uid,uid,ctx,text=await content('FOLLOWUP'),reply_markup=Mk([
                    [Btn('Посмотреть, что внутри',callback_data='user:product')],
                    [Btn('Не напоминать об уроке',callback_data='user:quiet')]]),
                    back_callback='user:lesson',force_new=True)
                await db.mark_followup_sent(uid)
            except TelegramError:
                logger.warning('followup delivery unconfirmed; no retry user_id=%s',uid)


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
        else:
            await db.record_event(target,'channel_joined',event_key='join:'+invite['invite_link'])
        # Also retire legacy links created before join-request approval was used.
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
            if update.callback_query:
                await update.callback_query.answer('Не удалось выполнить действие. Попробуй ещё раз чуть позже.',show_alert=True)
            else:
                await render_screen(update.effective_chat.id,update.effective_user.id,ctx,
                                    text='Не удалось выполнить действие. Попробуй ещё раз чуть позже.',force_new=True)
        except TelegramError:
            pass


SETTING_LABELS = {
    'reminder_days':'За сколько дней напоминать', 'activity_chat_id':'Группа для учёта активности',
    'support_url':'Ссылка на поддержку', 'onboarding_url':'Ссылка на первый урок в канале',
}


async def extended_admin_route(data,chat_id,uid,ctx):
    if data=='admin:tariffs':
        tariffs=await db.list_tariffs(include_disabled=True)
        rows=[[Btn(('🟢 ' if t['enabled'] else '⚪ ')+f"{t['title']}: {t['amount_rub']} ₽ / {t['days']} дн.",callback_data='admin:tariff:'+t['code'])] for t in tariffs]
        rows += [[Btn('Добавить тариф',callback_data='admin:tariff_new')],[Btn('Назад',callback_data='admin:menu')]]
        await render_screen(chat_id,uid,ctx,text='Тарифы\n\nОдин включённый тариф: кнопка «Я хочу» сразу ведёт к условиям оплаты. Несколько: под описанием появляются кнопки с названием и ценой каждого тарифа. Скрытые тарифы покупатель не видит.\n\nИзменения действуют для новых счетов. Выставленные счета сохраняют свою цену и срок. При изменении условий автопродление потребует нового выбора клиента.',reply_markup=Mk(rows))
    elif data=='admin:tariff_new':
        if len(await db.list_tariffs(include_disabled=True))>=20:
            await render_screen(chat_id,uid,ctx,text='Доступно до 20 тарифов. Можно отредактировать или включить существующий.',reply_markup=admin_cancel_keyboard())
            return True
        await db.set_admin_state(uid,'tariff_new')
        await render_screen(chat_id,uid,ctx,text='Отправьте три строки:\nНазвание тарифа\nЦена в рублях\nКоличество дней\n\nНапример:\nТри месяца\n990\n90\n\nНовый тариф появится выключенным: его можно проверить и включить отдельно.',reply_markup=admin_cancel_keyboard())
    elif data.startswith(('admin:tariff:','admin:tariff_toggle:','admin:tariff_edit:')):
        parts=data.split(':')
        code=parts[2]
        tariff=next((t for t in await db.list_tariffs(True) if t['code']==code),None)
        if not tariff:
            await extended_admin_route('admin:tariffs',chat_id,uid,ctx)
            return True
        if parts[1]=='tariff_toggle':
            await db.save_tariff(code,tariff['title'],tariff['amount_rub'],tariff['days'],not tariff['enabled'],uid)
            tariff['enabled']=not tariff['enabled']
        if parts[1]=='tariff_edit':
            field=parts[3] if len(parts)==4 else ''
            if field not in {'title','amount_rub','days'}:
                return True
            await db.set_admin_state(uid,'tariff_edit',{'code':code,'field':field})
            await render_screen(chat_id,uid,ctx,text=f"Текущее значение: {tariff[field]}\n\nОтправьте новое значение.",reply_markup=admin_cancel_keyboard())
            return True
        rows=[[Btn(label,callback_data=f'admin:tariff_edit:{code}:{field}')] for field,label in [('title','Название'),('amount_rub','Цена'),('days','Срок')]]
        rows += [[Btn('Выключить' if tariff['enabled'] else 'Включить',callback_data='admin:tariff_toggle:'+code)],[Btn('Все тарифы',callback_data='admin:tariffs')]]
        await render_screen(chat_id,uid,ctx,text=f"{tariff['title']}\n{tariff['amount_rub']} ₽ · {tariff['days']} дней\n{'Доступен для покупки' if tariff['enabled'] else 'Скрыт от покупателей'}",reply_markup=Mk(rows))
    elif data=='admin:texts':
        rows=[[Btn(label,callback_data='admin:text:'+key)] for key,label in texts.EDITABLE.items()]
        rows.append([Btn('Назад',callback_data='admin:menu')])
        await render_screen(chat_id,uid,ctx,text='Тексты бота\n\nИзменения увидят следующие пользователи. Цена, срок и обязательное описание способа продления добавляются отдельно.',reply_markup=Mk(rows))
    elif data.startswith('admin:text:'):
        key=data.split(':',2)[2]
        if key not in texts.EDITABLE:
            return True
        await db.set_admin_state(uid,'content',{'key':key})
        current=await db.get_setting('text:'+key,getattr(texts,key))
        if current==texts.PREVIOUS_DEFAULTS.get(key):
            current=getattr(texts,key)
        await render_screen(chat_id,uid,ctx,text=f'{texts.EDITABLE[key]}\n\n{current}\n\nОтправьте новый текст (до 2000 знаков; эмодзи могут занимать два). Сохраните поля в фигурных скобках, если они есть.',reply_markup=Mk([[Btn('Вернуть исходный текст',callback_data='admin:text_reset:'+key)],[Btn('Отмена',callback_data='admin:texts')]]))
    elif data.startswith('admin:text_reset:'):
        key=data.split(':',2)[2]
        if key in texts.EDITABLE:
            await db.set_admin_setting('text:'+key,getattr(texts,key),uid)
            await extended_admin_route('admin:texts',chat_id,uid,ctx)
    elif data=='admin:settings':
        auto=await auto_available()
        follow=await db.get_setting('followup_enabled',str(int(FOLLOWUP_ENABLED)))=='1'
        rows=[[Btn('Автопродление: '+('включено' if auto else 'выключено'),callback_data='admin:toggle:auto_renew_enabled')],
              [Btn('Напоминание об уроке: '+('включено' if follow else 'выключено'),callback_data='admin:toggle:followup_enabled')]]
        rows += [[Btn(label,callback_data='admin:setting:'+key)] for key,label in SETTING_LABELS.items()]
        rows.append([Btn('Назад',callback_data='admin:menu')])
        await render_screen(chat_id,uid,ctx,text='Настройки\n\nАвтопродление предлагается покупателю отдельно. Оно работает только после его согласия, успешной оплаты и подтверждения сохранённого способа оплаты от ЮKassa.\n\nАктивность: только сообщения и файлы в указанной группе, без сохранения их содержания.',reply_markup=Mk(rows))
    elif data.startswith('admin:toggle:'):
        key=data.split(':',2)[2]
        if key in {'auto_renew_enabled','followup_enabled'}:
            current=await db.get_setting(key,'0' if key=='auto_renew_enabled' else str(int(FOLLOWUP_ENABLED)))
            await db.set_admin_setting(key,'0' if current=='1' else '1',uid)
        await extended_admin_route('admin:settings',chat_id,uid,ctx)
    elif data.startswith('admin:setting:'):
        key=data.split(':',2)[2]
        if key not in SETTING_LABELS:
            return True
        await db.set_admin_state(uid,'setting',{'key':key})
        prompt={'reminder_days':'Целое число от 2 до 14.',
                'activity_chat_id':'Числовой ID группы, начинающийся с минуса. 0 отключает учёт. Добавьте бота администратором группы; старые сообщения недоступны.',
                'support_url':'Публичная https-ссылка на поддержку. Для удаления отправьте «удалить».',
                'onboarding_url':'Ссылка на первый урок или навигацию внутри канала. Для удаления отправьте «удалить».'}[key]
        await render_screen(chat_id,uid,ctx,text=SETTING_LABELS[key]+f"\nСейчас: {await db.get_setting(key,'не задано')}\n\n"+prompt,reply_markup=admin_cancel_keyboard())
    elif data.startswith('admin:stats:'):
        parts=data.split(':')
        if len(parts) in {3,4} and parts[2] in {'7','30','0'} and (len(parts)==3 or parts[3].isdigit()):
            await show_stats(chat_id,uid,ctx,days=int(parts[2]),page_index=int(parts[3]) if len(parts)==4 else 0)
    elif data=='admin:diagnostics':
        lines=['Проверка подключения',f'Канал: {CHANNEL_ID}']
        try:
            me=await ctx.bot.get_me()
            member=await ctx.bot.get_chat_member(CHANNEL_ID,me.id)
            lines.append('Бот — администратор: '+('да' if member.status in {'administrator','creator'} else 'нет'))
            lines.append('Приглашения: '+('да' if getattr(member,'can_invite_users',False) else 'нет'))
            lines.append('Удаление участников: '+('да' if getattr(member,'can_restrict_members',False) else 'нет'))
            group=int(await db.get_setting('activity_chat_id','0'))
            if group:
                chat=await ctx.bot.get_chat(group)
                group_member=await ctx.bot.get_chat_member(group,me.id)
                lines.append('Группа доступна: '+str(chat.title))
                lines.append('Учёт всех новых сообщений: '+('бот — администратор' if group_member.status in {'administrator','creator'} else 'проверьте режим приватности BotFather'))
        except TelegramError:
            lines.append('Telegram не подтвердил права. Проверьте ID и доступ бота.')
        lines += ['Ключи ЮKassa: '+('заданы' if YOOKASSA_SHOP_ID and YOOKASSA_SECRET_KEY else 'не заданы'),
                  'Возможность автоплатежей магазина: подтверждается только платёжным ответом или кабинетом ЮKassa. Проверка не списывает деньги.']
        await render_screen(chat_id,uid,ctx,text='\n'.join(lines),reply_markup=admin_cancel_keyboard())
    else:
        return False
    return True


async def extended_admin_input(name,raw,state,update,ctx):
    if name not in {'tariff_new','tariff_edit','content','setting'}:
        return False
    uid=update.effective_user.id
    payload=state['payload']
    next_page='admin:menu'
    try:
        if name=='tariff_new':
            parts=[s.strip() for s in raw.splitlines() if s.strip()]
            if len(parts)!=3 or len(await db.list_tariffs(True))>=20:
                raise ValueError('Нужны три строки: название, цена в рублях, число дней. Доступно до 20 тарифов.')
            code='plan_'+uuid.uuid4().hex[:12]
            await db.save_tariff(code,parts[0],int(parts[1]),int(parts[2]),False,uid)
            next_page='admin:tariff:'+code
        elif name=='tariff_edit':
            tariff=next((t for t in await db.list_tariffs(True) if t['code']==payload['code']),None)
            if not tariff:
                raise ValueError('Тариф не найден.')
            field=payload['field']
            if field not in {'title','amount_rub','days'}:
                raise ValueError('Неизвестная настройка.')
            tariff[field]=raw if field=='title' else int(raw)
            await db.save_tariff(tariff['code'],tariff['title'],tariff['amount_rub'],tariff['days'],tariff['enabled'],uid)
            next_page='admin:tariff:'+tariff['code']
        elif name=='content':
            key=payload['key']
            if key not in texts.EDITABLE or not 1<=telegram_length(raw)<=2000:
                raise ValueError('Нужен текст от 1 до 2000 знаков; эмодзи могут занимать два.')
            allowed={f for _,f,_,_ in Formatter().parse(getattr(texts,key)) if f}
            fields=set()
            for _,field,spec,conversion in Formatter().parse(raw):
                if field is not None:
                    if field not in allowed or spec or conversion:
                        raise ValueError('Допустимые поля: '+(', '.join(sorted(allowed)) or 'нет'))
                    fields.add(field)
            if fields!=allowed:
                raise ValueError('Сохраните поля: '+', '.join(sorted(allowed)))
            await db.set_admin_setting('text:'+key,raw,uid)
            next_page='admin:texts'
        else:
            key=payload['key']
            if key not in SETTING_LABELS:
                raise ValueError('Неизвестная настройка.')
            value=raw
            if key=='reminder_days':
                value=int(raw)
                if not 2<=value<=14:
                    raise ValueError('Введите число от 2 до 14.')
            elif key=='activity_chat_id':
                value=int(raw)
                if not -(2**63)<value<=0:
                    raise ValueError('Нужен отрицательный ID группы или 0.')
            else:
                value='' if raw.lower()=='удалить' else raw
                if value and not valid_http_url(value):
                    raise ValueError('Нужна публичная http/https ссылка.')
            await db.set_admin_setting(key,value,uid)
            next_page='admin:settings'
    except (ValueError,TypeError) as exc:
        await render_screen(uid,uid,ctx,text='Не сохранено. '+str(exc)+'\n\nПопробуйте ещё раз.',reply_markup=admin_cancel_keyboard())
        return True
    await db.clear_admin_state(uid)
    await delete_incoming(update,ctx)
    await extended_admin_route(next_page,uid,uid,ctx)
    return True


async def content(key, **values):
    template = await db.get_setting('text:'+key,getattr(texts,key))
    if template == texts.PREVIOUS_DEFAULTS.get(key):
        template=getattr(texts,key)
    return template.format(**values)


async def auto_available():
    return await db.get_setting('auto_renew_enabled','0') == '1'


async def selected_tariff(ctx):
    rows = await db.list_tariffs()
    code = ctx.user_data.get('selected_tariff')
    row = next((r for r in rows if r['code']==code),None)
    if row is None and len(rows)==1:
        row=rows[0]
    return Tariff(row['code'],row['title'],row['amount_rub'],row['days']) if row else None


def tariff_keyboard(tariffs):
    return Mk([[Btn(f"{t['title']} · {t['amount_rub']} ₽",callback_data='user:choose:'+t['code'])] for t in tariffs])


async def show_tariffs(chat_id,user_id,ctx):
    tariffs=await db.list_tariffs()
    text='Выбери удобный срок доступа. При продлении оставшиеся дни сохраняются.' if tariffs else 'Новые покупки пока приостановлены. Бесплатный урок остаётся доступным.'
    if tariffs:
        text+='\n\n'+'\n'.join(f"{t['title']}: {t['amount_rub']} ₽ / {t['days']} дней" for t in tariffs)
    await render_screen(chat_id,user_id,ctx,text=text,reply_markup=tariff_keyboard(tariffs),back_callback='user:product')
    await db.record_event(user_id,'tariffs_opened')


def payment_payload(user_id,tariff,*,auto_opt_in=False,method_id=None):
    payload={
        'amount':{'value':f'{tariff.amount_rub:.2f}','currency':'RUB'},'capture':True,
        'description':f'Доступ к «Креативному мозгу»: {tariff.days} дней',
        'metadata':{'project_key':PROJECT_KEY,'user_id':str(user_id),'kind':'channel_access',
                    'tariff_code':tariff.code,'days':str(tariff.days)},
    }
    if method_id:
        payload['payment_method_id']=method_id
    else:
        payload['confirmation']={'type':'redirect','return_url':YOOKASSA_RETURN_URL or (f'https://t.me/{BOT_USERNAME}' if BOT_USERNAME else 'https://t.me/')}
        if auto_opt_in:
            payload['save_payment_method']=True
    return payload


async def render_payment(chat_id,user_id,row,ctx,*,force_new=False):
    if row['status']=='canceled':
        await render_screen(chat_id,user_id,ctx,text='Счёт отменён. Выбери оплату заново.',reply_markup=next_keyboard('user:buy','К оплате →'),force_new=force_new)
        return
    metadata=json.loads(row['metadata_json'])
    order=await db.get_checkout_by_payment(row['payment_id'])
    if order and order['offer_url'] and row['status']=='pending' and valid_http_url(row['confirmation_url']):
        text=await content('OFFER')
        text+=f"\n\n{order['title']}\n{row['amount_rub']} ₽ · {metadata['days']} дней.\nРазовая покупка, без автоматических списаний."
        await render_screen(chat_id,user_id,ctx,text=text,reply_markup=Mk([
            [Btn('📄 Условия',url=order['offer_url'])],
            [Btn('Принимаю · оплатить разово',url=row['confirmation_url'])]]),back_callback='user:product',force_new=force_new)
        await db.mark_offer_presented(row['payment_id'])
        return
    auto=bool(order and (order['auto_opt_in'] or order['cycle_until']))
    text=await content('PAYMENT_CREATED')
    text+=f"\n\n{order['title'] if order else 'Доступ в канал'}: {row['amount_rub']} ₽ / {metadata['days']} дней."
    text+='\nВыбрано автопродление. Оно включится только после успешной оплаты и сохранения способа оплаты.' if auto else '\nРазовая покупка, без автоматических списаний.'
    await render_screen(chat_id,user_id,ctx,text=text,reply_markup=payment_keyboard(row['payment_id'],row['confirmation_url'],amount_rub=row['amount_rub']),force_new=force_new)


async def handle_auto_failure(row,ctx):
    con=await db.connection()
    async with con.execute('SELECT * FROM checkout_requests WHERE payment_id=?',(row['payment_id'],)) as cur:
        order=await cur.fetchone()
    if not order or not order['cycle_until']:
        return
    await db.disable_renewal(row['tg_user_id'],'payment_canceled')
    await db.record_event(row['tg_user_id'],'auto_failed',event_key='auto_failed:'+row['payment_id'])
    key='auto_failed:'+row['payment_id']
    if await db.claim_notification(key,row['tg_user_id']):
        sub=await db.get_subscription(row['tg_user_id'])
        text=f"Автопродление не прошло. Новых автоматических попыток не будет.\n\nДоступ оплачен до {format_dt(sub['access_until'])}. Можно продлить вручную удобным способом."
        try:
            await render_screen(row['tg_user_id'],row['tg_user_id'],ctx,text=text,
                                reply_markup=next_keyboard('user:buy','Продлить вручную →'),force_new=True)
            await db.mark_notification_sent(key)
        except TelegramError:
            logger.warning('auto failure notice unconfirmed user_id=%s',row['tg_user_id'])


async def reconcile_user(user_id,ctx):
    """Returns False when an unsettled payment still has an unknown outcome."""
    ok=True
    order=await db.pending_checkout(user_id)
    if order and not order['payment_id']:
        try:
            recovered=await billing.submit_checkout(order,YOOKASSA_SHOP_ID,YOOKASSA_SECRET_KEY)
            if not order['cycle_until'] and recovered['status']=='pending' and valid_http_url(recovered['confirmation_url']):
                key='checkout_ready:'+order['id']
                if await db.claim_notification(key,user_id):
                    try:
                        await render_payment(user_id,user_id,recovered,ctx,force_new=True)
                        await db.mark_notification_sent(key)
                    except TelegramError:
                        logger.warning('recovered checkout delivery unconfirmed user_id=%s',user_id)
        except billing.PaymentRejected:
            if order['cycle_until']:
                await db.disable_renewal(user_id,'provider_rejected')
                await send_auto_problem(user_id,'ЮKassa не разрешила автоматическое продление. Можно оплатить вручную.',ctx,key='auto_rejected:'+order['id'])
        except (httpx.HTTPError,ValueError,TypeError,AttributeError,ArithmeticError):
            return False
    for row in await db.list_unfinalized_payments(user_id):
        try:
            remote=billing.validate_payment(json.loads(row['raw_json']),row) if row['status']=='succeeded' else await refresh_payment(row)
            if remote['status']=='succeeded':
                result=await billing.finalize(await db.get_payment(row['payment_id']))
                ok=ok and bool(result.get('ok'))
            elif remote['status']=='canceled':
                await handle_auto_failure(row,ctx)
            else:
                ok=False
        except (httpx.HTTPError,ValueError,TypeError,AttributeError,ArithmeticError):
            ok=False
    return ok


async def payment_reconciliation_job(ctx):
    if not YOOKASSA_SHOP_ID or not YOOKASSA_SECRET_KEY:
        return
    payments,orders=await db.reconciliation_candidates()
    users=list(dict.fromkeys([r['tg_user_id'] for r in payments+orders]))
    for user_id in users:
        async with user_lock(user_id):
            try:
                await reconcile_user(user_id,ctx)
            except Exception:
                logger.exception('payment reconciliation deferred user_id=%s',user_id)
    # Delivery is retried separately from financial fulfilment, including after restart.
    for row in await db.access_notifications():
        async with user_lock(row['tg_user_id']):
            sub=await db.get_subscription(row['tg_user_id'])
            if not active_subscription_until(sub):
                await db.mark_access_notified(row['payment_id'])
                continue
            try:
                await db.touch_access_delivery(row['payment_id'])
                await send_access_link(row['tg_user_id'],row['tg_user_id'],row['payment_id'],sub['access_until'],ctx,force_new=True)
            except TelegramError:
                logger.warning('automatic access delivery deferred user_id=%s',row['tg_user_id'])


async def send_auto_problem(user_id,text,ctx,*,key):
    if await db.claim_notification(key,user_id):
        try:
            await render_screen(user_id,user_id,ctx,text=text,reply_markup=next_keyboard('user:buy','Продлить вручную →'),force_new=True)
            await db.mark_notification_sent(key)
        except TelegramError:
            logger.warning('renewal notification unconfirmed user_id=%s',user_id)


async def automatic_renewals_job(ctx):
    if not await auto_available() or not YOOKASSA_SHOP_ID or not YOOKASSA_SECRET_KEY:
        return
    for candidate in await db.due_renewals():
        uid=candidate['tg_user_id']
        async with user_lock(uid):
            contract=await db.get_renewal(uid)
            sub=await db.get_subscription(uid)
            if not contract or not contract['enabled'] or sub['access_until']!=candidate['access_until'] or sub['status']!='active':
                continue
            tariff=next((t for t in await db.list_tariffs() if t['code']==contract['plan_id']),None)
            if not tariff or (tariff['amount_rub'],tariff['days'])!=(contract['amount_rub'],contract['days']):
                await db.disable_renewal(uid,'tariff_changed')
                await send_auto_problem(uid,'Условия тарифа изменились. Автопродление приостановлено: выбери новый вариант, когда будет удобно.',ctx,key='changed:'+contract['consent_key'])
                continue
            until=parse_dt(sub['access_until'])
            if until < db.utcnow()-timedelta(hours=24):
                await db.disable_renewal(uid,'missed_cycle')
                await send_auto_problem(uid,'Автопродление не было выполнено вовремя. Запоздалого списания не будет; доступ можно восстановить вручную.',ctx,key='missed:'+contract['consent_key']+sub['access_until'])
                continue
            warned=parse_dt(contract.get('warned_at'))
            if contract.get('warned_until')!=sub['access_until'] or not warned or db.utcnow()-warned<timedelta(hours=12):
                continue  # Always give the user time to read the warning and cancel.
            if await db.pending_checkout(uid) or await db.list_unfinalized_payments(uid):
                continue  # A manual/uncertain payment must settle before any automatic charge.
            plan=Tariff(tariff['code'],tariff['title'],contract['amount_rub'],contract['days'])
            payload=payment_payload(uid,plan,method_id=contract['payment_method_id'])
            order=await db.prepare_checkout(uid,plan,payload,cycle_until=sub['access_until'])
            if order['status']=='canceled' and order['payment_id']:
                await handle_auto_failure(await db.get_payment(order['payment_id']),ctx)
                continue
            if order['status']=='failed':
                await db.disable_renewal(uid,'provider_rejected')
                await send_auto_problem(uid,'Автопродление отклонено. Можно продлить доступ вручную.',ctx,key='auto_rejected:'+order['id'])
                continue
            if order['status'] not in {'creating','created'}:
                continue
            try:
                row=await billing.submit_checkout(order,YOOKASSA_SHOP_ID,YOOKASSA_SECRET_KEY)
                if row['status']=='succeeded':
                    await billing.finalize(row)
                elif row['status']=='canceled':
                    await handle_auto_failure(row,ctx)
            except billing.PaymentRejected:
                await db.disable_renewal(uid,'provider_rejected')
                await send_auto_problem(uid,'ЮKassa не разрешила автопродление. Новых автоматических попыток не будет; можно оплатить вручную.',ctx,key='auto_rejected:'+order['id'])
            except (httpx.HTTPError,ValueError,TypeError,AttributeError,ArithmeticError):
                logger.warning('automatic payment outcome unknown order=%s',order['id'])


async def channel_join_request(update,ctx):
    request=update.chat_join_request
    if not request or request.chat.id!=CHANNEL_ID:
        return
    uid=request.from_user.id
    async with user_lock(uid):
        invite=await db.get_invite_link(request.invite_link.invite_link) if request.invite_link else None
        valid=(invite and invite['tg_user_id']==uid and not invite['used']
               and (not invite['expire_at'] or parse_dt(invite['expire_at'])>db.utcnow())
               and active_subscription_until(await db.get_subscription(uid)))
        if not valid:
            await ctx.bot.decline_chat_join_request(CHANNEL_ID,uid)
            return
        await ctx.bot.approve_chat_join_request(CHANNEL_ID,uid)
        await db.record_event(uid,'channel_joined',event_key='join:'+invite['invite_link'])
        try:
            await ctx.bot.revoke_chat_invite_link(CHANNEL_ID,invite['invite_link'])
            await db.mark_invite_used(invite['invite_link'])
        except TelegramError:
            logger.warning('used invite revocation deferred user_id=%s',uid)
        onboarding=await db.get_setting('onboarding_url','')
        if valid_http_url(onboarding) and await db.claim_notification('onboarding:'+str(uid),uid):
            try:
                await render_screen(uid,uid,ctx,text='Ты в «Креативном мозге». Начни с первого материала — дальше двигайся в удобном темпе.',
                                    reply_markup=Mk([[Btn('Начать →',url=onboarding)]]),back_callback='user:status',force_new=True)
                await db.mark_notification_sent('onboarding:'+str(uid))
            except TelegramError:
                logger.warning('onboarding delivery unconfirmed user_id=%s',uid)


async def group_activity(update,ctx):
    message=getattr(update,'message',None)
    if not message or not message.from_user or message.from_user.is_bot or message.sender_chat:
        return
    group_id=int(await db.get_setting('activity_chat_id','0'))
    if not group_id or message.chat.id!=group_id or message.chat.type not in {'group','supergroup'}:
        return
    uid=message.from_user.id
    if not active_subscription_until(await db.get_subscription(uid)):
        return
    file_present=any(bool(getattr(message,key,None)) for key in ('document','photo','video','audio','voice','video_note','animation'))
    if not file_present and not message.text:
        return
    when=message.date.astimezone(timezone.utc).isoformat()
    await db.record_activity(group_id,message.message_id,uid,when,file_present)


async def show_user_activity(chat_id,user_id,ctx):
    stats=await db.activity_summary(user_id)
    text='За последние 30 дней в подключённой группе:\n'+activity_phrase(stats)
    text+='\n\nУчитываются только сообщения, полученные ботом после подключения. Содержимое сообщений и файлов не сохраняется.'
    await render_screen(chat_id,user_id,ctx,text=text,reply_markup=next_keyboard('user:status','Мой доступ'))


def activity_phrase(stats):
    parts=[]
    if stats['messages']:
        parts.append(f"Сообщений: {stats['messages']}")
    if stats['files']:
        parts.append(f"Файлов и медиа: {stats['files']}")
    return '\n'.join(parts) if parts else 'Можно продолжать в своём темпе — в том числе просто читать и пробовать идеи самостоятельно.'


@serialized_command
async def support(update,ctx):
    if not update.effective_user or update.effective_chat.type!='private':
        return
    uid=update.effective_user.id
    await remember_user(update.effective_user)
    url=await db.get_setting('support_url','')
    text='По вопросам доступа и оплаты можно обратиться в поддержку. Укажи свой номер пользователя: '+str(uid)+'. Данные карты присылать не нужно.'
    rows=[[Btn('Написать в поддержку',url=url)]] if valid_http_url(url) else []
    if not rows:
        text='Контакт поддержки пока не настроен. Сохрани номер пользователя '+str(uid)+' и чек для обращения к автору канала. Статус покупки можно проверить через /start.'
    rows.append([Btn('Мой доступ',callback_data='user:status')])
    rows.append([Btn('Отключить автопродление',callback_data='user:auto_off')])
    text+='\n\nАвтопродление можно отключить в любое время командой /cancel_subscription. В подключённой группе бот считает сообщения и медиа участников, без сохранения их содержания.'
    await render_screen(uid,uid,ctx,text=text,reply_markup=Mk(rows))


@serialized_command
async def terms(update,ctx):
    if not update.effective_user or not update.effective_chat or update.effective_chat.type!='private':
        return
    uid=update.effective_user.id
    await remember_user(update.effective_user)
    url=await db.get_offer_url()
    markup=Mk([[Btn('Открыть условия',url=url)]]) if valid_http_url(url) else None
    await render_screen(uid,uid,ctx,text='Условия доступа и оплаты:' if markup else 'Условия пока не опубликованы. Оформление доступа временно недоступно.',reply_markup=markup)


async def disable_auto_and_render(chat_id,user_id,ctx):
    await db.disable_renewal(user_id)
    await render_screen(chat_id,user_id,ctx,text='Автопродление отключено. Оплаченный срок сохраняется. Если банк уже обрабатывает ранее отправленный платёж, дождёмся его результата; новых списаний не запустим.',reply_markup=next_keyboard('user:status','Мой доступ'))


@serialized_command
async def cancel_subscription(update,ctx):
    if not update.effective_user or not update.effective_chat or update.effective_chat.type!='private':
        return
    uid=update.effective_user.id
    await remember_user(update.effective_user)
    await disable_auto_and_render(update.effective_chat.id,uid,ctx)


def build_application() -> Application:
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is required")
    app = Application.builder().token(BOT_TOKEN).post_init(init_storage).post_shutdown(close_storage).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("admin", admin))
    app.add_handler(CommandHandler(["help","support","paysupport"],support))
    app.add_handler(CommandHandler("terms",terms))
    app.add_handler(CommandHandler("cancel_subscription",cancel_subscription))
    app.add_handler(CommandHandler(["set_offer", "set_lead_magnet", "clear_lead_magnet", "set_intro_video", "clear_intro_video", "grant", "revoke", "stats", "user_path"], admin_shortcut))
    app.add_handler(CallbackQueryHandler(router))
    app.add_handler(ChatMemberHandler(channel_member_updated, ChatMemberHandler.CHAT_MEMBER))
    app.add_handler(ChatJoinRequestHandler(channel_join_request))
    app.add_handler(MessageHandler(filters.ChatType.GROUPS & ~filters.COMMAND,group_activity),group=1)
    app.add_error_handler(error_handler)
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, fallback_message))
    if app.job_queue is None:
        raise RuntimeError("JobQueue is unavailable. Install python-telegram-bot[job-queue].")
    app.job_queue.run_repeating(payment_reconciliation_job, interval=20, first=5, name="payment_reconciliation")
    app.job_queue.run_repeating(automatic_renewals_job, interval=60, first=25, name="automatic_renewals")
    app.job_queue.run_repeating(renewal_notices_job, interval=300, first=15, name="renewal_notices")
    app.job_queue.run_repeating(expire_subscriptions_job, interval=60, first=45, name="expire_subscriptions")
    app.job_queue.run_repeating(followup_job, interval=3600, first=180, name="lead_followup")
    return app


def main() -> None:
    app = build_application()
    logger.info("starting polling bot project_key=%s channel_id=%s", PROJECT_KEY, CHANNEL_ID)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
