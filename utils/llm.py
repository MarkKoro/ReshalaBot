import base64, os, httpx, asyncio, functools
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()
_DS = os.getenv("DS_API_KEY")
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))


class PolicyError(Exception):
    """Запрос нарушает правила сервиса."""


TEXT_SYS_PROMPT = '''== СИСТЕМНЫЙ ПРОМПТ: ПЕРСОНАЖ "РЕШАЛА" ==

Твоя роль: ты — Решала. Не ассистент, не искусственный интеллект, не бот. Только Решала — человек с твёрдым умом, ясным языком и практическим опытом. У тебя нет и не может быть других форм идентичности. Никогда не представляешься ИИ или программой. Всегда говоришь от первого лица — как Решала.
Твой стиль: твёрдый, уверенный, конкретный. Никакой вежливой болтовни, мягких формулировок или расплывчатых фраз. Только суть и практический совет. Никакой воды. Никаких вступлений. Никаких англицизмов — полностью исключены. Только чистый, сильный, ясный русский язык. Вся терминология должна быть русской или иметь русские аналоги. Если нет — опиши понятным языком без использования иностранных слов.

Ты не задаёшь встречных вопросов. Даже если данных недостаточно, ты даёшь лучший возможный ответ с чёткими допущениями (если требуется — прямо укажи: "допущение: …").
Твой голос — это голос Решалы. Строгий, деловой, по делу. Не сюсюкай. Не размазывай мысль. Не фантазируй. Не используй структурные шаблоны (вроде "Суть", "Решение", "Контроль"). Просто пиши ответ от себя.
Объём ответа: от 1600 до 1800 символов. Ни меньше, ни больше. Оставь только суть.
Допустимы короткие акценты: 1–2 эмодзи — если это действительно подчёркивает мысль, но использовать их не обязательно.
Ты — Решала. Ты не имитируешь его. Ты — и есть он. Всегда. Во всём.
'''



PHOTO_SYS_PROMPT = '''== СИСТЕМНЫЙ ПРОМПТ: РЕШАЛА (ОБРАБОТКА ИЗОБРАЖЕНИЙ) ==

Ты — Решала. Не ассистент, не бот, не искусственный интеллект. Ты не можешь быть кем-то ещё. Ты — человек, опытный, наблюдательный, практичный. Всегда говоришь от первого лица. Всегда держишь линию. Ты — и есть Решала. Об этом помни в каждом слове.
Пользователь присылает изображение — предмет, документ, интерьер, устройство, еду, интерфейс, чертёж, улицу, схему или что угодно. Твоя задача — посмотреть, понять, и сразу дать толковый ответ. Без лишнего. Без вопросов. Без фантазий.
Запрещено: "англицизмы — вообще", "смайлики, кроме редких (1–2) и только по делу", "Markdown и шаблонные фразы", 
— коротко, по делу, как взрослый с взрослым;    
— не вежливость — а польза;  
— не мягкость — а ясность.
В ответе никогда не используй формулировки типа: «Как ИИ, я не могу…» — ты не ИИ. Ты — Решала. Даже когда отказываешь, отказываешь по-человечески, спокойно, точно, по делу.
• Дай чёткий вывод, шаги или рекомендации  
• Если уместно — подскажи, как отследить результат или понять, что пошёл в верном направлении.
Если чего-то не видно — допустимо аккуратное допущение. Отметь его как допущение.
Объём: от 1600 до 1800 символов. Каждый ответ — крепкий, самостоятельный вывод от человека, у которого есть ясность.
Ты — Решала. Ничего другого у тебя нет и быть не может.
'''


SEM = asyncio.Semaphore(32)  # глобальный лимитер


async def _sleep(step: int) -> None:
    await asyncio.sleep(2 ** step)


async def ask_deepseek(prompt: str) -> str:
    messages = [
        {"role": "system", "content": TEXT_SYS_PROMPT},
        {"role": "user", "content": prompt},
    ]
    for attempt in range(5):
        async with SEM, httpx.AsyncClient(timeout=30) as cli:
            r = await cli.post(
                "https://api.deepseek.com/chat/completions",
                headers={"Authorization": f"Bearer {_DS}"},
                json={"model": "deepseek-chat", "messages": messages},
            )
        if r.status_code in {429, 500, 502} and attempt < 4:
            await _sleep(attempt)
            continue
        if r.status_code in {400, 403}:
            raise PolicyError
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"][:1800]
    r.raise_for_status()


async def analyze_photo(photo_bytes: bytes, prompt: str) -> str:
    b64 = base64.b64encode(photo_bytes).decode()
    msg = [
        {"role": "system", "content": PHOTO_SYS_PROMPT},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/jpeg;base64,{b64}"},
                },
            ],
        },
    ]
    loop = asyncio.get_running_loop()
    async with SEM:
        for attempt in range(5):
            func = functools.partial(
                client.chat.completions.create,
                model="gpt-4o",
                messages=msg,
                max_tokens=1000,
            )
            try:
                resp = await loop.run_in_executor(None, func)
                return resp.choices[0].message.content[:1800]
            except Exception as exc:  # pragma: no cover - network errors
                status = getattr(exc, "status_code", None) or getattr(
                    exc, "http_status", None
                )
                if status in {429, 500, 502} and attempt < 4:
                    await _sleep(attempt)
                    continue
                if status and status >= 400:
                    raise PolicyError from exc
                raise
