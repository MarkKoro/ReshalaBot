import os, base64
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

# DeepSeek client uses OpenAI-compatible API
_deepseek = OpenAI(api_key=os.getenv("DEEPSEEK_API_KEY"), base_url="https://api.deepseek.com")
# OpenAI client for vision analysis
_openai = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

SYSTEM_TEXT = (
    "Ты — Решало. Отвечай уверенно и по делу, без лишних эмоций. "
    "Максимум 1200 символов."
)


def ask_deepseek(question: str) -> str:
    """Отправить текстовый запрос в DeepSeek и вернуть ответ."""
    resp = _deepseek.chat.completions.create(
        model="deepseek-chat",
        messages=[
            {"role": "system", "content": SYSTEM_TEXT},
            {"role": "user", "content": question},
        ],
        max_tokens=600,
        temperature=0.7,
    )
    return resp.choices[0].message.content.strip()


def analyze_photo(image_bytes: bytes, description: str) -> str:
    """Отправить фото и описание в GPT‑4o и получить ответ."""
    b64 = base64.b64encode(image_bytes).decode()
    resp = _openai.responses.create(
        model="gpt-4o-mini",  # 4o vision
        input=[
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": description},
                    {"type": "input_image", "image": b64},
                ],
            }
        ],
        max_output_tokens=600,
        temperature=0.7,
        system=SYSTEM_TEXT,
    )
    return resp.output_text.strip()
