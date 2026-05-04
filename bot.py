import os
import asyncio
import logging
from datetime import datetime

from telegram import Bot, Update
from telegram.ext import Application, CommandHandler, ContextTypes
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from sheets import fetch_pvz_rows
from cache import load_cache, save_cache
from whitelist import (
    is_allowed, is_owner, add_user, remove_user,
    load_whitelist, OWNER_ID
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ─── CONFIG ────────────────────────────────────────────────────────────────────
BOT_TOKEN      = os.environ["BOT_TOKEN"]
SPREADSHEET_ID = os.environ["SPREADSHEET_ID"]
CHAT_ID        = int(os.environ.get("CHAT_ID", str(OWNER_ID)))

MY_PVZ = {
    "ТАШ-3", "ТАШ-5", "ТАШ-8", "ТАШ-27", "ТАШ-29", "ТАШ-50",
    "ТАШ-52", "ТАШ-65", "ТАШ-79", "ТАШ-82", "ТАШ-90", "ТАШ-93",
    "ТАШ-98", "ТАШ-100", "ТАШ-107", "ТАШ-151", "ТАШ-146",
    "FRTАШ-168", "FRTАШ-183", "FRTАШ-185", "FRTАШ-187", "FRTАШ_205",
    "FRTАШ-225", "FRTАШ-255", "FRTАШ-296", "FRTАШ-310", "FRTАШ-313",
}

# ─── HELPERS ───────────────────────────────────────────────────────────────────
def normalize_pvz(name: str) -> str:
    return (
        name.upper()
        .replace("TAШ", "ТАШ")
        .replace("ТAШ", "ТАШ")
        .strip()
    )

MY_PVZ_NORMALIZED = {normalize_pvz(p) for p in MY_PVZ}

def is_my_pvz(pvz_name: str) -> bool:
    return normalize_pvz(pvz_name) in MY_PVZ_NORMALIZED

STATUS_EMOJI = {
    "ожидает приемки": "🚚",
    "принят":          "📦",
    "выдан":           "✅",
}

def status_icon(status: str) -> str:
    return STATUS_EMOJI.get(status.lower().strip(), "❓")

def format_rows(rows: list[dict], title: str) -> str:
    if not rows:
        return ""
    lines = [f"<b>{title}</b>"]
    for r in rows:
        icon     = status_icon(r.get("status_priemki", ""))
        pvz      = r.get("pvz", "—")
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

# ─── ACCESS CONTROL ────────────────────────────────────────────────────────────
async def deny(update: Update) -> None:
    """Тихо логирует попытку и ничего не отвечает чужим."""
    user = update.effective_user
    cmd  = update.message.text if update.message else "?"
    logger.warning(
        f"ДОСТУП ЗАПРЕЩЁН | user_id={user.id} "
        f"username=@{user.username} name={user.full_name} | cmd={cmd}"
    )
    # Уведомляем владельца о чужой попытке
    try:
        await update.get_bot().send_message(
            OWNER_ID,
            f"⚠️ <b>Несанкционированная команда</b>\n\n"
            f"👤 Имя: {user.full_name}\n"
            f"🆔 ID: <code>{user.id}</code>\n"
            f"📎 Username: @{user.username}\n"
            f"💬 Команда: <code>{cmd}</code>",
            parse_mode="HTML"
        )
    except Exception:
        pass

def owner_only(func):
    """Декоратор: только владелец."""
    async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not is_owner(update.effective_user.id):
            await deny(update)
            return
        return await func(update, ctx)
    return wrapper

def whitelist_only(func):
    """Декоратор: только пользователи из whitelist."""
    async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not is_allowed(update.effective_user.id):
            await deny(update)
            return
        return await func(update, ctx)
    return wrapper

# ─── CORE CHECK ────────────────────────────────────────────────────────────────
async def check_and_notify(bot: Bot, manual: bool = False, requester_id: int = None):
    logger.info("Проверяем таблицу...")
    # Кому отвечать при manual=True
    reply_to = requester_id if requester_id else CHAT_ID

    try:
        all_rows = await asyncio.to_thread(fetch_pvz_rows, SPREADSHEET_ID)
    except Exception as e:
        logger.error(f"Ошибка при чтении таблицы: {e}")
        if manual:
            await bot.send_message(reply_to, f"❌ Ошибка при чтении таблицы:\n<code>{e}</code>", parse_mode="HTML")
        return

    my_rows = [r for r in all_rows if is_my_pvz(r.get("pvz", ""))]

    cache = load_cache()
    seen_keys: set = set(cache.get("seen_keys", []))

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

    cache["seen_keys"] = list(seen_keys)
    cache["last_check"] = datetime.now().isoformat()
    save_cache(cache)

    if not (new_waiting or new_accepted or new_issued):
        if manual:
            await bot.send_message(
                reply_to,
                f"✅ <b>Новых данных нет</b>\n"
                f"Всего строк: {len(all_rows)}, ваших ПВЗ: {len(my_rows)}",
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
    # Всегда шлём владельцу
    await bot.send_message(CHAT_ID, msg, parse_mode="HTML")
    # Если запросил не владелец — ему тоже отвечаем
    if manual and reply_to != CHAT_ID:
        await bot.send_message(reply_to, msg, parse_mode="HTML")

# ─── COMMANDS ──────────────────────────────────────────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_allowed(user_id):
        # /start доступен всем, но показываем ограниченное сообщение
        await update.message.reply_text("🔒 У вас нет доступа к этому боту.")
        await deny(update)
        return

    extra = "\n/admins — управление доступом" if is_owner(user_id) else ""
    await update.message.reply_text(
        "👋 <b>ПВЗ Монитор</b>\n\n"
        "Слежу за вашими ПВЗ в Google Таблице.\n\n"
        "Команды:\n"
        "/refresh — проверить таблицу прямо сейчас\n"
        "/status — время последней проверки\n"
        "/mypvz — список отслеживаемых ПВЗ"
        + extra,
        parse_mode="HTML"
    )

@whitelist_only
async def cmd_refresh(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    logger.info(f"/refresh от user_id={user.id} @{user.username}")
    await update.message.reply_text("🔄 Проверяю таблицу...")
    await check_and_notify(ctx.bot, manual=True, requester_id=user.id)

@whitelist_only
async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cache = load_cache()
    last = cache.get("last_check", "никогда")
    seen = len(cache.get("seen_keys", []))
    wl   = load_whitelist()
    await update.message.reply_text(
        f"📊 <b>Статус бота</b>\n\n"
        f"🕐 Последняя проверка: <code>{last}</code>\n"
        f"📋 Строк в кеше: <code>{seen}</code>\n"
        f"🏪 Отслеживаемых ПВЗ: <code>{len(MY_PVZ)}</code>\n"
        f"👥 Пользователей с доступом: <code>{len(wl)}</code>",
        parse_mode="HTML"
    )

@whitelist_only
async def cmd_mypvz(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    pvz_list = "\n".join(f"• {p}" for p in sorted(MY_PVZ))
    await update.message.reply_text(
        f"🏪 <b>Ваши ПВЗ ({len(MY_PVZ)} шт.):</b>\n\n{pvz_list}",
        parse_mode="HTML"
    )

# ─── ADMINS (только владелец) ──────────────────────────────────────────────────
@owner_only
async def cmd_admins(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """
    /admins               — показать список
    /admins add 123456    — добавить пользователя
    /admins remove 123456 — удалить пользователя
    """
    args = ctx.args  # список аргументов после команды

    wl = load_whitelist()

    # Без аргументов — показываем список
    if not args:
        lines = []
        for uid in wl:
            marker = " 👑 (владелец)" if uid == OWNER_ID else ""
            lines.append(f"• <code>{uid}</code>{marker}")
        await update.message.reply_text(
            f"👥 <b>Белый список ({len(wl)} чел.):</b>\n\n" + "\n".join(lines) + "\n\n"
            "Команды:\n"
            "<code>/admins add ID</code> — добавить\n"
            "<code>/admins remove ID</code> — удалить\n\n"
            "💡 ID пользователя можно узнать у @userinfobot",
            parse_mode="HTML"
        )
        return

    action = args[0].lower()

    if action in ("add", "remove") and len(args) < 2:
        await update.message.reply_text("❌ Укажи ID: /admins add 123456789")
        return

    if action not in ("add", "remove"):
        await update.message.reply_text("❌ Неизвестная команда. Используй: add или remove")
        return

    try:
        target_id = int(args[1])
    except ValueError:
        await update.message.reply_text("❌ ID должен быть числом")
        return

    if action == "add":
        if add_user(target_id):
            await update.message.reply_text(f"✅ Пользователь <code>{target_id}</code> добавлен в белый список.", parse_mode="HTML")
            logger.info(f"Добавлен в whitelist: {target_id}")
        else:
            await update.message.reply_text(f"ℹ️ Пользователь <code>{target_id}</code> уже в списке.", parse_mode="HTML")

    elif action == "remove":
        if target_id == OWNER_ID:
            await update.message.reply_text("❌ Нельзя удалить владельца.")
            return
        if remove_user(target_id):
            await update.message.reply_text(f"🗑 Пользователь <code>{target_id}</code> удалён из белого списка.", parse_mode="HTML")
            logger.info(f"Удалён из whitelist: {target_id}")
        else:
            await update.message.reply_text(f"❌ Пользователь <code>{target_id}</code> не найден в списке.", parse_mode="HTML")

# ─── MAIN ──────────────────────────────────────────────────────────────────────
async def post_init(app: Application):
    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        check_and_notify,
        "interval",
        hours=5,
        args=[app.bot],
        id="pvz_check",
        next_run_time=datetime.now()
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
    app.add_handler(CommandHandler("admins",  cmd_admins))

    logger.info("Бот запущен")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
