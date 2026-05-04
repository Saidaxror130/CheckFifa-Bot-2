import os
import asyncio
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from telegram import Bot, Update
from telegram.ext import Application, CommandHandler, ContextTypes
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from sheets import fetch_pvz_rows
from cache import load_cache, save_cache, clear_cache
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
CHECK_INTERVAL_HOURS = int(os.environ.get("CHECK_INTERVAL_HOURS", "5"))
TIMEZONE = ZoneInfo("Asia/Tashkent")  # UTC+5

def now_local():
    """Возвращает текущее время в часовом поясе Ташкента."""
    return datetime.now(TIMEZONE)

def format_datetime(dt_str: str) -> str:
    """Конвертирует ISO строку в читаемый формат с местным временем."""
    try:
        dt = datetime.fromisoformat(dt_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=ZoneInfo("UTC"))
        dt_local = dt.astimezone(TIMEZONE)
        return dt_local.strftime("%d.%m.%Y %H:%M")
    except Exception:
        return dt_str

MY_PVZ = {
    "ТАШ-3", "ТАШ-5", "ТАШ-8", "ТАШ-27", "ТАШ-29", "ТАШ-50",
    "ТАШ-52", "ТАШ-65", "ТАШ-79", "ТАШ-82", "ТАШ-90", "ТАШ-93",
    "ТАШ-98", "ТАШ-100", "ТАШ-107", "ТАШ-151", "ТАШ-146",
    "FRТАШ-168", "FRТАШ-183", "FRТАШ-185", "FRТАШ-187", "FRТАШ_205",
    "FRТАШ-225", "FRТАШ-255", "FRТАШ-296", "FRТАШ-310", "FRТАШ-313",
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

def format_rows(rows: list, title: str) -> str:
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

def pending_block(all_pending: list) -> str:
    """Блок с текущими заказами которые ожидают приёмки."""
    if not all_pending:
        return "🟢 Все заказы приняты, ничего не ожидает."
    lines = "\n".join(
        f"  🚚 <b>{r.get('pvz')}</b> | Заказ: <code>{r.get('order_id')}</code>"
        for r in all_pending
    )
    return f"⏳ <b>Ожидают приёмки ({len(all_pending)} шт.):</b>\n{lines}"

# ─── ACCESS CONTROL ────────────────────────────────────────────────────────────
async def deny(update: Update) -> None:
    user = update.effective_user
    cmd  = update.message.text if update.message else "?"
    logger.warning(
        f"ДОСТУП ЗАПРЕЩЁН | user_id={user.id} "
        f"username=@{user.username} name={user.full_name} | cmd={cmd}"
    )
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
    async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not is_owner(update.effective_user.id):
            await deny(update)
            return
        return await func(update, ctx)
    return wrapper

def whitelist_only(func):
    async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not is_allowed(update.effective_user.id):
            await deny(update)
            return
        return await func(update, ctx)
    return wrapper

# ─── CORE CHECK ────────────────────────────────────────────────────────────────
async def check_and_notify(bot: Bot, manual: bool = False, requester_id: int = None):
    logger.info("Проверяем таблицу...")
    reply_to = requester_id if requester_id else CHAT_ID

    try:
        all_rows = await asyncio.to_thread(fetch_pvz_rows, SPREADSHEET_ID)
    except Exception as e:
        logger.error(f"Ошибка при чтении таблицы: {e}", exc_info=True)
        if manual:
            await bot.send_message(
                reply_to,
                f"❌ <b>Ошибка при загрузке таблицы</b>\n\n"
                f"<code>{str(e)}</code>\n\n"
                f"Проверьте:\n"
                f"• Доступ к таблице (должна быть открыта по ссылке)\n"
                f"• Правильность SPREADSHEET_ID\n"
                f"• Интернет соединение",
                parse_mode="HTML"
            )
        return

    my_rows = [r for r in all_rows if is_my_pvz(r.get("pvz", ""))]

    cache = load_cache()
    seen_keys = cache.get("seen_keys", {})

    def row_key(r: dict) -> str:
        return f"{r.get('order_id')}|{normalize_pvz(r.get('pvz',''))}|{r.get('status_priemki','')}|{r.get('status_vydachi','')}"

    new_waiting  = []
    new_accepted = []
    new_issued   = []

    now = now_local().isoformat()

    for r in my_rows:
        key = row_key(r)
        if key in seen_keys:
            continue
        seen_keys[key] = now  # сохраняем с timestamp
        st = r.get("status_priemki", "").lower().strip()
        sv = r.get("status_vydachi", "").lower().strip()
        if sv == "выдан":
            new_issued.append(r)
        elif st == "принят":
            new_accepted.append(r)
        elif st == "ожидает приемки":
            new_waiting.append(r)

    cache["seen_keys"] = seen_keys
    cache["last_check"] = now
    save_cache(cache)

    # Все текущие "Ожидает приёмки" — независимо от новизны
    all_pending = [
        r for r in my_rows
        if r.get("status_priemki", "").lower().strip() == "ожидает приемки"
        and r.get("status_vydachi", "").lower().strip() != "выдан"
    ]

    if not (new_waiting or new_accepted or new_issued):
        if manual:
            await bot.send_message(
                reply_to,
                f"✅ <b>Новых данных нет</b>\n"
                f"Всего строк: {len(all_rows)}, ваших ПВЗ: {len(my_rows)}\n\n"
                + pending_block(all_pending),
                parse_mode="HTML"
            )
        return

    parts = []
    if new_waiting:
        parts.append(format_rows(new_waiting,  "🆕 Новые — Ожидают приёмки"))
    if new_accepted:
        parts.append(format_rows(new_accepted, "📦 Приняты на ПВЗ"))
    if new_issued:
        parts.append(format_rows(new_issued,   "✅ Выданы клиентам"))

    # Итог: все ожидающие прямо сейчас
    parts.append(pending_block(all_pending))

    msg = "\n\n".join(parts)
    await bot.send_message(CHAT_ID, msg, parse_mode="HTML")
    if manual and reply_to != CHAT_ID:
        await bot.send_message(reply_to, msg, parse_mode="HTML")

# ─── COMMANDS ──────────────────────────────────────────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_allowed(user_id):
        await update.message.reply_text("🔒 У вас нет доступа к этому боту.")
        await deny(update)
        return
    extra = "\n/admins — управление доступом" if is_owner(user_id) else ""
    owner_cmds = "\n/interval — настроить интервал проверки" if is_owner(user_id) else ""
    await update.message.reply_text(
        "👋 <b>ПВЗ Монитор</b>\n\n"
        "Слежу за вашими ПВЗ в Google Таблице.\n\n"
        "Команды:\n"
        "/refresh — проверить таблицу прямо сейчас\n"
        "/status — статус и ожидающие заказы\n"
        "/mypvz — список отслеживаемых ПВЗ\n"
        "/clear_cache — очистить кеш"
        + extra + owner_cmds,
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
    if last != "никогда":
        last = format_datetime(last)
    seen = len(cache.get("seen_keys", {}))
    wl   = load_whitelist()

    # Получаем текущие ожидающие из таблицы
    try:
        all_rows = await asyncio.to_thread(fetch_pvz_rows, SPREADSHEET_ID)
        my_rows  = [r for r in all_rows if is_my_pvz(r.get("pvz", ""))]
        all_pending = [
            r for r in my_rows
            if r.get("status_priemki", "").lower().strip() == "ожидает приемки"
            and r.get("status_vydachi", "").lower().strip() != "выдан"
        ]
        pending_info = "\n\n" + pending_block(all_pending)
    except Exception as e:
        logger.error(f"Ошибка при загрузке данных для /status: {e}", exc_info=True)
        pending_info = "\n\n⚠️ Не удалось загрузить данные из таблицы."

    await update.message.reply_text(
        f"📊 <b>Статус бота</b>\n\n"
        f"🕐 Последняя проверка: <code>{last}</code>\n"
        f"📋 Строк в кеше: <code>{seen}</code>\n"
        f"🏪 Отслеживаемых ПВЗ: <code>{len(MY_PVZ)}</code>\n"
        f"👥 Пользователей с доступом: <code>{len(wl)}</code>"
        + pending_info,
        parse_mode="HTML"
    )

@whitelist_only
async def cmd_mypvz(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    pvz_list = "\n".join(f"• {p}" for p in sorted(MY_PVZ))
    await update.message.reply_text(
        f"🏪 <b>Ваши ПВЗ ({len(MY_PVZ)} шт.):</b>\n\n{pvz_list}",
        parse_mode="HTML"
    )

# ─── CLEAR CACHE (все в whitelist) ────────────────────────────────────────────
@whitelist_only
async def cmd_clear_cache(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Полностью очищает кеш обработанных заказов."""
    count = clear_cache()
    await update.message.reply_text(
        f"🗑 <b>Кеш очищен</b>\n\n"
        f"Удалено записей: <code>{count}</code>\n"
        f"При следующей проверке все заказы будут считаться новыми.",
        parse_mode="HTML"
    )
    logger.info(f"Кеш очищен пользователем {update.effective_user.id}. Удалено {count} записей.")

# ─── ADMINS (только владелец) ──────────────────────────────────────────────────
@owner_only
async def cmd_admins(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    args = ctx.args
    wl = load_whitelist()

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
            "💡 ID можно узнать у @userinfobot",
            parse_mode="HTML"
        )
        return

    action = args[0].lower()

    if action in ("add", "remove") and len(args) < 2:
        await update.message.reply_text("❌ Укажи ID: /admins add 123456789")
        return

    if action not in ("add", "remove"):
        await update.message.reply_text("❌ Используй: add или remove")
        return

    try:
        target_id = int(args[1])
    except ValueError:
        await update.message.reply_text("❌ ID должен быть числом")
        return

    if action == "add":
        if add_user(target_id):
            await update.message.reply_text(f"✅ Пользователь <code>{target_id}</code> добавлен.", parse_mode="HTML")
        else:
            await update.message.reply_text(f"ℹ️ Уже в списке: <code>{target_id}</code>.", parse_mode="HTML")

    elif action == "remove":
        if target_id == OWNER_ID:
            await update.message.reply_text("❌ Нельзя удалить владельца.")
            return
        if remove_user(target_id):
            await update.message.reply_text(f"🗑 Удалён: <code>{target_id}</code>.", parse_mode="HTML")
        else:
            await update.message.reply_text(f"❌ Не найден: <code>{target_id}</code>.", parse_mode="HTML")

# ─── INTERVAL (только владелец) ────────────────────────────────────────────────
@owner_only
async def cmd_interval(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Изменяет интервал автоматической проверки таблицы."""
    args = ctx.args

    if not args:
        scheduler = ctx.bot_data.get("scheduler")
        job = scheduler.get_job("pvz_check") if scheduler else None
        current_hours = CHECK_INTERVAL_HOURS
        if job and hasattr(job.trigger, 'interval'):
            current_hours = int(job.trigger.interval.total_seconds() / 3600)

        await update.message.reply_text(
            f"⏱ <b>Текущий интервал проверки:</b> {current_hours} ч.\n\n"
            f"Использование:\n"
            f"<code>/interval ЧАСЫ</code>\n\n"
            f"Пример: <code>/interval 3</code> — проверять каждые 3 часа\n"
            f"Минимум: 1 час, Максимум: 24 часа",
            parse_mode="HTML"
        )
        return

    try:
        new_hours = int(args[0])
    except ValueError:
        await update.message.reply_text("❌ Укажи число часов: /interval 3")
        return

    if new_hours < 1 or new_hours > 24:
        await update.message.reply_text("❌ Интервал должен быть от 1 до 24 часов")
        return

    # Перезапускаем планировщик с новым интервалом
    scheduler = ctx.bot_data.get("scheduler")
    if scheduler:
        scheduler.remove_job("pvz_check")
        scheduler.add_job(
            check_and_notify,
            "interval",
            hours=new_hours,
            args=[ctx.bot],
            id="pvz_check",
            next_run_time=now_local()
        )
        logger.info(f"Интервал проверки изменен на {new_hours} ч. владельцем")
        await update.message.reply_text(
            f"✅ <b>Интервал обновлен</b>\n\n"
            f"Теперь таблица проверяется каждые <b>{new_hours} ч.</b>\n"
            f"Первая проверка начнется сейчас.",
            parse_mode="HTML"
        )
    else:
        await update.message.reply_text("❌ Планировщик не найден")

# ─── MAIN ──────────────────────────────────────────────────────────────────────
async def post_init(app: Application):
    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        check_and_notify,
        "interval",
        hours=CHECK_INTERVAL_HOURS,
        args=[app.bot],
        id="pvz_check",
        next_run_time=now_local()
    )
    scheduler.start()
    app.bot_data["scheduler"] = scheduler
    logger.info(f"✅ Планировщик запущен (каждые {CHECK_INTERVAL_HOURS} часов)")
    logger.info(f"🤖 Бот инициализирован. Отслеживается {len(MY_PVZ)} ПВЗ")

def main():
    try:
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
        app.add_handler(CommandHandler("clear_cache", cmd_clear_cache))
        app.add_handler(CommandHandler("interval", cmd_interval))

        logger.info("🚀 Бот запущен и готов к работе")
        app.run_polling(drop_pending_updates=True)
    except Exception as e:
        logger.critical(f"❌ Критическая ошибка при запуске бота: {e}", exc_info=True)
        raise

if __name__ == "__main__":
    main()
