import threading
from flask import Flask, request, jsonify
import os
import calendar
from datetime import datetime, date, time, timedelta, timezone

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)
from supabase import create_client

BOT_TOKEN = os.getenv("BOT_TOKEN")
GROUP_ID = os.getenv("GROUP_ID")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_ANON_KEY = os.getenv("SUPABASE_ANON_KEY")

supabase = create_client(SUPABASE_URL, SUPABASE_ANON_KEY)
flask_app = Flask(__name__)

@flask_app.route("/timemoto", methods=["POST"])
def timemoto_webhook():
    data = request.get_json(silent=True) or {}

    print("TIMEMOTO WEBHOOK RECEIVED:")
    print(data)

    try:
        import requests

        text = "📩 TimeMoto webhook received\n\n"
        text += str(data)[:3000]

        requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={
                "chat_id": GROUP_ID,
                "text": text
            },
            timeout=10
        )
    except Exception as e:
        print("Telegram send error:", e)

    return jsonify({"ok": True})


def is_allowed(update: Update) -> bool:
    return not GROUP_ID or str(update.effective_chat.id) == GROUP_ID


def fmt_time(value):
    if not value:
        return "—"
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt.strftime("%H:%M")
    except Exception:
        return str(value)


def build_calendar(year: int, month: int):
    month_name = calendar.month_name[month]
    keyboard = [[InlineKeyboardButton(f"{month_name} {year}", callback_data="ignore")]]

    keyboard.append([
        InlineKeyboardButton("Mon", callback_data="ignore"),
        InlineKeyboardButton("Tue", callback_data="ignore"),
        InlineKeyboardButton("Wed", callback_data="ignore"),
        InlineKeyboardButton("Thu", callback_data="ignore"),
        InlineKeyboardButton("Fri", callback_data="ignore"),
        InlineKeyboardButton("Sat", callback_data="ignore"),
        InlineKeyboardButton("Sun", callback_data="ignore"),
    ])

    cal = calendar.Calendar(firstweekday=0)
    for week in cal.monthdayscalendar(year, month):
        row = []
        for day in week:
            if day == 0:
                row.append(InlineKeyboardButton(" ", callback_data="ignore"))
            else:
                d = f"{year}-{month:02d}-{day:02d}"
                row.append(InlineKeyboardButton(str(day), callback_data=f"history:{d}"))
        keyboard.append(row)

    prev_month = month - 1 or 12
    prev_year = year if month > 1 else year - 1
    next_month = month + 1 if month < 12 else 1
    next_year = year if month < 12 else year + 1

    keyboard.append([
        InlineKeyboardButton("◀️", callback_data=f"cal:{prev_year}:{prev_month}"),
        InlineKeyboardButton("Today", callback_data=f"history:{date.today().isoformat()}"),
        InlineKeyboardButton("▶️", callback_data=f"cal:{next_year}:{next_month}"),
    ])

    return InlineKeyboardMarkup(keyboard)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "✅ Staff Control Bot работает\n\n"
        "Команды:\n"
        "/who — кто сейчас на работе\n"
        "/report — отчёт за сегодня\n"
        "/history — календарь истории"
    )


async def who(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        await update.message.reply_text("⛔ Нет доступа.")
        return

    result = (
        supabase.table("attendance_status")
        .select("clock_in_time, employees(full_name, position, location)")
        .eq("on_shift", True)
        .execute()
    )

    data = result.data or []

    if not data:
        await update.message.reply_text("🔴 Сейчас никто не отмечен на работе.")
        return

    text = f"🟢 Сейчас на работе: {len(data)}\n\n"

    for item in data:
        employee = item.get("employees") or {}
        name = employee.get("full_name", "Без имени")
        location = employee.get("location") or ""
        clock_in = fmt_time(item.get("clock_in_time"))

        text += f"• {name} — с {clock_in}"
        if location:
            text += f" | {location}"
        text += "\n"

    await update.message.reply_text(text)


async def report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_history_for_date(update, context, date.today().isoformat(), title="📋 Отчёт за сегодня")


async def history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        await update.message.reply_text("⛔ Нет доступа.")
        return

    today = date.today()
    await update.message.reply_text(
        "📅 Выберите дату:",
        reply_markup=build_calendar(today.year, today.month)
    )


async def send_history_for_date(update: Update, context: ContextTypes.DEFAULT_TYPE, day: str, title=None):
    if not is_allowed(update):
        await update.message.reply_text("⛔ Нет доступа.")
        return

    start_dt = f"{day}T00:00:00+00:00"
    end_day = (datetime.fromisoformat(day) + timedelta(days=1)).date().isoformat()
    end_dt = f"{end_day}T00:00:00+00:00"

    result = (
        supabase.table("attendance_sessions")
        .select("clock_in_time, clock_out_time, duration_minutes, employees(full_name, location)")
        .gte("clock_in_time", start_dt)
        .lt("clock_in_time", end_dt)
        .order("clock_in_time")
        .execute()
    )

    data = result.data or []

    if not data:
        message = f"📅 {day}\n\nНет записей за эту дату."
    else:
        message = f"{title or '📅 История'}\n{day}\n\n"

        for item in data:
            employee = item.get("employees") or {}
            name = employee.get("full_name", "Без имени")
            location = employee.get("location") or ""
            in_time = fmt_time(item.get("clock_in_time"))
            out_time = fmt_time(item.get("clock_out_time"))
            minutes = item.get("duration_minutes")

            if minutes:
                h = minutes // 60
                m = minutes % 60
                duration = f"{h}ч {m:02d}м"
            else:
                duration = "смена открыта"

            message += f"• {name}\n"
            if location:
                message += f"  📍 {location}\n"
            message += f"  {in_time} → {out_time}\n"
            message += f"  ⏱ {duration}\n\n"

    if update.callback_query:
        await update.callback_query.message.reply_text(message)
    else:
        await update.message.reply_text(message)


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data = query.data

    if data == "ignore":
        return

    if data.startswith("cal:"):
        _, year, month = data.split(":")
        await query.edit_message_reply_markup(
            reply_markup=build_calendar(int(year), int(month))
        )
        return

    if data.startswith("history:"):
        _, selected_date = data.split(":")
        await send_history_for_date(update, context, selected_date)


def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("who", who))
    app.add_handler(CommandHandler("shift", who))
    app.add_handler(CommandHandler("report", report))
    app.add_handler(CommandHandler("history", history))
    app.add_handler(CallbackQueryHandler(button_handler))
    
def run_flask():
    port = int(os.getenv("PORT", 8080))
    flask_app.run(host="0.0.0.0", port=port)

threading.Thread(target=run_flask, daemon=True).start()

    app.run_polling()


if __name__ == "__main__":
    main()
