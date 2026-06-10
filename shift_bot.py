import os
import logging
import calendar
import threading
from datetime import datetime, date, timedelta, timezone

import requests
from flask import Flask, request, jsonify
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

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is missing")

if not GROUP_ID:
    raise RuntimeError("GROUP_ID is missing")

if not SUPABASE_URL:
    raise RuntimeError("SUPABASE_URL is missing")

if not SUPABASE_ANON_KEY:
    raise RuntimeError("SUPABASE_ANON_KEY is missing")


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("shift_bot")

supabase = create_client(SUPABASE_URL, SUPABASE_ANON_KEY)
flask_app = Flask(__name__)


def is_allowed(update: Update) -> bool:
    return str(update.effective_chat.id) == str(GROUP_ID)


def fmt_time(value):
    if not value:
        return "—"
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt.strftime("%H:%M")
    except Exception:
        return str(value)


def fmt_duration(minutes):
    if not minutes:
        return "смена открыта"
    hours = int(minutes) // 60
    mins = int(minutes) % 60
    return f"{hours}ч {mins:02d}м"


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
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


# --------------------------------------------------------------------------- #
# Supabase helpers
# --------------------------------------------------------------------------- #

def find_employee_by_timemoto_id(timemoto_user_id):
    if not timemoto_user_id:
        return None
    result = (
        supabase.table("employees")
        .select("id, full_name, timemoto_user_id")
        .eq("timemoto_user_id", timemoto_user_id)
        .limit(1)
        .execute()
    )
    rows = result.data or []
    return rows[0] if rows else None


def find_employee_by_name(full_name):
    if not full_name:
        return None
    result = (
        supabase.table("employees")
        .select("id, full_name, timemoto_user_id")
        .eq("full_name", full_name)
        .limit(1)
        .execute()
    )
    rows = result.data or []
    return rows[0] if rows else None


def upsert_employee(user_id, full_name, employee_number, department, location):
    """Find an employee by TimeMoto user id (fallback: full_name).
    Update their data if found, otherwise create a new record.
    Returns the employee row or None.
    """
    fields = {
        "full_name": full_name,
        "employee_number": employee_number,
        "position": department,
        "department": department,
        "location": location or "",
    }

    existing = find_employee_by_timemoto_id(user_id)
    if not existing:
        existing = find_employee_by_name(full_name)

    if existing:
        update_fields = dict(fields)
        # Link manually-created records to TimeMoto on first match by name.
        if user_id and not existing.get("timemoto_user_id"):
            update_fields["timemoto_user_id"] = user_id

        result = (
            supabase.table("employees")
            .update(update_fields)
            .eq("id", existing["id"])
            .execute()
        )
        rows = result.data or []
        return rows[0] if rows else existing

    insert_fields = dict(fields)
    insert_fields["timemoto_user_id"] = user_id
    insert_fields["is_active"] = True

    result = supabase.table("employees").insert(insert_fields).execute()
    rows = result.data or []
    return rows[0] if rows else None


def get_open_session(employee_id):
    result = (
        supabase.table("attendance_sessions")
        .select("id, clock_in_time")
        .eq("employee_id", employee_id)
        .is_("clock_out_time", "null")
        .order("clock_in_time", desc=True)
        .limit(1)
        .execute()
    )
    rows = result.data or []
    return rows[0] if rows else None


def close_open_sessions(employee_id, clock_out_iso):
    """Close any dangling open sessions for an employee (e.g. a missed Out)."""
    result = (
        supabase.table("attendance_sessions")
        .select("id, clock_in_time")
        .eq("employee_id", employee_id)
        .is_("clock_out_time", "null")
        .execute()
    )

    clock_out_dt = parse_timestamp(clock_out_iso)

    for row in result.data or []:
        clock_in_dt = parse_timestamp(row.get("clock_in_time"))
        duration = None
        if clock_in_dt and clock_out_dt:
            duration = int((clock_out_dt - clock_in_dt).total_seconds() // 60)

        supabase.table("attendance_sessions").update(
            {
                "clock_out_time": clock_out_iso,
                "duration_minutes": duration,
            }
        ).eq("id", row["id"]).execute()


def set_attendance_status(employee_id, on_shift, clock_in_iso, clock_out_iso, now_iso):
    supabase.table("attendance_status").upsert(
        {
            "employee_id": employee_id,
            "on_shift": on_shift,
            "clock_in_time": clock_in_iso,
            "clock_out_time": clock_out_iso,
            "last_event_time": now_iso,
        },
        on_conflict="employee_id",
    ).execute()


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #

@flask_app.route("/", methods=["GET"])
def home():
    return jsonify({"ok": True, "service": "staff-control-bot"})


def persist_attendance(
    user_id, full_name, employee_number, department, location,
    clock_type, clock_iso, clock_dt,
):
    """Write the TimeMoto event to Supabase.

    Returns duration_minutes for an Out event (or None). Raises on DB errors so
    the caller can still send the Telegram notification.
    """
    now_iso = datetime.now(timezone.utc).isoformat()

    employee = upsert_employee(
        user_id=user_id,
        full_name=full_name,
        employee_number=employee_number,
        department=department,
        location=location,
    )
    if not employee:
        raise RuntimeError("employee upsert returned no row")

    employee_id = employee["id"]
    duration_minutes = None

    if clock_type == "In":
        # Close any stale open session before opening a new one.
        close_open_sessions(employee_id, clock_iso)

        supabase.table("attendance_sessions").insert(
            {
                "employee_id": employee_id,
                "clock_in_time": clock_iso,
                "source": "timemoto",
            }
        ).execute()

        set_attendance_status(
            employee_id=employee_id,
            on_shift=True,
            clock_in_iso=clock_iso,
            clock_out_iso=None,
            now_iso=now_iso,
        )

    elif clock_type == "Out":
        session = get_open_session(employee_id)

        set_attendance_status(
            employee_id=employee_id,
            on_shift=False,
            clock_in_iso=(session or {}).get("clock_in_time"),
            clock_out_iso=clock_iso,
            now_iso=now_iso,
        )

        if session:
            clock_in_dt = parse_timestamp(session.get("clock_in_time"))
            if clock_in_dt:
                duration_minutes = int((clock_dt - clock_in_dt).total_seconds() // 60)

            supabase.table("attendance_sessions").update(
                {
                    "clock_out_time": clock_iso,
                    "duration_minutes": duration_minutes,
                }
            ).eq("id", session["id"]).execute()

    return duration_minutes


@flask_app.route("/timemoto", methods=["GET", "POST"])
def timemoto_webhook():

    if request.method == "GET":
        return jsonify({"ok": True})

    payload = request.get_json(silent=True) or {}

    event = payload.get("event")
    data = payload.get("data", {})

    if event != "attendance.inserted":
        return jsonify({"ok": True, "skipped": "event"})

    user_id = data.get("userId")
    employee_number = data.get("userEmployeeNumber")
    full_name = data.get("userFullName", "Unknown")
    department = data.get("departmentName", "")
    location = data.get("locationName", "")
    clock_type = data.get("clockingType", "")
    time_logged = data.get("timeLogged", "")

    clock_dt = parse_timestamp(time_logged) or datetime.now(timezone.utc)
    clock_iso = clock_dt.isoformat()
    time_display = clock_dt.strftime("%H:%M")

    # Persist to Supabase, but never let a DB error block the notification.
    duration_minutes = None
    try:
        duration_minutes = persist_attendance(
            user_id, full_name, employee_number, department, location,
            clock_type, clock_iso, clock_dt,
        )
    except Exception:
        logger.exception("Supabase write failed for TimeMoto webhook")

    # Telegram notification is always sent.
    if clock_type == "In":
        send_telegram_message(
            f"🟢 Приход\n\n"
            f"👤 {full_name}\n"
            f"🏢 {department}\n"
            f"🕒 {time_display}"
        )
    elif clock_type == "Out":
        send_telegram_message(
            f"🔴 Уход\n\n"
            f"👤 {full_name}\n"
            f"🏢 {department}\n"
            f"🕒 {time_display}\n"
            f"⏱ {fmt_duration(duration_minutes)}"
        )

    # Always 200 so TimeMoto does not retry-storm on transient DB issues.
    return jsonify({"ok": True})


# --------------------------------------------------------------------------- #
# Telegram UI
# --------------------------------------------------------------------------- #

def build_calendar(year: int, month: int):
    month_name = calendar.month_name[month]

    keyboard = [
        [InlineKeyboardButton(f"{month_name} {year}", callback_data="ignore")]
    ]

    keyboard.append(
        [
            InlineKeyboardButton("Mon", callback_data="ignore"),
            InlineKeyboardButton("Tue", callback_data="ignore"),
            InlineKeyboardButton("Wed", callback_data="ignore"),
            InlineKeyboardButton("Thu", callback_data="ignore"),
            InlineKeyboardButton("Fri", callback_data="ignore"),
            InlineKeyboardButton("Sat", callback_data="ignore"),
            InlineKeyboardButton("Sun", callback_data="ignore"),
        ]
    )

    cal = calendar.Calendar(firstweekday=0)

    for week in cal.monthdayscalendar(year, month):
        row = []
        for day in week:
            if day == 0:
                row.append(InlineKeyboardButton(" ", callback_data="ignore"))
            else:
                selected_date = f"{year}-{month:02d}-{day:02d}"
                row.append(
                    InlineKeyboardButton(
                        str(day),
                        callback_data=f"history:{selected_date}",
                    )
                )
        keyboard.append(row)

    prev_month = month - 1 or 12
    prev_year = year if month > 1 else year - 1

    next_month = month + 1 if month < 12 else 1
    next_year = year if month < 12 else year + 1

    keyboard.append(
        [
            InlineKeyboardButton("◀️", callback_data=f"cal:{prev_year}:{prev_month}"),
            InlineKeyboardButton("Today", callback_data=f"history:{date.today().isoformat()}"),
            InlineKeyboardButton("▶️", callback_data=f"cal:{next_year}:{next_month}"),
        ]
    )

    return InlineKeyboardMarkup(keyboard)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "✅ Staff Control Bot работает\n\n"
        "Команды:\n"
        "/who — кто сейчас на работе\n"
        "/shift — кто сейчас на работе\n"
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
    if not is_allowed(update):
        await update.message.reply_text("⛔ Нет доступа.")
        return

    await send_history_for_date(
        update=update,
        context=context,
        selected_date=date.today().isoformat(),
        title="📋 Отчёт за сегодня",
    )


async def history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        await update.message.reply_text("⛔ Нет доступа.")
        return

    today = date.today()

    await update.message.reply_text(
        "📅 Выберите дату:",
        reply_markup=build_calendar(today.year, today.month),
    )


async def send_history_for_date(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    selected_date: str,
    title: str = "📅 История",
):
    start_dt = f"{selected_date}T00:00:00+00:00"
    end_date = (datetime.fromisoformat(selected_date) + timedelta(days=1)).date()
    end_dt = f"{end_date.isoformat()}T00:00:00+00:00"

    result = (
        supabase.table("attendance_sessions")
        .select(
            "clock_in_time, clock_out_time, duration_minutes, "
            "employees(full_name, location)"
        )
        .gte("clock_in_time", start_dt)
        .lt("clock_in_time", end_dt)
        .order("clock_in_time")
        .execute()
    )

    data = result.data or []

    if not data:
        message = f"📅 {selected_date}\n\nНет записей за эту дату."
    else:
        message = f"{title}\n{selected_date}\n\n"

        for item in data:
            employee = item.get("employees") or {}
            name = employee.get("full_name", "Без имени")
            location = employee.get("location") or ""
            clock_in = fmt_time(item.get("clock_in_time"))
            clock_out = fmt_time(item.get("clock_out_time"))
            duration = fmt_duration(item.get("duration_minutes"))

            message += f"• {name}\n"
            if location:
                message += f"  📍 {location}\n"
            message += f"  {clock_in} → {clock_out}\n"
            message += f"  ⏱ {duration}\n\n"

    if update.callback_query:
        await update.callback_query.message.reply_text(message)
    else:
        await update.message.reply_text(message)


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    callback_data = query.data

    if callback_data == "ignore":
        return

    if callback_data.startswith("cal:"):
        _, year, month = callback_data.split(":")
        await query.edit_message_reply_markup(
            reply_markup=build_calendar(int(year), int(month))
        )
        return

    if callback_data.startswith("history:"):
        _, selected_date = callback_data.split(":")
        await send_history_for_date(
            update=update,
            context=context,
            selected_date=selected_date,
        )


def run_flask():
    port = int(os.getenv("PORT", 8080))
    flask_app.run(host="0.0.0.0", port=port)


def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("who", who))
    app.add_handler(CommandHandler("shift", who))
    app.add_handler(CommandHandler("report", report))
    app.add_handler(CommandHandler("history", history))
    app.add_handler(CallbackQueryHandler(button_handler))

    threading.Thread(target=run_flask, daemon=True).start()

    app.run_polling()


if __name__ == "__main__":
    main()
