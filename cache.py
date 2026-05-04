"""
Простой кеш на основе JSON-файла.
Хранит seen_keys — множество уже обработанных строк таблицы.
"""

import json
import os
from typing import Dict, Any

CACHE_FILE = os.environ.get("CACHE_FILE", "pvz_cache.json")


def load_cache() -> Dict[str, Any]:
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {"seen_keys": [], "last_check": None}


def save_cache(data: Dict[str, Any]) -> None:
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
