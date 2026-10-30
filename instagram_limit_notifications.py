"""Admin-only quota notices. Meta secrets and message bodies are never read here."""
from datetime import datetime, timedelta, timezone
import json
import logging
import time

from telegram import InlineKeyboardButton as Btn, InlineKeyboardMarkup as Mk
from telegram.error import TelegramError

import db
import instagram_automation as campaign

logger = logging.getLogger(__name__)
SUBSCRIBERS_KEY = "instagram_limit_subscribers"
KINDS = {"comments", "messages", "send", "connection"}
LABELS = {
    "comments": "Получение комментариев", "messages": "Чтение Direct",
    "send": "Отправка автоответов", "connection": "Проверка подключения Meta",
    "private_reply": "Первый личный ответ на комментарий", "comment_reply": "Ответ под комментарием",
    "direct_message": "Личное сообщение", "media_list": "Проверка списка публикаций",
    "comment_list": "Получение комментариев", "comment_detail": "Проверка комментария перед ответом",
    "message_list": "Чтение Direct", "conversation_list": "Поиск переписки", "thread_owner": "Проверка управления перепиской",
}


def local_time(stamp):
    return datetime.fromtimestamp(float(stamp), timezone(timedelta(hours=7))).strftime("%d.%m.%Y %H:%M") + " (Бангкок, UTC+7)"


async def recipients(admin_ids):
    raw = await db.get_setting(SUBSCRIBERS_KEY)
    if raw is None:
        return set(admin_ids) if len(admin_ids) == 1 else set()
    try:
        values = json.loads(raw)
        return {uid for uid in values if type(uid) is int and uid in admin_ids} if isinstance(values, list) else set()
    except (ValueError, TypeError):
        return set()


async def toggle_recipient(uid, admin_ids):
    if uid not in admin_ids:
        return False
    values = await recipients(admin_ids)
    values.symmetric_difference_update({uid})
    await db.set_admin_setting(SUBSCRIBERS_KEY, json.dumps(sorted(values)), uid)
    return uid in values


def relevant_notices(status):
    return [item for item in status.get("quota_notices", []) if isinstance(item, dict)
            and item.get("kind") in KINDS and isinstance(item.get("episode"), str)
            and len(item["episode"]) == 32
            and not (status.get("delivery_mode") == "direct_link" and item["kind"] == "messages")]


def limit_text(notice, recovered=False):
    operation = LABELS.get(notice.get("operation"), LABELS[notice["kind"]])
    if recovered:
        return ("✅ Instagram «Биомозг»: Meta снова разрешила запрос\n\n"
                + operation + " — успешный запрос подтверждён.\n"
                "Подходящие ожидающие ответы продолжатся автоматически; пауза в админ-панели сохраняется.")
    impact = {
        "comments": "Получение новых комментариев временно ограничено. Ответы по ним могут задержаться.",
        "messages": "Чтение ответов в Direct ограничено. В двухшаговом режиме ссылка после подтверждения может задержаться.",
        "send": "Автоответы временно приостановлены. Ожидающие ответы сохранены в очереди.",
        "connection": "Meta ограничила проверку подключения. Автоответы ждут подтверждения доступа.",
    }[notice["kind"]]
    text = "⚠️ Instagram «Биомозг»: лимит Meta\n\n" + operation + ".\n" + impact
    if notice.get("provider_retry_at", 0) > 0:
        text += "\n\nMeta просит подождать до " + local_time(notice["provider_retry_at"]) + "."
    elif notice.get("estimated_regain_at", 0) > 0:
        text += "\n\nОценка Meta: примерно до " + local_time(notice["estimated_regain_at"]) + ". Время может измениться."
    else:
        text += "\n\nMeta не указала точный срок снятия ограничения."
    text += "\nСледующая автоматическая проверка: " + local_time(notice["next_check_at"]) + "."
    text += "\n\nПока проверьте комментарии и ответьте людям вручную через Instagram. О восстановлении сообщу отдельно."
    return text


def status_text(status):
    text = ("Лимиты Instagram\n\nПервые личные ответы на комментарии публикаций/Reels: до 750 API-вызовов в час на аккаунт."
            " Дополнительно действуют общие квоты Meta.\n"
            "Подтверждённых первых Direct нашей системы за последний час: " + str(status.get("private_replies_last_hour", 0))
            + ". Это счётчик отправок; точный остаток квоты Meta не предоставляет.\n")
    items = relevant_notices(status)
    if not items:
        text += "\nОтказов из-за лимитов по активным операциям не зарегистрировано."
    for item in items:
        text += "\n\n" + LABELS.get(item.get("operation"), LABELS[item["kind"]]) + ": "
        if item.get("cleared_at"):
            text += "успешный запрос подтверждён " + local_time(item["cleared_at"]) + "."
        else:
            text += "ограничение обнаружено. "
            text += ("Следующая проверка " + local_time(item["next_check_at"]) + ".") if item["next_check_at"] > time.time() else "Восстановление ещё не подтверждено."
            if not item.get("provider_retry_at") and not item.get("estimated_regain_at"):
                text += " Meta не указала срок снятия ограничения."
    return text


def buttons():
    return Mk([[Btn("Instagram · настройки", callback_data="admin:instagram:limits")],
               [Btn("Открыть Instagram", url="https://www.instagram.com/biomozg/")]])


async def monitor(ctx, admin_ids):
    status = campaign.connection_status()
    if time.time() - float(status.get("updated_at", 0)) > 120:
        return
    targets = await recipients(admin_ids)
    for notice in relevant_notices(status):
        recovered = bool(notice.get("cleared_at"))
        if recovered and notice["cleared_at"] < time.time() - 86400:
            continue
        phase = "recovered" if recovered else "limited"
        message = limit_text(notice, recovered)
        for uid in targets:
            key = "instagram-quota:" + str(uid) + ":" + notice["kind"] + ":" + notice["episode"] + ":" + phase
            if not await db.claim_notification(key, uid):
                continue
            try:
                await ctx.bot.send_message(chat_id=uid, text=message,
                                           reply_markup=buttons(), disable_web_page_preview=True)
                await db.mark_notification_sent(key)
            except TelegramError:
                # Delivery may have happened. A persisted claim prevents blind duplicates.
                logger.warning("Instagram quota notification delivery unconfirmed")


async def send_test(ctx, uid):
    await ctx.bot.send_message(chat_id=uid, text="🔔 Проверка уведомлений Instagram\n\n"
        "Уведомления о лимитах будут приходить сюда. При ограничении сообщу затронутую операцию и время следующей проверки, после успешного восстановления — отдельно.\n\n"
        "Это тестовое сообщение; оно не означает, что автоответы остановлены.", reply_markup=buttons(), disable_web_page_preview=True)
