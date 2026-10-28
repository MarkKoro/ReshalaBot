"""Shared Instagram campaign settings. No Telegram, payment or Meta credentials."""
from __future__ import annotations

import copy
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import tempfile
import time
import unicodedata
from urllib.parse import urlsplit

CONFIG_PATH = Path(os.getenv("INSTAGRAM_CONFIG_PATH", Path(__file__).with_name("instagram-settings.json")))
STATUS_PATH = Path(os.getenv("INSTAGRAM_STATUS_PATH", Path(__file__).with_name("instagram-status.json")))
SETTING_KEY = "instagram_automation"
OPT_IN = "BIOMOZG_LINK_OPTIN"
DEFAULTS = {
    "enabled": False,
    "keywords": ["креатив"],
    "target_url": "https://t.me/BioMozg_agent",
    "opening": [
        "Бесплатное видео «Биомозга» — в Telegram. Прислать ссылку?",
        "Могу отправить бесплатное видео «Биомозга». Ссылка ведёт в Telegram-бота — прислать?",
        "В «Биомозге» есть бесплатное видео про креатив. Отправить ссылку?",
    ],
    "link_text": [
        "Держи бесплатное видео «Биомозга». Нажми кнопку ниже, а в Telegram — «Запустить».",
        "Вот ссылка на бесплатное видео. Открой бота и нажми «Запустить» — видео придёт туда.",
        "Готово! Видео ждёт в Telegram-боте. Перейди по кнопке и нажми «Запустить».",
    ],
    "public_reply": [
        "Ответ уже в личных сообщениях. Если не видно — загляни в «Запросы».",
        "Написали в Direct. Проверь также папку «Запросы».",
        "Загляни в личные сообщения — там первый шаг к бесплатному видео.",
    ],
    "public_reply_enabled": True,
    "opt_in_label": "Да, хочу видео",
    "link_label": "Получить видео",
}
TEXT_LABELS = {
    "opening": "Первое сообщение в Direct",
    "link_text": "Сообщение со ссылкой",
    "public_reply": "Ответ под комментарием",
    "opt_in_label": "Кнопка подтверждения",
    "link_label": "Кнопка со ссылкой",
}


def words(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).casefold().replace("ё", "е")
    return " ".join(re.findall(r"[^\W_]+", value, flags=re.UNICODE))


def parse_keywords(raw: str) -> list[str]:
    result = []
    for item in re.split(r"[,;\n]+", raw):
        item = words(item)
        if not item:
            continue
        if len(item) > 40 or len(item.split()) > 5:
            raise ValueError("Ключевое слово или фраза: до 40 знаков и 5 слов.")
        if item not in result:
            result.append(item)
    if len(result) > 30:
        raise ValueError("Можно сохранить до 30 ключевых слов или фраз.")
    return result


def matches(text: str, keywords: list[str]) -> bool:
    searchable = " " + words(text) + " "
    return any(" " + word + " " in searchable for word in keywords)


def validate_url(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("Нужна публичная https-ссылка.")
    value = value.strip()
    try:
        parsed = urlsplit(value)
        host = (parsed.hostname or "").rstrip(".").lower()
        if (parsed.scheme != "https" or not host or "." not in host
                or parsed.username or parsed.password or parsed.port not in (None, 443)
                or re.search(r"[\s\x00-\x1f\x7f]", value) or len(value) > 1000
                or host.endswith((".local", ".localhost", ".internal", ".test"))):
            raise ValueError
        try:
            ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            raise ValueError
        host.encode("idna")
    except (ValueError, UnicodeError):
        raise ValueError("Нужна публичная https-ссылка без пароля и нестандартного порта.") from None
    return value


def variants(raw: str, limit: int = 600) -> list[str]:
    result = [part.strip() for part in re.split(r"(?m)^\s*---\s*$", raw) if part.strip()]
    if not 1 <= len(result) <= 3 or any(len(part) > limit for part in result):
        raise ValueError(f"Нужно от 1 до 3 вариантов, каждый до {limit} знаков. Разделитель: --- на отдельной строке.")
    return result


def validate(settings: dict) -> dict:
    if not isinstance(settings, dict):
        raise ValueError("Некорректные настройки Instagram.")
    result = copy.deepcopy(DEFAULTS)
    for key in result:
        if key in settings:
            result[key] = copy.deepcopy(settings[key])
    for key in ("enabled", "public_reply_enabled"):
        if not isinstance(result[key], bool):
            raise ValueError("Некорректный переключатель Instagram.")
    if not isinstance(result["keywords"], list) or any(not isinstance(x, str) for x in result["keywords"]):
        raise ValueError("Некорректные ключевые слова.")
    result["keywords"] = parse_keywords("\n".join(result["keywords"]))
    result["target_url"] = validate_url(result["target_url"])
    for key in ("opening", "link_text", "public_reply"):
        items = result[key]
        if not isinstance(items, list) or not 1 <= len(items) <= 3 or any(not isinstance(x, str) or not x.strip() or len(x) > 600 for x in items):
            raise ValueError("Нужно от 1 до 3 непустых вариантов текста, каждый до 600 знаков.")
        result[key] = [x.strip() for x in items]
    for key in ("opt_in_label", "link_label"):
        if not isinstance(result[key], str) or not 1 <= len(result[key].strip()) <= 20 or "\n" in result[key]:
            raise ValueError("Надпись на кнопке: от 1 до 20 знаков, одна строка.")
        result[key] = result[key].strip()
    if not result["keywords"]:
        result["enabled"] = False
    return result


def load_settings(path: Path | None = None) -> dict:
    path = path or CONFIG_PATH
    if not path.exists():
        return copy.deepcopy(DEFAULTS)
    if path.stat().st_size > 40000:
        raise ValueError("Файл настроек Instagram слишком большой.")
    return validate(json.loads(path.read_text(encoding="utf-8")))


def save_settings(settings: dict, path: Path | None = None) -> dict:
    settings = validate(settings)
    path = path or CONFIG_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".instagram-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(settings, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o644)  # Only public copy/settings; credentials are stored elsewhere.
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return settings


def choose(items: list[str], person: str) -> str:
    number = int.from_bytes(hashlib.sha256(person.encode()).digest()[:4], "big")
    return items[number % len(items)]


def connection_status(path: Path | None = None) -> dict:
    try:
        value = json.loads((path or STATUS_PATH).read_text(encoding="utf-8"))
        if not isinstance(value, dict) or time.time() - float(value.get("updated_at", 0)) > 120:
            return {"connected": False, "label": "Обработчик не подтвердил работу за последние 2 минуты."}
        return value
    except (OSError, ValueError, TypeError):
        return {"connected": False, "label": "Meta ещё не подключён. Сообщения не отправляются."}
