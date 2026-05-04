"""
Простой кеш на основе JSON-файла.
Хранит seen_keys — множество уже обработанных строк таблицы.
Автоматически удаляет записи старше 3 дней.
"""

import json
import os
from typing import Dict, Any
from datetime import datetime, timedelta

CACHE_FILE = os.environ.get("CACHE_FILE", "pvz_cache.json")
CACHE_RETENTION_DAYS = 3


def load_cache() -> Dict[str, Any]:
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                # Автоматическая ротация старых записей
                data = _rotate_cache(data)
                return data
        except (json.JSONDecodeError, OSError):
            pass
    return {"seen_keys": {}, "last_check": None}


def _rotate_cache(data: Dict[str, Any]) -> Dict[str, Any]:
    """Удаляет записи старше CACHE_RETENTION_DAYS дней."""
    seen_keys = data.get("seen_keys", {})

    # Если seen_keys это список (старый формат), конвертируем в словарь
    if isinstance(seen_keys, list):
        now = datetime.now().isoformat()
        seen_keys = {key: now for key in seen_keys}

    if not seen_keys:
        return data

    cutoff_date = datetime.now() - timedelta(days=CACHE_RETENTION_DAYS)
    original_count = len(seen_keys)

    # Удаляем старые записи
    seen_keys = {
        key: timestamp
        for key, timestamp in seen_keys.items()
        if datetime.fromisoformat(timestamp) > cutoff_date
    }

    removed_count = original_count - len(seen_keys)
    if removed_count > 0:
        print(f"Ротация кеша: удалено {removed_count} записей старше {CACHE_RETENTION_DAYS} дней")

    data["seen_keys"] = seen_keys
    return data


def save_cache(data: Dict[str, Any]) -> None:
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def clear_cache() -> int:
    """Полностью очищает кеш. Возвращает количество удаленных записей."""
    cache = load_cache()
    count = len(cache.get("seen_keys", {}))
    save_cache({"seen_keys": {}, "last_check": None})
    return count
