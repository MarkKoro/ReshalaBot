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

SUPPORT_TEXT = "Если возникли сложности — пиши @support"

BACK_KB = InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад", callback_data="back")]])


def build_main_kb(uid: int) -> InlineKeyboardMarkup:
    """Главное меню с динамическими подсказками."""
    free_text = 1 if can_use_daily_text(uid) else 0
    photos = get_photo_credits(uid)
    credits = get_credits(uid, ADMIN_IDS)
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    f"Текстовый запрос ({free_text})", callback_data="ask_text"
                )
            ],
            [
                InlineKeyboardButton(
                    f"Фото запрос ({photos})", callback_data="ask_photo"
                )
            ],
            [
                InlineKeyboardButton(
                    f"Пополнить счёт ({credits})", callback_data="buy"
                )
            ],
            [InlineKeyboardButton("Как это работает", callback_data="how")],
            [InlineKeyboardButton("Пригласить друга", callback_data="ref")],
            [InlineKeyboardButton("Техподдержка", callback_data="support")],
        ]
    )


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
    await update.message.reply_text(MAIN_TEXT, reply_markup=build_main_kb(uid))
    ctx.user_data["stage"] = "main"


async def balance(update: Update, ctx):
    uid = update.effective_user.id
    await update.message.reply_text(build_balance_text(uid))


async def buy(update: Update, ctx):
    await update.message.reply_text(build_buy_text())


# ── Обработка текстов и фото ─────────────────────────
async def handle_text(update: Update, ctx):
    uid = update.effective_user.id
    stage = ctx.user_data.get("stage")
    text = update.message.text

    # Завершение анализа фото (этап текста)
    if stage == "await_photo_text":
        photo_bytes = ctx.user_data.pop("photo_bytes", None)
        if not photo_bytes:
            await update.message.reply_text("Сначала пришлите фото.")
            return
        if get_photo_credits(uid) > 0:
            dec_photo_credit(uid)
        else:
            if get_credits(uid, ADMIN_IDS) < PHOTO_COST:
                await update.message.reply_text("Недостаточно баллов. /buy")
                return
            dec_credits(uid, PHOTO_COST, ADMIN_IDS)
        await update.message.chat.send_action("typing")
        try:
            resp = await asyncio.to_thread(analyze_photo, photo_bytes, text)
        except Exception:
            log.exception("vision error")
            await update.message.reply_text("Ошибка обработки фото.")
            return
        await update.message.reply_text(resp)
        ctx.user_data["stage"] = "main"
        await update.message.chat.send_message(
            MAIN_TEXT, reply_markup=build_main_kb(uid)
        )
        try:
            await update.message.delete()
        except Exception:
            pass
        return

    # Обычный текстовый запрос
    if stage != "await_text":
        await update.message.reply_text(
            "Используйте кнопку ‘Текстовый запрос’ в меню."
        )
        return
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
    except Exception:
        log.exception("deepseek error")
        await update.message.reply_text("Ошибка запроса.")
        return
    await update.message.reply_text(resp)
    ctx.user_data["stage"] = "main"
    await update.message.chat.send_message(
        MAIN_TEXT, reply_markup=build_main_kb(uid)
    )
    try:
        await update.message.delete()
    except Exception:
        pass


async def handle_photo(update: Update, ctx):
    uid = update.effective_user.id
    stage = ctx.user_data.get("stage")
    if stage != "await_photo":
        await update.message.reply_text(
            "Сначала выберите ‘Фото запрос’ в меню."
        )
        return
    photo = update.message.photo[-1]
    bio = io.BytesIO()
    file = await photo.get_file()
    await file.download(out=bio)
    bio.seek(0)
    ctx.user_data["photo_bytes"] = bio.getvalue()
    ctx.user_data["stage"] = "await_photo_text"
    await update.message.reply_text("Теперь напишите проблему.")
    try:
        await update.message.delete()
    except Exception:
        pass


async def on_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    data = query.data
    uid = query.from_user.id
    if data == "ask_text":
        await query.message.delete()
        credits = get_credits(uid, ADMIN_IDS)
        free = 1 if can_use_daily_text(uid) else 0
        msg = (
            "Рассказывай, в чем проблема, я её решу.\n"
            f"Стоимость: {TEXT_COST} баллов. Баланс: {credits}.\n"
            f"Бесплатных текстов сегодня: {free}.\n"
            "Напишите текст снизу."
        )
        await query.message.chat.send_message(msg, reply_markup=BACK_KB)
        ctx.user_data["stage"] = "await_text"
    elif data == "ask_photo":
        await query.message.delete()
        credits = get_credits(uid, ADMIN_IDS)
        photos = get_photo_credits(uid)
        msg = (
            "Пришли фотографию, я помогу разобраться.\n"
            f"Стоимость: {PHOTO_COST} баллов. Баланс: {credits}.\n"
            f"Бесплатных фото: {photos}.\n"
            "Присылай фотографию."
        )
        await query.message.chat.send_message(msg, reply_markup=BACK_KB)
        ctx.user_data["stage"] = "await_photo"
    elif data == "buy":
        await query.message.edit_text(build_buy_text(), reply_markup=BACK_KB)
        ctx.user_data["stage"] = "buy"
    elif data == "how":
        await query.message.edit_text(HOW_TEXT, reply_markup=BACK_KB)
        ctx.user_data["stage"] = "how"
    elif data == "ref":
        await query.message.edit_text(REF_TEXT.format(id=uid), reply_markup=BACK_KB)
        ctx.user_data["stage"] = "ref"
    elif data == "support":
        await query.message.edit_text(SUPPORT_TEXT, reply_markup=BACK_KB)
        ctx.user_data["stage"] = "support"
    elif data == "back":
        await query.message.edit_text(
            MAIN_TEXT, reply_markup=build_main_kb(uid)
        )
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
