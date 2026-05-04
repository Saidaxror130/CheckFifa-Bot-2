import os
import asyncio
import logging
import json
from datetime import datetime

from telegram import Bot, Update
from telegram.ext import Application, CommandHandler, ContextTypes
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from sheets import fetch_pvz_rows
from cache import load_cache, save_cache

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ─── CONFIG ────────────────────────────────────────────────────────────────────
BOT_TOKEN       = os.environ["BOT_TOKEN"]
SPREADSHEET_ID  = os.environ["SPREADSHEET_ID"]   # только ID после /d/
CHAT_ID         = int(os.environ.get("CHAT_ID", "6061065577"))

MY_PVZ = {
    "ТАШ-3", "ТАШ-5", "ТАШ-8", "ТАШ-27", "ТАШ-29", "ТАШ-50",
    "ТАШ-52", "ТАШ-65", "ТАШ-79", "ТАШ-82", "ТАШ-90", "ТАШ-93",
    "ТАШ-98", "ТАШ-100", "ТАШ-107", "ТАШ-151", "ТАШ-146",
    "FRTАШ-168", "FRTАШ-183", "FRTАШ-185", "FRTАШ-187", "FRTАШ_205",
    "FRTАШ-225", "FRTАШ-255", "FRTАШ-296", "FRTАШ-310", "FRTАШ-313",
}

def normalize_pvz(name: str) -> str:
    """Нормализует название ПВЗ для сравнения."""
    return (
        name.upper()
        .replace("FR", "FR")
        .replace("ТАШ", "ТАШ")
        .replace("TAШ", "ТАШ")     # латинская A → кириллическая А
        .replace("ТAШ", "ТАШ")     # латинская A → кириллическая А
        .strip()
    )

MY_PVZ_NORMALIZED = {normalize_pvz(p) for p in MY_PVZ}

def is_my_pvz(pvz_name: str) -> bool:
    return normalize_pvz(pvz_name) in MY_PVZ_NORMALIZED

# ─── STATUS EMOJI ──────────────────────────────────────────────────────────────
STATUS_EMOJI = {
    "ожидает приемки": "🚚",
    "принят":          "📦",
    "выдан":           "✅",
}

def status_icon(status: str) -> str:
    return STATUS_EMOJI.get(status.lower().strip(), "❓")

# ─── FORMAT MESSAGE ────────────────────────────────────────────────────────────
def format_rows(rows: list[dict], title: str) -> str:
    if not rows:
        return ""
    lines = [f"<b>{title}</b>"]
    for r in rows:
        icon = status_icon(r.get("status_priemki", ""))
        pvz  = r.get("pvz", "—")
        order_id = r.get("order_id", "—")
        status_p = r.get("status_priemki", "—")
        status_v = r.get("status_vydachi", "")
        date_p   = r.get("date_priemki", "")
        date_v   = r.get("date_vydachi", "")

        line = f"{icon} <b>{pvz}</b> | Заказ: <code>{order_id}</code>\n"
        line += f"   Статус: {status_p}"
        if date_p:
            line += f" ({date_p})"
        if status_v and status_v.lower() == "выдан":
            line += f"\n   Выдан: ✅ {date_v}"
        lines.append(line)
    return "\n".join(lines)

# ─── CORE CHECK ────────────────────────────────────────────────────────────────
async def check_and_notify(bot: Bot, manual: bool = False):
    logger.info("Проверяем таблицу...")
    try:
        all_rows = await asyncio.to_thread(fetch_pvz_rows, SPREADSHEET_ID)
    except Exception as e:
        logger.error(f"Ошибка при чтении таблицы: {e}")
        if manual:
            await bot.send_message(CHAT_ID, f"❌ Ошибка при чтении таблицы:\n<code>{e}</code>", parse_mode="HTML")
        return

    # Фильтруем только наши ПВЗ
    my_rows = [r for r in all_rows if is_my_pvz(r.get("pvz", ""))]

    cache = load_cache()
    seen_keys: set = set(cache.get("seen_keys", []))

    # Ключ строки = order_id + pvz + статус приёмки + статус выдачи
    def row_key(r: dict) -> str:
        return f"{r.get('order_id')}|{normalize_pvz(r.get('pvz',''))}|{r.get('status_priemki','')}|{r.get('status_vydachi','')}"

    new_waiting  = []
    new_accepted = []
    new_issued   = []

    for r in my_rows:
        key = row_key(r)
        if key in seen_keys:
            continue
        seen_keys.add(key)
        st = r.get("status_priemki", "").lower().strip()
        sv = r.get("status_vydachi", "").lower().strip()
        if sv == "выдан":
            new_issued.append(r)
        elif st == "принят":
            new_accepted.append(r)
        elif st == "ожидает приемки":
            new_waiting.append(r)

    # Сохраняем обновлённый кеш
    cache["seen_keys"] = list(seen_keys)
    cache["last_check"] = datetime.now().isoformat()
    save_cache(cache)

    if not (new_waiting or new_accepted or new_issued):
        if manual:
            await bot.send_message(
                CHAT_ID,
                f"✅ <b>Обновление завершено</b>\nНовых данных по вашим ПВЗ нет.\n"
                f"Всего строк в таблице: {len(all_rows)}, ваших: {len(my_rows)}",
                parse_mode="HTML"
            )
        return

    parts = []
    if new_waiting:
        parts.append(format_rows(new_waiting,  "🚚 Ожидают приёмки"))
    if new_accepted:
        parts.append(format_rows(new_accepted, "📦 Приняты на ПВЗ"))
    if new_issued:
        parts.append(format_rows(new_issued,   "✅ Выданы клиентам"))

    msg = "\n\n".join(parts)
    await bot.send_message(CHAT_ID, msg, parse_mode="HTML")

# ─── COMMANDS ──────────────────────────────────────────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 <b>ПВЗ Монитор запущен!</b>\n\n"
        "Я слежу за вашими ПВЗ в Google Таблице и уведомляю о новых событиях.\n\n"
        "Команды:\n"
        "/refresh — принудительная проверка таблицы\n"
        "/status — статистика и время последней проверки\n"
        "/mypvz — список отслеживаемых ПВЗ",
        parse_mode="HTML"
    )

async def cmd_refresh(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔄 Проверяю таблицу...")
    await check_and_notify(ctx.bot, manual=True)

async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cache = load_cache()
    last = cache.get("last_check", "никогда")
    seen = len(cache.get("seen_keys", []))
    await update.message.reply_text(
        f"📊 <b>Статус бота</b>\n\n"
        f"🕐 Последняя проверка: <code>{last}</code>\n"
        f"📋 Строк в кеше: <code>{seen}</code>\n"
        f"🏪 Отслеживаемых ПВЗ: <code>{len(MY_PVZ)}</code>",
        parse_mode="HTML"
    )

async def cmd_mypvz(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    pvz_list = "\n".join(f"• {p}" for p in sorted(MY_PVZ))
    await update.message.reply_text(
        f"🏪 <b>Ваши ПВЗ ({len(MY_PVZ)} шт.):</b>\n\n{pvz_list}",
        parse_mode="HTML"
    )

# ─── MAIN ──────────────────────────────────────────────────────────────────────
async def post_init(app: Application):
    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        check_and_notify,
        "interval",
        hours=5,
        args=[app.bot],
        id="pvz_check",
        next_run_time=datetime.now()   # сразу при старте
    )
    scheduler.start()
    app.bot_data["scheduler"] = scheduler
    logger.info("Планировщик запущен (каждые 5 часов)")

def main():
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )
    app.add_handler(CommandHandler("start",   cmd_start))
    app.add_handler(CommandHandler("refresh", cmd_refresh))
    app.add_handler(CommandHandler("status",  cmd_status))
    app.add_handler(CommandHandler("mypvz",   cmd_mypvz))

    logger.info("Бот запущен")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
