import base64, os, httpx, asyncio
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()
_DS = os.getenv("DS_API_KEY")
_OAI = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

SEM = asyncio.Semaphore(32)   # глобальный лимитер

async def ask_deepseek(prompt: str) -> str:
    async with SEM, httpx.AsyncClient(timeout=30) as cli:
        r = await cli.post(
            "https://api.deepseek.com/chat/completions",
            headers={"Authorization": f"Bearer {_DS}"},
            json={"model":"deepseek-chat", "messages":[{"role":"user","content":prompt}]})
        return r.json()["choices"][0]["message"]["content"][:1200]

async def analyze_photo(photo_bytes: bytes, prompt: str) -> str:
    import base64, asyncio, functools
    b64 = base64.b64encode(photo_bytes).decode()
    msg = [
      {"role":"system","content":"Ты — Решала. Отвечай кратко (до 1200 симв.), чётко, по делу."},
      {"role":"user","content":[
         {"type":"text","text": prompt},
         {"type":"image_url","image_url":{"url":f"data:image/jpeg;base64,{b64}"}}
      ]}
    ]
    loop = asyncio.get_running_loop()
    with (await SEM):
        fn = functools.partial(_OAI.chat.completions.create, model="gpt-4o", messages=msg, max_tokens=800)
        resp = await loop.run_in_executor(None, fn)
    return resp.choices[0].message.content[:1200]
