import base64, os, httpx, asyncio, functools
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()
_DS = os.getenv("DS_API_KEY")
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

SEM = asyncio.Semaphore(32)  # глобальный лимитер


async def _sleep(step: int) -> None:
    await asyncio.sleep(2 ** step)


async def ask_deepseek(prompt: str) -> str:
    for attempt in range(5):
        async with SEM, httpx.AsyncClient(timeout=30) as cli:
            r = await cli.post(
                "https://api.deepseek.com/chat/completions",
                headers={"Authorization": f"Bearer {_DS}"},
                json={
                    "model": "deepseek-chat",
                    "messages": [{"role": "user", "content": prompt}],
                },
            )
        if r.status_code in {429, 500, 502} and attempt < 4:
            await _sleep(attempt)
            continue
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"][:1200]
    r.raise_for_status()

async def analyze_photo(photo_bytes: bytes, prompt: str) -> str:
    b64 = base64.b64encode(photo_bytes).decode()
    msg = [
      {"role":"system","content":"Ты – Решала. Отвечай коротко (≤1200 симв.)"},
      {"role":"user","content":[
          {"type":"text","text": prompt},
          {"type":"image_url","image_url":{"url":f"data:image/jpeg;base64,{b64}"}}
      ]}
    ]
    loop = asyncio.get_running_loop()
    async with SEM:
        for attempt in range(5):
            func = functools.partial(
                client.chat.completions.create,
                model="gpt-4o",
                messages=msg,
                max_tokens=800,
            )
            try:
                resp = await loop.run_in_executor(None, func)
                return resp.choices[0].message.content[:1200]
            except Exception as exc:  # pragma: no cover - network errors
                status = getattr(exc, "status_code", None) or getattr(
                    exc, "http_status", None
                )
                if status in {429, 500, 502} and attempt < 4:
                    await _sleep(attempt)
                    continue
                raise
