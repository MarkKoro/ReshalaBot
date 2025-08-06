import json
from pathlib import Path
from typing import Any

ASSETS_FILE = Path(__file__).with_name("assets.json")
SLOTS = ("main", "pay", "how", "ref")


def load_assets() -> dict[str, Any]:
    if ASSETS_FILE.exists():
        with ASSETS_FILE.open("r", encoding="utf-8") as f:
            data = json.load(f)
    else:
        data = {}
    data.setdefault("cache_chat_id", 0)
    banners = data.setdefault("banners", {})
    for s in SLOTS:
        slot = banners.setdefault(s, {})
        slot.setdefault("msg_id", 0)
        slot.setdefault("file_id", "")
    save_assets(data)
    return data


def save_assets(data: dict[str, Any]) -> None:
    with ASSETS_FILE.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
