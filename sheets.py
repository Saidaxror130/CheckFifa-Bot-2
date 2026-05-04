"""
Читает данные из Google Sheets через публичный CSV-экспорт.

Структура колонок (по фото):
  A  - дзак / дата+время заказа  (28.04.2026 12:05:48)
  B  - Номер телефона             (998909851771)
  C  - ID ЗАКАЗА                  (1170, 1171, ...)
  D  - ПВЗ                        (ТАШ-41, ТАШ-111, АКК-1, ФЕР-1, ...)
  E  - План (Кол-во)              (20, 2, 1, ...)
  F  - Факт Приём                 (1, 0, ...)
  G  - Статус Приёмки             (Принят, Ожидает приемки)
  H  - ДАТА ПРИЁМКИ               (30.04.2026 17:12:24)
  I  - ЯЧЕЙКА                     (411, 212, ...)
  J  - Факт Выд                   (0, 1, ...)
  K  - Статус Выд                 (Выдан / пусто)
  L  - ДАТА ВЫДАЧИ                (01.05.2026 14:29:08)
"""

import csv
import io
import urllib.parse
import urllib.request
import time
import logging
from typing import List, Dict

logger = logging.getLogger(__name__)


SHEET_NAME = "Лист1"


def fetch_pvz_rows(spreadsheet_id: str, sheet_name: str = SHEET_NAME, max_retries: int = 3) -> List[Dict]:
    """Возвращает список строк таблицы как словарей с retry механизмом."""

    url = (
        f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}"
        f"/gviz/tq?tqx=out:csv&sheet={urllib.parse.quote(sheet_name)}"
    )

    last_error = None
    for attempt in range(max_retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read().decode("utf-8")

            reader = csv.reader(io.StringIO(raw))
            rows_out = []

            for i, row in enumerate(reader):
                if i == 0:
                    continue   # заголовок

                if len(row) < 7:
                    continue

                order_id       = row[2].strip() if len(row) > 2 else ""   # C
                pvz            = row[3].strip() if len(row) > 3 else ""   # D
                status_priemki = row[6].strip() if len(row) > 6 else ""   # G
                date_priemki   = row[7].strip() if len(row) > 7 else ""   # H
                status_vydachi = row[10].strip() if len(row) > 10 else "" # K
                date_vydachi   = row[11].strip() if len(row) > 11 else "" # L
                phone          = row[1].strip() if len(row) > 1 else ""   # B

                if not pvz:
                    continue

                rows_out.append({
                    "order_id":       order_id,
                    "pvz":            pvz,
                    "phone":          phone,
                    "status_priemki": status_priemki,
                    "date_priemki":   date_priemki,
                    "status_vydachi": status_vydachi,
                    "date_vydachi":   date_vydachi,
                })

            logger.info(f"Успешно загружено {len(rows_out)} строк из таблицы")
            return rows_out

        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as e:
            last_error = e
            if attempt < max_retries - 1:
                wait_time = 2 ** attempt  # экспоненциальная задержка: 1s, 2s, 4s
                logger.warning(f"Ошибка при чтении таблицы (попытка {attempt + 1}/{max_retries}): {e}. Повтор через {wait_time}s")
                time.sleep(wait_time)
            else:
                logger.error(f"Не удалось загрузить таблицу после {max_retries} попыток: {e}")
                raise Exception(f"Ошибка загрузки таблицы после {max_retries} попыток: {e}") from last_error

    raise Exception(f"Не удалось загрузить таблицу: {last_error}")
