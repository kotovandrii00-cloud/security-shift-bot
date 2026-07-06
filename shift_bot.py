import os
import logging
import calendar
import asyncio
import threading
import time
from datetime import datetime, timedelta, timezone
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
from supabase import create_client

from sheets_sync import GoogleSheetsSync, month_bounds


BOT_TOKEN = os.getenv("BOT_TOKEN")
GROUP_ID = os.getenv("GROUP_ID")
SUPABASE_URL = os.getenv("SUPABASE_URL")
# Prefer the service_role key (bypasses RLS); fall back to anon for local/dev.
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY") or os.getenv("SUPABASE_ANON_KEY")
DATA_SOURCE = os.getenv("DATA_SOURCE", "supabase").strip().lower()
AUTO_CLOSE_HOURS = float(os.getenv("AUTO_CLOSE_HOURS", "8"))
AUTO_CLOSE_MINUTES = int(AUTO_CLOSE_HOURS * 60)

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is missing")

if not GROUP_ID:
    raise RuntimeError("GROUP_ID is missing")

if not SUPABASE_URL:
    raise RuntimeError("SUPABASE_URL is missing")

if not SUPABASE_KEY:
    raise RuntimeError("SUPABASE_SERVICE_ROLE_KEY or SUPABASE_ANON_KEY is missing")


# Business timezone. TimeMoto sends local wall-clock time without an offset,
# and "today"/day boundaries for /report and /history are computed here.
APP_TZ = ZoneInfo(os.getenv("TIMEZONE", "Europe/Monaco"))

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("shift_bot")

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
flask_app = Flask(__name__)
sheets_sync = GoogleSheetsSync(APP_TZ, logger)

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


def day_bounds_utc(selected_date):
    """Return [start, end) of a local calendar day as UTC ISO strings."""
    local_start = datetime.fromisoformat(selected_date).replace(tzinfo=APP_TZ)
    local_end = local_start + timedelta(days=1)
    return (
        local_start.astimezone(timezone.utc).isoformat(),
        local_end.astimezone(timezone.utc).isoformat(),
    )


def range_bounds_utc(date_from, date_to):
    """Return [start, end) covering local days date_from..date_to (inclusive)."""
    start = day_bounds_utc(date_from.isoformat())[0]
    end = day_bounds_utc(date_to.isoformat())[1]
    return start, end


def fmt_period(date_from, date_to):
    if date_from == date_to:
        return date_from.strftime("%d.%m.%Y")
    return f"{date_from.strftime('%d.%m')} — {date_to.strftime('%d.%m.%Y')}"


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


def fetch_on_shift_supabase():
    result = (
        supabase.table("attendance_status")
        .select("clock_in_time, employees(full_name, department, position, location)")
        .eq("on_shift", True)
        .order("clock_in_time")
        .execute()
    )
    return result.data or []


def fetch_sessions_supabase(start_utc, end_utc):
    result = (
        supabase.table("attendance_sessions")
        .select(
            "id, clock_in_time, clock_out_time, duration_minutes, source, "
            "employees(full_name, department, position, location)"
        )
        .gte("clock_in_time", start_utc)
        .lt("clock_in_time", end_utc)
        .order("clock_in_time")
        .execute()
    )
    return result.data or []


def fetch_clockins_supabase(start_utc, end_utc):
    result = (
        supabase.table("attendance_sessions")
        .select("clock_in_time, employees(full_name, department, position, location)")
        .gte("clock_in_time", start_utc)
        .lt("clock_in_time", end_utc)
        .order("clock_in_time", desc=True)
        .execute()
    )
    return result.data or []


def fetch_clockouts_supabase(start_utc, end_utc):
    result = (
        supabase.table("attendance_sessions")
        .select("clock_out_time, duration_minutes, employees(full_name, department, position)")
        .gte("clock_out_time", start_utc)
        .lt("clock_out_time", end_utc)
        .order("clock_out_time", desc=True)
        .execute()
    )
    return result.data or []


def read_from_sheets():
    return DATA_SOURCE == "sheets" and sheets_sync.is_ready()


def fetch_on_shift():
    if read_from_sheets():
        try:
            rows = sheets_sync.day_rows_for_bot(today_local())
            return [row for row in rows if row.get("status") == "Открыта"]
        except Exception:
            logger.exception("Google Sheets read failed; falling back to Supabase")
    return fetch_on_shift_supabase()


def fetch_sessions_for_period(date_from, date_to):
    if read_from_sheets():
        try:
            return sheets_sync.range_rows_for_bot(date_from, date_to)
        except Exception:
            logger.exception("Google Sheets history read failed; falling back to Supabase")

    start_utc, end_utc = range_bounds_utc(date_from, date_to)
    return fetch_sessions_supabase(start_utc, end_utc)


def fetch_clockins(start_utc, end_utc):
    if read_from_sheets():
        try:
            return [
                row for row in sheets_sync.day_rows_for_bot(today_local())
                if row.get("clock_in_time")
            ]
        except Exception:
            logger.exception("Google Sheets clock-in read failed; falling back to Supabase")
    return fetch_clockins_supabase(start_utc, end_utc)


def fetch_clockouts(start_utc, end_utc):
    if read_from_sheets():
        try:
            return [
                row for row in sheets_sync.day_rows_for_bot(today_local())
                if row.get("clock_out_time")
            ]
        except Exception:
            logger.exception("Google Sheets clock-out read failed; falling back to Supabase")
    return fetch_clockouts_supabase(start_utc, end_utc)


def sync_day_to_sheets(selected_date):
    if not sheets_sync.is_ready():
        return None

    start_utc, end_utc = day_bounds_utc(selected_date.isoformat())
    rows = fetch_sessions_supabase(start_utc, end_utc)
    return sheets_sync.sync_day(selected_date, rows)


def sync_month_to_sheets(selected_date, sync_days=True):
    if not sheets_sync.is_ready():
        return None

    first_day, last_day = month_bounds(selected_date)
    if sync_days:
        current = first_day
        stop_at = min(last_day, today_local())
        while current <= stop_at:
            start_utc, end_utc = day_bounds_utc(current.isoformat())
            rows = fetch_sessions_supabase(start_utc, end_utc)
            if rows or current == today_local():
                sheets_sync.sync_day(current, rows)
            current += timedelta(days=1)

    start_utc, end_utc = range_bounds_utc(first_day, last_day)
    rows = fetch_sessions_supabase(start_utc, end_utc)
    return sheets_sync.sync_month_summary(selected_date, rows)


def auto_close_stale_sessions():
    """Close sessions left open before today with a fixed 8-hour duration."""
    today = today_local()
    today_start_utc = day_bounds_utc(today.isoformat())[0]
    result = (
        supabase.table("attendance_sessions")
        .select("id, employee_id, clock_in_time")
        .is_("clock_out_time", "null")
        .lt("clock_in_time", today_start_utc)
        .execute()
    )

    rows = result.data or []
    now_iso = datetime.now(timezone.utc).isoformat()
    changed_dates = set()

    for row in rows:
        clock_in_dt = parse_timestamp(row.get("clock_in_time"))
        if not clock_in_dt:
            continue

        local_clock_in = clock_in_dt.astimezone(APP_TZ)
        clock_out_dt = local_clock_in + timedelta(minutes=AUTO_CLOSE_MINUTES)
        clock_out_iso = clock_out_dt.astimezone(timezone.utc).isoformat()

        supabase.table("attendance_sessions").update(
            {
                "clock_out_time": clock_out_iso,
                "duration_minutes": AUTO_CLOSE_MINUTES,
                "source": "timemoto_auto_8h",
            }
        ).eq("id", row["id"]).execute()

        set_attendance_status(
            employee_id=row["employee_id"],
            on_shift=False,
            clock_in_iso=row.get("clock_in_time"),
            clock_out_iso=clock_out_iso,
            now_iso=now_iso,
        )
        changed_dates.add(local_clock_in.date())

    for changed_date in sorted(changed_dates):
        try:
            sync_day_to_sheets(changed_date)
        except Exception:
            logger.exception("Google Sheets day sync failed after auto-close")

    for changed_date in sorted({d.replace(day=1) for d in changed_dates}):
        try:
            sync_month_to_sheets(changed_date, sync_days=False)
        except Exception:
            logger.exception("Google Sheets month summary sync failed after auto-close")

    if rows:
        logger.info("Auto-closed %s stale attendance session(s)", len(rows))
    return len(rows)


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
# Webhook (TimeMoto -> Supabase + Telegram)
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

    clock_dt = parse_timestamp(time_logged) or datetime.now(APP_TZ)
    clock_iso = clock_dt.astimezone(timezone.utc).isoformat()
    time_display = clock_dt.astimezone(APP_TZ).strftime("%H:%M")

    # Persist to Supabase, but never let a DB error block the notification.
    duration_minutes = None
    persisted = False
    try:
        duration_minutes = persist_attendance(
            user_id, full_name, employee_number, department, location,
            clock_type, clock_iso, clock_dt,
        )
        persisted = True
    except Exception:
        logger.exception("Supabase write failed for TimeMoto webhook")

    if persisted:
        try:
            local_event_date = clock_dt.astimezone(APP_TZ).date()
            dates_to_sync = {local_event_date}
            if clock_type == "Out":
                dates_to_sync.add(local_event_date - timedelta(days=1))
            for selected_date in sorted(dates_to_sync):
                sync_day_to_sheets(selected_date)
            for selected_month in sorted({d.replace(day=1) for d in dates_to_sync}):
                sync_month_to_sheets(selected_month, sync_days=False)
        except Exception:
            logger.exception("Google Sheets sync failed for TimeMoto webhook")

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

    if not rows:
        return f"{title}\n📅 {period}\n━━━━━━━━━━━━━━\nНет записей за этот период."

    text = f"{title}\n📅 {period}\n━━━━━━━━━━━━━━\n"
    for item in rows:
        emp = item.get("employees") or {}
        name = emp.get("full_name", "Без имени")
        location = emp.get("location") or ""
        clock_in = fmt_time(item.get("clock_in_time"))
        clock_out = fmt_time(item.get("clock_out_time"))
        duration = fmt_duration(item.get("duration_minutes"))

        text += f"\n• {name}\n   {clock_in} → {clock_out} · ⏱ {duration}"
        if location:
            text += f"\n   📍 {location}"
        text += "\n"
    return text.rstrip()


def build_clockins_text():
    start_utc, end_utc = day_bounds_utc(today_local().isoformat())
    rows = fetch_clockins(start_utc, end_utc)

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
    start_utc, end_utc = day_bounds_utc(today_local().isoformat())
    rows = fetch_clockouts(start_utc, end_utc)

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
    report_source = "Google Sheets" if read_from_sheets() else "Supabase"
    sheets_status = "включён" if sheets_sync.is_ready() else "выключен"
    return (
        "⚙️ Настройки\n"
        "━━━━━━━━━━━━━━\n"
        f"🕒 Часовой пояс: {APP_TZ}\n"
        f"💬 ID группы: {GROUP_ID}\n"
        "🔗 Источник отметок: TimeMoto\n"
        f"📊 Источник отчётов: {report_source}\n"
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
            "Google Sheets не включён. Проверьте SHEETS_ENABLED, "
            "GOOGLE_DRIVE_FOLDER_ID и Google service account в Railway Variables."
        )
        return

    await update.message.reply_text("Начинаю синхронизацию текущего месяца в Google Sheets...")
    try:
        spreadsheet_id = await asyncio.to_thread(sync_month_to_sheets, today_local(), True)
    except Exception:
        logger.exception("Manual Google Sheets sync failed")
        await update.message.reply_text("Не получилось синхронизировать Google Sheets. Проверьте логи Railway.")
        return

    if spreadsheet_id:
        await update.message.reply_text(
            "Готово: "
            f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/edit"
        )
    else:
        await update.message.reply_text("Синхронизация пропущена: Google Sheets не настроен.")


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
