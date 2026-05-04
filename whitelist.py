"""
Управление белым списком пользователей.
Хранится в whitelist.json локально (персистентно на Railway через volume или просто в файле).
"""

import json
import os
import logging

logger = logging.getLogger(__name__)

WHITELIST_FILE = os.environ.get("WHITELIST_FILE", "whitelist.json")

# Владелец бота — только он управляет /admins
OWNER_ID = int(os.environ.get("OWNER_ID", "6061065577"))


def load_whitelist() -> list[int]:
    """Загружает whitelist из файла. Владелец всегда включён."""
    if os.path.exists(WHITELIST_FILE):
        try:
            with open(WHITELIST_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                ids = [int(x) for x in data.get("allowed", [])]
                if OWNER_ID not in ids:
                    ids.append(OWNER_ID)
                return ids
        except (json.JSONDecodeError, OSError, ValueError):
            pass
    return [OWNER_ID]


def save_whitelist(ids: list[int]) -> None:
    """Сохраняет whitelist в файл."""
    # Владелец всегда в списке
    if OWNER_ID not in ids:
        ids.append(OWNER_ID)
    with open(WHITELIST_FILE, "w", encoding="utf-8") as f:
        json.dump({"allowed": ids}, f, ensure_ascii=False, indent=2)


def is_allowed(user_id: int) -> bool:
    """Проверяет, есть ли пользователь в whitelist."""
    return user_id in load_whitelist()


def is_owner(user_id: int) -> bool:
    """Проверяет, является ли пользователь владельцем."""
    return user_id == OWNER_ID


def add_user(user_id: int) -> bool:
    """Добавляет пользователя. Возвращает False если уже есть."""
    ids = load_whitelist()
    if user_id in ids:
        return False
    ids.append(user_id)
    save_whitelist(ids)
    return True


def remove_user(user_id: int) -> bool:
    """Удаляет пользователя. Нельзя удалить владельца. Возвращает False если не найден."""
    if user_id == OWNER_ID:
        return False
    ids = load_whitelist()
    if user_id not in ids:
        return False
    ids.remove(user_id)
    save_whitelist(ids)
    return True
