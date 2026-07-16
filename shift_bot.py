import os
import logging
import calendar
import asyncio
import threading
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests
from flask import Flask, request, jsonify
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)

from sheets_sync import GoogleSheetsSync


BOT_TOKEN = os.getenv("BOT_TOKEN")
GROUP_ID = os.getenv("GROUP_ID")
AUTO_CLOSE_HOURS = float(os.getenv("AUTO_CLOSE_HOURS", "8"))
AUTO_CLOSE_MINUTES = int(AUTO_CLOSE_HOURS * 60)

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is missing")

if not GROUP_ID:
    raise RuntimeError("GROUP_ID is missing")


# Business timezone. TimeMoto sends local wall-clock time without an offset,
# and "today"/day boundaries for /report and /history are computed here.
APP_TZ = ZoneInfo(os.getenv("TIMEZONE", "Europe/Monaco"))

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("shift_bot")

flask_app = Flask(__name__)
sheets_sync = GoogleSheetsSync(APP_TZ, logger)

if not sheets_sync.is_ready():
    raise RuntimeError(
        "Google Sheets is not configured. Set GOOGLE_DRIVE_FOLDER_ID "
        "and Google service account variables."
    )

RU_MONTHS = [
    "Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
    "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь",
]
RU_WEEKDAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]


def is_allowed(update: Update) -> bool:
    return str(update.effective_chat.id) == str(GROUP_ID)


def fmt_time(value):
    if not value:
        return "—"
    dt = parse_timestamp(value)
    if dt is None:
        return str(value)
    return dt.astimezone(APP_TZ).strftime("%H:%M")


def fmt_duration(minutes):
    if minutes is None:
        return "смена открыта"
    minutes = int(minutes)
    if minutes < 1:
        return "меньше минуты"
    hours = minutes // 60
    mins = minutes % 60
    return f"{hours}ч {mins:02d}м"


def fmt_clock_out_status(duration_minutes):
    lines = ["⏱ смена закрыта"]
    if duration_minutes is not None:
        lines.append(f"⌛ Отработано: {fmt_duration(duration_minutes)}")
    return "\n".join(lines)


def send_telegram_message(text: str):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    response = requests.post(
        url,
        json={
            "chat_id": GROUP_ID,
            "text": text,
        },
        timeout=10,
    )
    response.raise_for_status()


def parse_timestamp(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None
    # TimeMoto sends naive local wall-clock time -> interpret it in APP_TZ.
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=APP_TZ)
    return dt


def today_local():
    return datetime.now(APP_TZ).date()


def fmt_period(date_from, date_to):
    if date_from == date_to:
        return date_from.strftime("%d.%m.%Y")
    return f"{date_from.strftime('%d.%m')} — {date_to.strftime('%d.%m.%Y')}"


# --------------------------------------------------------------------------- #
# Google Sheets storage helpers
# --------------------------------------------------------------------------- #

def parse_sheet_date(value):
    if not value:
        return None
    try:
        return datetime.strptime(str(value).strip(), "%d.%m.%Y").date()
    except ValueError:
        return None


def parse_clock_minutes(value):
    if not value or ":" not in str(value):
        return None
    hours, _, minutes = str(value).strip().partition(":")
    try:
        return int(hours) * 60 + int(minutes)
    except ValueError:
        return None


def row_clock_out_date(row):
    row_date = parse_sheet_date(row.get("date") or row.get("date_label"))
    clock_in = parse_clock_minutes(row.get("clock_in_time"))
    clock_out = parse_clock_minutes(row.get("clock_out_time"))
    if row_date is None or clock_in is None or clock_out is None:
        return None
    return row_date + timedelta(days=1 if clock_out < clock_in else 0)


def fetch_on_shift():
    rows = sheets_sync.day_rows_for_bot(today_local())
    return [row for row in rows if row.get("status") == "Открыта"]


def fetch_sessions_for_period(date_from, date_to):
    return sheets_sync.range_rows_for_bot(date_from, date_to)


def fetch_clockins(selected_date):
    return [
        row for row in sheets_sync.day_rows_for_bot(selected_date)
        if row.get("clock_in_time")
    ]


def fetch_clockouts(selected_date):
    rows = sheets_sync.range_rows_for_bot(selected_date - timedelta(days=1), selected_date)
    return [
        row for row in rows
        if row.get("clock_out_time") and row_clock_out_date(row) == selected_date
    ]


def sync_day_to_sheets(selected_date):
    sheets_sync.ensure_month_layout(selected_date)
    return sheets_sync.sync_month_summary_from_sheets(selected_date)


def sync_month_to_sheets(selected_date, sync_days=True):
    return sheets_sync.sync_month_summary_from_sheets(selected_date)


def auto_close_stale_sessions():
    return sheets_sync.auto_close_stale_sessions(today_local(), AUTO_CLOSE_MINUTES)


def scheduler_loop():
    last_daily_run = None
    while True:
        try:
            now_local = datetime.now(APP_TZ)
            if last_daily_run != now_local.date():
                auto_close_stale_sessions()
                sync_day_to_sheets(now_local.date())
                if now_local.day == 1:
                    sync_month_to_sheets(now_local.date() - timedelta(days=1), sync_days=False)
                last_daily_run = now_local.date()
        except Exception:
            logger.exception("Background scheduler failed")
        time.sleep(60)


# --------------------------------------------------------------------------- #
# Webhook (TimeMoto -> Google Sheets + Telegram)
# --------------------------------------------------------------------------- #

@flask_app.route("/", methods=["GET"])
def home():
    return jsonify({"ok": True, "service": "staff-control-bot"})


def persist_attendance(
    full_name, department, location, clock_type, clock_dt,
):
    if clock_type == "In":
        return sheets_sync.record_clock_in(
            clock_dt=clock_dt,
            full_name=full_name,
            department=department,
            location=location,
        )

    if clock_type == "Out":
        return sheets_sync.record_clock_out(
            clock_dt=clock_dt,
            full_name=full_name,
            department=department,
            location=location,
        )

    logger.warning("Unsupported TimeMoto clockingType: %s", clock_type)
    return None


@flask_app.route("/timemoto", methods=["GET", "POST"])
def timemoto_webhook():

    if request.method == "GET":
        return jsonify({"ok": True})

    payload = request.get_json(silent=True) or {}

    event = payload.get("event")
    data = payload.get("data", {})

    if event != "attendance.inserted":
        return jsonify({"ok": True, "skipped": "event"})

    full_name = data.get("userFullName", "Unknown")
    department = data.get("departmentName", "")
    location = data.get("locationName", "")
    clock_type = data.get("clockingType", "")
    time_logged = data.get("timeLogged", "")

    clock_dt = parse_timestamp(time_logged) or datetime.now(APP_TZ)
    time_display = clock_dt.astimezone(APP_TZ).strftime("%H:%M")

    # Persist to Google Sheets, but never let a write error block notification.
    duration_minutes = None
    try:
        duration_minutes = persist_attendance(
            full_name, department, location, clock_type, clock_dt,
        )
    except Exception:
        logger.exception("Google Sheets write failed for TimeMoto webhook")

    # Telegram notification is always sent.
    if clock_type == "In":
        send_telegram_message(
            f"🟢 Приход\n\n"
            f"👤 {full_name}\n"
            f"🏢 {department}\n"
            f"🕒 {time_display}\n"
            f"⏱ смена открыта"
        )
    elif clock_type == "Out":
        send_telegram_message(
            f"🔴 Уход\n\n"
            f"👤 {full_name}\n"
            f"🏢 {department}\n"
            f"🕒 {time_display}\n"
            f"{fmt_clock_out_status(duration_minutes)}"
        )

    # Always 200 so TimeMoto does not retry-storm on transient DB issues.
    return jsonify({"ok": True})


# --------------------------------------------------------------------------- #
# Telegram UI — keyboards
# --------------------------------------------------------------------------- #

def main_menu_kb():
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("👮 Кто на смене", callback_data="who")],
            [
                InlineKeyboardButton("📅 История", callback_data="history"),
                InlineKeyboardButton("📊 Отчёт", callback_data="report"),
            ],
            [
                InlineKeyboardButton("🟢 Приходы", callback_data="clockins"),
                InlineKeyboardButton("🔴 Уходы", callback_data="clockouts"),
            ],
            [InlineKeyboardButton("⚙️ Настройки", callback_data="settings")],
            [InlineKeyboardButton("🔄 Обновить", callback_data="refresh")],
        ]
    )


def back_kb(target="menu", label="⬅️ Назад"):
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(label, callback_data=target)]]
    )


def history_menu_kb():
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("📅 Сегодня", callback_data="hist:today"),
                InlineKeyboardButton("📅 Вчера", callback_data="hist:yesterday"),
            ],
            [InlineKeyboardButton("📅 Последние 7 дней", callback_data="hist:7d")],
            [InlineKeyboardButton("📅 Этот месяц", callback_data="hist:month")],
            [InlineKeyboardButton("📅 Выбрать дату", callback_data="hist:pick")],
            [InlineKeyboardButton("🏠 Главное меню", callback_data="menu")],
        ]
    )


def calendar_kb(year, month):
    prev_year, prev_month = (year - 1, 12) if month == 1 else (year, month - 1)
    next_year, next_month = (year + 1, 1) if month == 12 else (year, month + 1)

    keyboard = [
        [
            InlineKeyboardButton("◀️", callback_data=f"cal:{prev_year}:{prev_month}"),
            InlineKeyboardButton(f"{RU_MONTHS[month - 1]} {year}", callback_data="ignore"),
            InlineKeyboardButton("▶️", callback_data=f"cal:{next_year}:{next_month}"),
        ],
        [InlineKeyboardButton(d, callback_data="ignore") for d in RU_WEEKDAYS],
    ]

    today = today_local()
    cal = calendar.Calendar(firstweekday=0)
    for week in cal.monthdayscalendar(year, month):
        row = []
        for day in week:
            if day == 0:
                row.append(InlineKeyboardButton("·", callback_data="ignore"))
            else:
                iso = f"{year}-{month:02d}-{day:02d}"
                label = f"[{day}]" if (year, month, day) == (today.year, today.month, today.day) else str(day)
                row.append(InlineKeyboardButton(label, callback_data=f"day:{iso}"))
        keyboard.append(row)

    keyboard.append(
        [
            InlineKeyboardButton("📅 Сегодня", callback_data=f"day:{today.isoformat()}"),
            InlineKeyboardButton("⬅️ Назад", callback_data="history"),
        ]
    )
    return InlineKeyboardMarkup(keyboard)


def day_result_kb(d):
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("⬅️ К календарю", callback_data=f"cal:{d.year}:{d.month}"),
                InlineKeyboardButton("🏠 Меню", callback_data="menu"),
            ]
        ]
    )


# --------------------------------------------------------------------------- #
# Telegram UI — text builders (unified style)
# --------------------------------------------------------------------------- #

def menu_text():
    return (
        "🛡 Security Shift Bot\n"
        "━━━━━━━━━━━━━━\n"
        "Выберите раздел 👇\n\n"
        f"🕒 Обновлено: {datetime.now(APP_TZ).strftime('%H:%M:%S')}"
    )


def build_who_text():
    rows = fetch_on_shift()
    if not rows:
        return "🟢 Сейчас на работе\n━━━━━━━━━━━━━━\nСейчас никто не отмечен на смене."

    text = f"🟢 Сейчас на работе: {len(rows)}\n━━━━━━━━━━━━━━\n"
    for item in rows:
        emp = item.get("employees") or {}
        name = emp.get("full_name", "Без имени")
        position = emp.get("position") or ""
        location = emp.get("location") or ""
        clock_in = fmt_time(item.get("clock_in_time"))

        text += f"\n• {name}"
        if position:
            text += f" — {position}"
        text += f"\n   🕒 с {clock_in}"
        if location:
            text += f" · 📍 {location}"
        text += "\n"
    return text.rstrip()


def build_history_text(date_from, date_to, title):
    rows = fetch_sessions_for_period(date_from, date_to)
    period = fmt_period(date_from, date_to)
    open_count = sum(1 for item in rows if not item.get("clock_out_time"))
    closed_count = len(rows) - open_count
    totals_text = (
        f"⚠️ Открытые смены: {open_count}\n"
        f"✅ Закрытые смены: {closed_count}"
    )

    if not rows:
        return f"{title}\n📅 {period}\n━━━━━━━━━━━━━━\n{totals_text}\n\nНет записей за этот период."

    text = f"{title}\n📅 {period}\n━━━━━━━━━━━━━━\n{totals_text}\n"
    for item in rows:
        emp = item.get("employees") or {}
        name = emp.get("full_name", "Без имени")
        location = emp.get("location") or ""
        clock_in = fmt_time(item.get("clock_in_time"))
        clock_out = fmt_time(item.get("clock_out_time"))
        duration = fmt_duration(item.get("duration_minutes"))

        text += f"\n• {name}\n   {clock_in} → {clock_out} · ⏱ {duration}"
        if not item.get("clock_out_time"):
            text += "\n   ⚠️ Уход не отмечен"
        if location:
            text += f"\n   📍 {location}"
        text += "\n"
    return text.rstrip()


def build_clockins_text():
    rows = fetch_clockins(today_local())

    if not rows:
        return "🟢 Приходы за сегодня\n━━━━━━━━━━━━━━\nСегодня приходов не было."

    text = f"🟢 Приходы за сегодня: {len(rows)}\n━━━━━━━━━━━━━━\n"
    for item in rows:
        emp = item.get("employees") or {}
        name = emp.get("full_name", "Без имени")
        location = emp.get("location") or ""
        clock_in = fmt_time(item.get("clock_in_time"))
        text += f"\n• {name} — 🕒 {clock_in}"
        if location:
            text += f" · 📍 {location}"
    return text


def build_clockouts_text():
    rows = fetch_clockouts(today_local())

    if not rows:
        return "🔴 Уходы за сегодня\n━━━━━━━━━━━━━━\nСегодня уходов не было."

    text = f"🔴 Уходы за сегодня: {len(rows)}\n━━━━━━━━━━━━━━\n"
    for item in rows:
        emp = item.get("employees") or {}
        name = emp.get("full_name", "Без имени")
        clock_out = fmt_time(item.get("clock_out_time"))
        duration = fmt_duration(item.get("duration_minutes"))
        text += f"\n• {name} — 🕒 {clock_out} · ⏱ {duration}"
    return text


def build_settings_text():
    sheets_status = "включён" if sheets_sync.is_ready() else "выключен"
    return (
        "⚙️ Настройки\n"
        "━━━━━━━━━━━━━━\n"
        f"🕒 Часовой пояс: {APP_TZ}\n"
        f"💬 ID группы: {GROUP_ID}\n"
        "🔗 Источник отметок: TimeMoto\n"
        "📊 Хранилище и отчёты: Google Sheets\n"
        f"📄 Google Sheets: {sheets_status}\n\n"
        "Часовой пояс меняется переменной окружения TIMEZONE."
    )


# --------------------------------------------------------------------------- #
# Telegram UI — handlers
# --------------------------------------------------------------------------- #

async def render(update: Update, text: str, keyboard: InlineKeyboardMarkup):
    """Edit the message in place for callbacks, or send a new one for commands."""
    query = update.callback_query
    if query is not None:
        try:
            await query.edit_message_text(text, reply_markup=keyboard)
            return
        except BadRequest as exc:
            if "not modified" in str(exc).lower():
                return
        except Exception:
            logger.exception("edit_message_text failed; sending a new message")
        await query.message.reply_text(text, reply_markup=keyboard)
    else:
        await update.message.reply_text(text, reply_markup=keyboard)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        await update.message.reply_text("⛔ Нет доступа.")
        return
    await update.message.reply_text(menu_text(), reply_markup=main_menu_kb())


async def who_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        await update.message.reply_text("⛔ Нет доступа.")
        return
    await update.message.reply_text(build_who_text(), reply_markup=back_kb())


async def report_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        await update.message.reply_text("⛔ Нет доступа.")
        return
    d = today_local()
    await update.message.reply_text(
        build_history_text(d, d, "📊 Отчёт за сегодня"), reply_markup=back_kb()
    )


async def history_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        await update.message.reply_text("⛔ Нет доступа.")
        return
    await update.message.reply_text(
        "📅 История\n━━━━━━━━━━━━━━\nВыберите период:",
        reply_markup=history_menu_kb(),
    )


async def sync_sheets_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        await update.message.reply_text("⛔ Нет доступа.")
        return

    if not sheets_sync.is_ready():
        await update.message.reply_text(
            "Google Sheets не включён. Проверьте GOOGLE_DRIVE_FOLDER_ID "
            "и Google service account в Railway Variables."
        )
        return

    await update.message.reply_text(
        "Обновляю дневные вкладки, недельные отчёты и итог месяца в Google Sheets..."
    )
    try:
        spreadsheet_id = await asyncio.to_thread(sync_month_to_sheets, today_local(), True)
    except Exception:
        logger.exception("Manual Google Sheets refresh failed")
        await update.message.reply_text("Не получилось обновить Google Sheets. Проверьте логи Railway.")
        return

    if spreadsheet_id:
        await update.message.reply_text(
            "Готово: "
            f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/edit"
        )
    else:
        await update.message.reply_text("Обновление пропущено: Google Sheets не настроен.")


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if not is_allowed(update):
        await query.edit_message_text("⛔ Нет доступа.")
        return

    data = query.data or ""

    if data == "ignore":
        return

    if data in ("menu", "refresh"):
        await render(update, menu_text(), main_menu_kb())
        return

    if data == "who":
        await render(update, build_who_text(), back_kb())
        return

    if data == "report":
        d = today_local()
        await render(update, build_history_text(d, d, "📊 Отчёт за сегодня"), back_kb())
        return

    if data == "clockins":
        await render(update, build_clockins_text(), back_kb())
        return

    if data == "clockouts":
        await render(update, build_clockouts_text(), back_kb())
        return

    if data == "settings":
        await render(update, build_settings_text(), back_kb())
        return

    if data == "history":
        await render(
            update,
            "📅 История\n━━━━━━━━━━━━━━\nВыберите период:",
            history_menu_kb(),
        )
        return

    if data.startswith("hist:"):
        kind = data.split(":", 1)[1]
        today = today_local()

        if kind == "today":
            await render(update, build_history_text(today, today, "📅 История · сегодня"), back_kb("history"))
        elif kind == "yesterday":
            y = today - timedelta(days=1)
            await render(update, build_history_text(y, y, "📅 История · вчера"), back_kb("history"))
        elif kind == "7d":
            await render(update, build_history_text(today - timedelta(days=6), today, "📅 История · последние 7 дней"), back_kb("history"))
        elif kind == "month":
            first = today.replace(day=1)
            await render(update, build_history_text(first, today, "📅 История · этот месяц"), back_kb("history"))
        elif kind == "pick":
            await render(update, "📅 Выберите дату:", calendar_kb(today.year, today.month))
        return

    if data.startswith("cal:"):
        _, year, month = data.split(":")
        await render(update, "📅 Выберите дату:", calendar_kb(int(year), int(month)))
        return

    if data.startswith("day:"):
        iso = data.split(":", 1)[1]
        d = datetime.fromisoformat(iso).date()
        await render(update, build_history_text(d, d, "📅 История"), day_result_kb(d))
        return


def run_flask():
    port = int(os.getenv("PORT", 8080))
    flask_app.run(host="0.0.0.0", port=port)


def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("menu", start))
    app.add_handler(CommandHandler("who", who_cmd))
    app.add_handler(CommandHandler("shift", who_cmd))
    app.add_handler(CommandHandler("report", report_cmd))
    app.add_handler(CommandHandler("history", history_cmd))
    app.add_handler(CommandHandler("syncsheets", sync_sheets_cmd))
    app.add_handler(CallbackQueryHandler(on_callback))

    threading.Thread(target=run_flask, daemon=True).start()
    threading.Thread(target=scheduler_loop, daemon=True).start()

    app.run_polling()


if __name__ == "__main__":
    main()
