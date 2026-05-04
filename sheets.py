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
from typing import List, Dict


SHEET_NAME = "Лист1"


def fetch_pvz_rows(spreadsheet_id: str, sheet_name: str = SHEET_NAME) -> List[Dict]:
    """Возвращает список строк таблицы как словарей."""

    url = (
        f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}"
        f"/gviz/tq?tqx=out:csv&sheet={urllib.parse.quote(sheet_name)}"
    )

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

    return rows_out
