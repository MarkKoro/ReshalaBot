#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Простой бот «Решало».

Бот отвечает на текстовые вопросы через DeepSeek и умеет анализировать
фотографии через GPT‑4o. Логика оплаты и бонусов реализована через
асинхронную SQLite-базу.
"""

import os, io, asyncio, logging, json, sys
from dotenv import load_dotenv
from PIL import Image
from telegram import (
    Update,
    InlineKeyboardButton as Btn,
    InlineKeyboardMarkup as Mk,
    LabeledPrice,
    CopyTextButton,
)
from telegram.constants import ParseMode
from telegram.error import NetworkError, RetryAfter
from telegram.request import HTTPXRequest
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    PreCheckoutQueryHandler,
    ContextTypes,
    JobQueue,
    AIORateLimiter,
    filters,
)

from utils.llm import ask_deepseek, analyze_photo
from db import (
    get_credits,
    add_credits,
    dec_credits,
    can_use_daily_text,
    mark_daily_text,
    get_free_photo,
    use_free_photo,
    grant_start_bonus,
    grant_referral_bonus,
    set_ref_parent,
    create_tables,
    inc_text_cnt,
    inc_photo_cnt,
    get_counts,
    get_all_uids,
    set_menu_msg,
    get_menu_msg,
    get_ref_count,
    set_free_photo,
    set_alias,
    get_uid_by_alias,
    get_alias,
    log_payment,
    get_referral_stats,
    get_alias_stats,
)

# ── Глобальные настройки ─────────────────────────────
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
YK_TOKEN = os.getenv("YK_TOKEN", "")
ADMINS = {
    int(x)
    for x in (os.getenv("ADMINS") or os.getenv("ADMIN_IDS", "")).split(",")
    if x
}

TEXT_COST = 10
PHOTO_COST = 20
# Ключ словаря — количество баллов, значение — цена
PRICE_PACKS_RUB = {100: 100_00, 350: 300_00, 600: 500_00}
PRICE_PACKS_STARS = {100: 55, 350: 170, 600: 280}

MAIN_TEXT = (
    "Я — Решало. Отправь текстовый вопрос или фото с подписью — и я подскажу, как действовать.\n"
    "Каждый день доступен один бесплатный текстовый вопрос. Фотоанализ стоит 20 баллов."
)
HOW_IT_WORKS = (
    "Бесплатно: 1 текст в день\n"
    "Фото-анализ: 1 стартовый, 2 по ссылке, +1 за друга\n"
    "Текст = 10 баллов, Фото = 20 баллов\n"
    "100₽→100⚡ | 300₽→350⚡ | 500₽→600⚡\n"
    "Команда /profile — статистика и рефералы"
)
FOOTER = "\n\n— Решала"

KB_BACK = Mk([[Btn("◀️ Назад", callback_data="back")]])
KB_PAY = Mk([[Btn("Оплатить", pay=True)], [Btn("◀️ Назад", callback_data="back")]])

def clean_md(text: str) -> str:
    for ch in "#*🔹":
        text = text.replace(ch, "")
    return text

async def clear_menu(chat, uid: int, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Убрать клавиатуру у предыдущих сообщений."""
    ans_id = ctx.user_data.pop("ans_msg_id", None)
    if ans_id:
        try:
            await ctx.bot.edit_message_reply_markup(
                chat_id=chat.id, message_id=ans_id, reply_markup=None
            )
        except Exception:
            logging.exception("failed to remove reply_markup from answer")
    msg_id = await get_menu_msg(uid)
    if msg_id:
        try:
            await ctx.bot.edit_message_reply_markup(
                chat_id=chat.id, message_id=msg_id, reply_markup=None
            )
        except Exception:
            logging.exception("failed to remove reply_markup from menu")

async def send_menu(chat, uid: int, text: str, kb: Mk, ctx: ContextTypes.DEFAULT_TYPE):
    """Показать меню, редактируя существующее сообщение."""
    msg_id = await get_menu_msg(uid)
    if msg_id:
        try:
            await ctx.bot.edit_message_text(
                chat_id=chat.id, message_id=msg_id, text=text, reply_markup=kb
            )
            return
        except Exception:
            logging.exception("failed to edit existing menu message")
    msg = await chat.send_message(text, reply_markup=kb)
    await set_menu_msg(uid, msg.message_id)
    return msg

def kb_main(free_txt: bool, free_ph: int, credits: int) -> Mk:
    """Главное меню."""
    return Mk(
        [
            [Btn(f"📝 Текстовый вопрос ({'1' if free_txt else '0'})", callback_data="ask_text")],
            [Btn(f"📷 Фото + вопрос ({free_ph})", callback_data="ask_photo")],
            [Btn(f"💳 Пополнить счёт ({credits})", callback_data="pay")],
            [Btn("🎁 Пригласить друга", callback_data="ref")],
            [Btn("ℹ️ Как это работает", callback_data="how")],
        ]
    )


def kb_ref(link: str) -> Mk:
    return Mk(
        [
            [Btn("📋 Скопировать ссылку", copy_text=CopyTextButton(link))],
            [Btn("📤 Отправить другу", url=f"https://t.me/share/url?url={link}")],
            [Btn("◀️ Назад", callback_data="back")],
        ]
    )

async def show_main(chat, uid: int, ctx: ContextTypes.DEFAULT_TYPE):
    kb = kb_main(
        await can_use_daily_text(uid),
        await get_free_photo(uid),
        "∞" if uid in ADMINS else await get_credits(uid),
    )
    await send_menu(chat, uid, MAIN_TEXT, kb, ctx)

# ── Команда /start ───────────────────────────────────
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    logging.info("command %s from %s", update.message.text, uid)
    if ctx.args:
        arg = ctx.args[0]
        parent = 0
        alias_mode = False
        try:
            parent = int(arg)
        except ValueError:
            parent = await get_uid_by_alias(arg)
            alias_mode = parent > 0
        if parent and parent != uid:
            await set_ref_parent(uid, parent)
            await grant_referral_bonus(parent)
            link = f"tg://user?id={uid}"
            try:
                text = (
                    f"По вашей ссылке зарегистрировался "
                    f"<a href=\"{link}\">{uid}</a>"
                )
                await ctx.bot.send_message(parent, text, parse_mode=ParseMode.HTML)
            except Exception:
                logging.exception("failed to notify referrer")
            if alias_mode:
                await set_free_photo(uid, 2)
    await grant_start_bonus(uid)
    await show_main(update.message.chat, uid, ctx)
    ctx.user_data["stage"] = "main"

async def send_profile(chat, uid: int, ctx: ContextTypes.DEFAULT_TYPE):
    credits = "∞" if uid in ADMINS else await get_credits(uid)
    tc, pc = await get_counts(uid)
    refs = await get_ref_count(uid)
    msg = (
        f"Баланс: {credits} баллов\n"
        f"Вопросов: {tc}\n"
        f"Фотоанализов: {pc}\n"
        f"Друзей по ссылке: {refs}"
    )
    await send_menu(chat, uid, msg, KB_BACK, ctx)

async def cmd_profile(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    logging.info("command %s from %s", update.message.text, uid)
    await send_profile(update.message.chat, uid, ctx)
    ctx.user_data["stage"] = "profile"

# ── Callback router ──────────────────────────────────
async def router(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    data = q.data
    logging.info("callback %s from %s", data, uid)

    if data == "ask_text":
        ctx.user_data["stage"] = "await_text"
        await send_menu(q.message.chat, uid, "Введи вопрос…", KB_BACK, ctx)
    elif data == "ask_photo":
        ctx.user_data["stage"] = "await_photo"
        await send_menu(q.message.chat, uid, "Пришли фото…", KB_BACK, ctx)
    elif data == "back":
        if ctx.user_data.get("stage") == "answer":
            try:
                await q.message.edit_reply_markup(None)
            except Exception:
                logging.exception("failed to remove answer markup")
            ctx.user_data.pop("ans_msg_id", None)
        ctx.user_data["stage"] = "main"
        await show_main(q.message.chat, uid, ctx)
    elif data == "pay":
        kb_rows = []
        for c, price in PRICE_PACKS_RUB.items():
            stars = PRICE_PACKS_STARS[c]
            kb_rows.append(
                [
                    Btn(f"{price/100:.0f}₽ → {c} баллов", callback_data=f"buy_{c}_card"),
                    Btn(f"{stars}⭐ → {c} баллов", callback_data=f"buy_{c}_stars"),
                ]
            )
        kb_rows.append([Btn("◀️ Назад", callback_data="back")])
        await send_menu(q.message.chat, uid, "Выбери пакет пополнения", Mk(kb_rows), ctx)
        ctx.user_data["stage"] = "pay"
    elif data == "ref":
        alias = await get_alias(uid)
        bot_name = ctx.bot.username or (await ctx.bot.get_me()).username
        link = f"https://t.me/{bot_name}?start={alias or uid}"
        refs = await get_ref_count(uid)
        msg = f"Друзей по ссылке: {refs}"
        await send_menu(q.message.chat, uid, msg, kb_ref(link), ctx)
        ctx.user_data["stage"] = "ref"
    elif data == "how":
        await send_menu(q.message.chat, uid, HOW_IT_WORKS, KB_BACK, ctx)
        ctx.user_data["stage"] = "how"
    elif data.startswith("buy_"):
        await clear_menu(q.message.chat, uid, ctx)
        _, c, method = data.split("_")
        credits = int(c)
        if method == "card":
            price = PRICE_PACKS_RUB[credits]
            currency = "RUB"
            token = YK_TOKEN
        else:
            price = PRICE_PACKS_STARS[credits]
            currency = "XTR"
            token = ""
        await ctx.bot.send_invoice(
            chat_id=q.message.chat_id,
            title=f"{credits} баллов",
            description="Пакет баллов",
            payload=f"buy_{credits}_{method}",
            provider_token=token,
            currency=currency,
            prices=[LabeledPrice(f"{credits} баллов", price)],
            reply_markup=KB_PAY,
        )

# ── Message handlers ─────────────────────────────────
async def handle_text(update: Update, ctx):
    uid = update.effective_user.id
    stage = ctx.user_data.get("stage")
    text = update.message.text

    if stage == "await_text":
        await clear_menu(update.message.chat, uid, ctx)
        if await can_use_daily_text(uid):
            await mark_daily_text(uid)
        else:
            if uid not in ADMINS:
                try:
                    await dec_credits(uid, TEXT_COST)
                except ValueError:
                    kb_topup = Mk(
                        [
                            [Btn("Пополнить счёт", callback_data="pay")],
                            [Btn("◀️ Назад", callback_data="back")],
                        ]
                    )
                    await send_menu(update.message.chat, uid, "Нет баллов", kb_topup, ctx)
                    ctx.user_data["stage"] = "main"
                    return
        wait = await update.message.reply_text("Сейчас решим по твоему вопросу… ⏳")
        ans = clean_md(await ask_deepseek(text))
        await wait.delete()
        max_len = 1200 - len(FOOTER)
        msg = await update.message.reply_text(ans[:max_len] + FOOTER, reply_markup=KB_BACK)
        await inc_text_cnt(uid)
        ctx.user_data["ans_msg_id"] = msg.message_id
        ctx.user_data["stage"] = "answer"

    elif stage == "await_photo_text":
        await clear_menu(update.message.chat, uid, ctx)
        photo = ctx.user_data.pop("photo", None)
        if photo is None:
            await update.message.reply_text("Сначала пришли фото")
            return
        free = await get_free_photo(uid)
        if free > 0:
            await use_free_photo(uid)
        else:
            if uid not in ADMINS:
                try:
                    await dec_credits(uid, PHOTO_COST)
                except ValueError:
                    kb_topup = Mk(
                        [
                            [Btn("Пополнить счёт", callback_data="pay")],
                            [Btn("◀️ Назад", callback_data="back")],
                        ]
                    )
                    await send_menu(update.message.chat, uid, "Нет баллов", kb_topup, ctx)
                    ctx.user_data["stage"] = "main"
                    return
        wait = await update.message.reply_text("Сейчас решим по твоему вопросу… ⏳")
        ans = clean_md(await analyze_photo(photo, text))
        await wait.delete()
        max_len = 1200 - len(FOOTER)
        msg = await update.message.reply_text(ans[:max_len] + FOOTER, reply_markup=KB_BACK)
        await inc_photo_cnt(uid)
        ctx.user_data["ans_msg_id"] = msg.message_id
        ctx.user_data["stage"] = "answer"

    else:
        await update.message.reply_text("Используй меню")

async def handle_photo(update: Update, ctx):
    uid = update.effective_user.id
    if ctx.user_data.get("stage") != "await_photo":
        await update.message.reply_text("Сначала выбери режим фото в меню")
        return
    await clear_menu(update.message.chat, uid, ctx)
    photo = update.message.photo[-1]
    f = await ctx.bot.get_file(photo.file_id)
    bio = io.BytesIO()
    await f.download_to_memory(out=bio)
    bio.seek(0)
    img = Image.open(bio)
    img.thumbnail((1024, 1024))
    out = io.BytesIO()
    img.save(out, format="JPEG")
    ctx.user_data["photo"] = out.getvalue()
    await send_menu(update.message.chat, uid, "Опиши ситуацию текстом", KB_BACK, ctx)
    ctx.user_data["stage"] = "await_photo_text"

# ── Оплата ───────────────────────────────────────────
async def pre_checkout(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.pre_checkout_query.answer(True)


async def paid(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    payload = update.message.successful_payment.invoice_payload
    parts = payload.split("_")
    credits = int(parts[1])
    method = parts[2]
    uid = update.effective_user.id
    await add_credits(uid, credits)
    if method == "card":
        await log_payment(uid, rub=PRICE_PACKS_RUB[credits] // 100)
    else:
        await log_payment(uid, stars=PRICE_PACKS_STARS[credits])
    total = "∞" if uid in ADMINS else await get_credits(uid)
    await update.message.reply_text(f"✅ Платёж прошёл! На счету {total} баллов")
    ctx.user_data["stage"] = "main"
    await show_main(update.message.chat, uid, ctx)

# ── Error handler ────────────────────────────────────
async def on_error(update: object, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        raise ctx.error
    except NetworkError:
        logging.exception("network error")
        if update and getattr(update, "effective_chat", None):
            await update.effective_chat.send_message("⛔ Временно нет связи")
    except RetryAfter as e:
        await asyncio.sleep(e.retry_after)
        if update:
            await ctx.application.process_update(update)
    except Exception:
        logging.exception("unknown error")
        if update and getattr(update, "effective_chat", None):
            await update.effective_chat.send_message("⚠️ Неизвестная ошибка")

async def grant(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    logging.info("command %s from %s", update.message.text, uid)
    if uid not in ADMINS:
        return
    try:
        target = int(ctx.args[0])
        qty = int(ctx.args[1])
    except (IndexError, ValueError):
        await update.message.reply_text("Usage: /grant <id> <qty>")
        return
    await add_credits(target, qty)
    await update.message.reply_text("OK")

async def decredits(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    logging.info("command %s from %s", update.message.text, uid)
    if uid not in ADMINS:
        return
    try:
        target = int(ctx.args[0])
        qty = int(ctx.args[1])
    except (IndexError, ValueError):
        await update.message.reply_text("Usage: /decredits <id> <qty>")
        return
    try:
        await dec_credits(target, qty)
    except ValueError:
        await update.message.reply_text("Not enough")
        return
    await update.message.reply_text("OK")

async def cmd_alias(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    logging.info("command %s from %s", update.message.text, uid)
    try:
        name = ctx.args[0]
    except IndexError:
        await update.message.reply_text("Usage: /alias <name>")
        return
    ok = await set_alias(uid, name)
    if ok:
        bot_name = ctx.bot.username or (await ctx.bot.get_me()).username
        link = f"https://t.me/{bot_name}?start={name}"
        await update.message.reply_text(
            "Нажми кнопку, чтобы скопировать ссылку",
            reply_markup=kb_ref(link),
        )
    else:
        await update.message.reply_text("Имя занято")

async def stats_referrals(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    logging.info("command %s from %s", update.message.text, uid)
    if uid not in ADMINS:
        return
    data = await get_referral_stats()
    await update.message.reply_text(json.dumps(data, ensure_ascii=False))

async def stats_alias(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    logging.info("command %s from %s", update.message.text, uid)
    alias = await get_alias(uid)
    if not alias:
        await update.message.reply_text("У вас нет alias")
        return
    cnt, rub, stars = await get_alias_stats(uid)
    data = {"alias": alias, "count": cnt, "rub": rub, "stars": stars}
    await update.message.reply_text(json.dumps(data, ensure_ascii=False))

async def stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    logging.info("command %s from %s", update.message.text, uid)
    if uid not in ADMINS:
        return
    users = len(await get_all_uids())
    await update.message.reply_text(f"Всего пользователей: {users}")

async def broadcast(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    logging.info("command %s from %s", update.message.text, uid)
    if uid not in ADMINS:
        return
    if not ctx.args:
        await update.message.reply_text("Usage: /broadcast <text>")
        return
    text = " ".join(ctx.args)
    for uid_ in await get_all_uids():
        try:
            await ctx.bot.send_message(uid_, text)
        except Exception:
            logging.exception("broadcast to %s failed", uid_)

# ── Запуск ───────────────────────────────────────────
def main() -> None:
    """Запустить бота в единственном цикле событий.

    Application.run_polling самостоятельно создаёт и управляет event loop.
    Чтобы избежать ошибки "event loop is already running", не оборачиваем
    вызов run_polling() в asyncio.run().
    """

    # Подготовка БД выполняется один раз перед запуском приложения.
    asyncio.run(create_tables())
    # asyncio.run() закрывает текущий event loop. При последующем вызове
    # Application.run_polling() в Python 3.11 это приводит к ошибке
    # "There is no current event loop". Создаём и устанавливаем новый цикл
    # событий, чтобы run_polling() мог им воспользоваться.
    asyncio.set_event_loop(asyncio.new_event_loop())

    logging.basicConfig(
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        level=logging.WARNING,
        stream=sys.stdout,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .concurrent_updates(64)
        .rate_limiter(AIORateLimiter())
        .job_queue(JobQueue())
        .request(HTTPXRequest(connection_pool_size=1024))
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("profile", cmd_profile))
    app.add_handler(CommandHandler("grant", grant))
    app.add_handler(CommandHandler("decredits", decredits))
    app.add_handler(CommandHandler("alias", cmd_alias))
    app.add_handler(CommandHandler("stats_referrals", stats_referrals))
    app.add_handler(CommandHandler("stats_alias", stats_alias))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("broadcast", broadcast))
    app.add_handler(CallbackQueryHandler(router))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(PreCheckoutQueryHandler(pre_checkout))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, paid))
    app.add_error_handler(on_error)

    # run_polling() блокирует поток, пока бот не будет остановлен.
    app.run_polling()


if __name__ == "__main__":
    main()
