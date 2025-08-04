# -*- coding: utf-8 -*-
"""Простой бот «Решало».

Удалены функции тарологии и номерологии. Бот отвечает на текстовые
вопросы через DeepSeek и может анализировать фотографию через GPT‑4o.
"""

import os, io, logging, asyncio
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)
from utils.llm import ask_deepseek, analyze_photo
from db import (
    get_credits,
    dec_credits,
    can_use_daily_text,
    mark_text_used,
    get_photo_credits,
    dec_photo_credit,
    set_referral,
)

# ── Настройки ─────────────────────────────────────────
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_IDS = {int(x) for x in os.getenv("ADMINS", "").split(",") if x}

TEXT_COST = 10
PHOTO_COST = 20
PACKS_RUB = {100: 100_00, 350: 300_00, 600: 500_00}
PACKS_STARS = {100: 56, 350: 168, 600: 280}

logging.basicConfig(format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO)
log = logging.getLogger(__name__)

MAIN_TEXT = (
    "Я — Решало. Отправь текстовый вопрос или фото с подписью, и я подскажу, как действовать.\n"
    "Каждый день 1 бесплатный текстовый вопрос, фото стоит 20 баллов (1 бесплатно при регистрации)."
)

HOW_TEXT = (
    "1️⃣ Отправь текстовый вопрос или фото с подписью.\n"
    "2️⃣ Получи краткий совет от Решалы."
)

REF_TEXT = (
    "Пригласи друзей по ссылке https://t.me/reshalabot?start={id} и получи бонусы."
)

MAIN_KB = InlineKeyboardMarkup(
    [
        [
            InlineKeyboardButton("Баланс", callback_data="balance"),
            InlineKeyboardButton("Купить", callback_data="buy"),
        ],
        [
            InlineKeyboardButton("Как это работает", callback_data="how"),
            InlineKeyboardButton("Рефералы", callback_data="ref"),
        ],
    ]
)

BACK_KB = InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад", callback_data="back")]])


def build_balance_text(uid: int) -> str:
    credits = get_credits(uid, ADMIN_IDS)
    free_text = "✅" if can_use_daily_text(uid) else "❌"
    photos = get_photo_credits(uid)
    return (
        f"Баланс: {credits} баллов\n"
        f"Свободный текст сегодня: {free_text}\n"
        f"Свободные фотоанализы: {photos}"
    )


def build_buy_text() -> str:
    return (
        "Пакеты пополнения:\n"
        "100₽ → 100 баллов (/pay_100)\n"
        "300₽ → 350 баллов (/pay_350)\n"
        "500₽ → 600 баллов (/pay_600)\n"
        "Оплата звёздами поддерживается вручную."
    )

# ── Команды ──────────────────────────────────────────
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    if ctx.args:
        try:
            parent = int(ctx.args[0])
            set_referral(uid, parent)
        except ValueError:
            pass
    await update.message.reply_text(MAIN_TEXT, reply_markup=MAIN_KB)
    ctx.user_data["stage"] = "main"


async def balance(update: Update, ctx):
    uid = update.effective_user.id
    await update.message.reply_text(build_balance_text(uid))


async def buy(update: Update, ctx):
    await update.message.reply_text(build_buy_text())


# ── Обработка текстов и фото ─────────────────────────
async def handle_text(update: Update, ctx):
    uid = update.effective_user.id
    text = update.message.text
    if can_use_daily_text(uid):
        mark_text_used(uid)
    else:
        if get_credits(uid, ADMIN_IDS) < TEXT_COST:
            await update.message.reply_text("Недостаточно баллов. /buy")
            return
        dec_credits(uid, TEXT_COST, ADMIN_IDS)
    await update.message.chat.send_action("typing")
    try:
        resp = await asyncio.to_thread(ask_deepseek, text)
    except Exception as e:
        log.exception("deepseek error")
        await update.message.reply_text("Ошибка запроса.")
        return
    sent = await update.message.reply_text(resp)
    try:
        await update.message.delete()
    except Exception:
        pass


async def handle_photo(update: Update, ctx):
    uid = update.effective_user.id
    photo = update.message.photo[-1]
    caption = update.message.caption or ""
    if not caption:
        await update.message.reply_text("Добавь подпись с описанием ситуации.")
        return
    bio = io.BytesIO()
    file = await photo.get_file()
    await file.download(out=bio)
    bio.seek(0)
    if get_photo_credits(uid) > 0:
        dec_photo_credit(uid)
    else:
        if get_credits(uid, ADMIN_IDS) < PHOTO_COST:
            await update.message.reply_text("Недостаточно баллов. /buy")
            return
        dec_credits(uid, PHOTO_COST, ADMIN_IDS)
    await update.message.chat.send_action("typing")
    try:
        resp = await asyncio.to_thread(analyze_photo, bio.getvalue(), caption)
    except Exception:
        log.exception("vision error")
        await update.message.reply_text("Ошибка обработки фото.")
        return
    sent = await update.message.reply_text(resp)
    try:
        await update.message.delete()
    except Exception:
        pass


async def on_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    data = query.data
    uid = query.from_user.id
    if data == "balance":
        await query.message.edit_text(build_balance_text(uid), reply_markup=BACK_KB)
        ctx.user_data["stage"] = "balance"
    elif data == "buy":
        await query.message.edit_text(build_buy_text(), reply_markup=BACK_KB)
        ctx.user_data["stage"] = "buy"
    elif data == "how":
        await query.message.edit_text(HOW_TEXT, reply_markup=BACK_KB)
        ctx.user_data["stage"] = "how"
    elif data == "ref":
        await query.message.edit_text(REF_TEXT.format(id=uid), reply_markup=BACK_KB)
        ctx.user_data["stage"] = "ref"
    elif data == "back":
        await query.message.edit_text(MAIN_TEXT, reply_markup=MAIN_KB)
        ctx.user_data["stage"] = "main"


# ── main ────────────────────────────────────────────
def main() -> None:
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handlers(
        [
            CommandHandler("start", start),
            CommandHandler("balance", balance),
            CommandHandler("buy", buy),
            CallbackQueryHandler(on_button),
            MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text),
            MessageHandler(filters.PHOTO, handle_photo),
        ]
    )
    log.info("bot started")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
