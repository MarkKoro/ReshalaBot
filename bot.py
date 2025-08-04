# -*- coding: utf-8 -*-
"""Простой бот «Решало».

Удалены функции тарологии и номерологии. Бот отвечает на текстовые
вопросы через DeepSeek и может анализировать фотографию через GPT‑4o.
"""

import os, io, logging, asyncio
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
from utils.llm import ask_deepseek, analyze_photo
from db import (
    get_credits,
    add_credits,
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

# ── Команды ──────────────────────────────────────────
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    if ctx.args:
        try:
            parent = int(ctx.args[0])
            set_referral(uid, parent)
        except ValueError:
            pass
    msg = (
        "Я — Решало. Отправь текстовый вопрос или фото с подписью, и я подскажу, как действовать.\n"
        "Каждый день 1 бесплатный текстовый вопрос, фото стоит 20 баллов (1 бесплатно при регистрации)."
    )
    await update.message.reply_text(msg)


async def balance(update: Update, ctx):
    uid = update.effective_user.id
    credits = get_credits(uid, ADMIN_IDS)
    free_text = "✅" if can_use_daily_text(uid) else "❌"
    photos = get_photo_credits(uid)
    text = f"Баланс: {credits} баллов\nСвободный текст сегодня: {free_text}\nСвободные фотоанализы: {photos}"
    await update.message.reply_text(text)


async def buy(update: Update, ctx):
    text = (
        "Пакеты пополнения:\n"
        "100₽ → 100 баллов (/pay_100)\n"
        "300₽ → 350 баллов (/pay_350)\n"
        "500₽ → 600 баллов (/pay_600)\n"
        "Оплата звёздами поддерживается вручную."
    )
    await update.message.reply_text(text)


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
    await update.message.reply_text(resp)


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
    await update.message.reply_text(resp)


# ── main ────────────────────────────────────────────
def main() -> None:
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handlers(
        [
            CommandHandler("start", start),
            CommandHandler("balance", balance),
            CommandHandler("buy", buy),
            MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text),
            MessageHandler(filters.PHOTO, handle_photo),
        ]
    )
    log.info("bot started")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
