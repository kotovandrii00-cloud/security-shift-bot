import base64
import json
import os
from collections import defaultdict
from datetime import datetime, timedelta


RU_MONTHS = [
    "Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
    "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь",
]

DAY_HEADERS = [
    "Отдел",
    "Сотрудник",
    "Дата",
    "Приход",
    "Уход",
    "Отработано",
]

SUMMARY_HEADERS = [
    "Отдел",
    "Сотрудник",
    "Смен",
    "Отработано за месяц",
]

SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/spreadsheets",
]


def env_bool(name, default=False):
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def parse_timestamp(value, app_tz):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=app_tz)
    return dt


def month_file_name(selected_date):
    return f"{RU_MONTHS[selected_date.month - 1]} {selected_date.year}"


def day_sheet_name(selected_date):
    return selected_date.strftime("%d.%m.%Y")


def month_bounds(selected_date):
    first = selected_date.replace(day=1)
    if selected_date.month == 12:
        next_month = selected_date.replace(year=selected_date.year + 1, month=1, day=1)
    else:
        next_month = selected_date.replace(month=selected_date.month + 1, day=1)
    return first, next_month - timedelta(days=1)


def worksheet_title_for_a1(title):
    return "'" + title.replace("'", "''") + "'"


def fmt_duration_label(minutes):
    if minutes is None:
        return ""
    minutes = int(minutes)
    hours = minutes // 60
    mins = minutes % 60
    return f"{hours}ч {mins:02d}м"


def parse_duration_label(value):
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None

    normalized = text.replace(",", ".").lower()
    if "ч" in normalized:
        hours_part, _, rest = normalized.partition("ч")
        mins_part = rest.replace("м", "").strip() or "0"
        try:
            return int(float(hours_part.strip()) * 60) + int(float(mins_part))
        except ValueError:
            return None

    try:
        return int(float(normalized) * 60)
    except ValueError:
        return None


def parse_date_label(value):
    if not value:
        return None
    try:
        return datetime.strptime(str(value).strip(), "%d.%m.%Y").date()
    except ValueError:
        return None


def parse_clock_minutes(value):
    if not value:
        return None
    text = str(value).strip()
    if not text or ":" not in text:
        return None
    hours, _, minutes = text.partition(":")
    try:
        return int(hours) * 60 + int(minutes)
    except ValueError:
        return None


def normalize_name(value):
    return " ".join(str(value or "").strip().lower().split())


def build_credentials():
    raw_json = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
    raw_b64 = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON_B64")
    file_path = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE")

    try:
        from google.oauth2 import service_account
    except ImportError as exc:
        raise RuntimeError(
            "Google libraries are not installed. Add google-api-python-client "
            "and google-auth to requirements.txt."
        ) from exc

    if raw_json:
        info = json.loads(raw_json)
        return service_account.Credentials.from_service_account_info(info, scopes=SCOPES)

    if raw_b64:
        info = json.loads(base64.b64decode(raw_b64).decode("utf-8"))
        return service_account.Credentials.from_service_account_info(info, scopes=SCOPES)

    if file_path:
        return service_account.Credentials.from_service_account_file(file_path, scopes=SCOPES)

    raise RuntimeError(
        "GOOGLE_SERVICE_ACCOUNT_JSON, GOOGLE_SERVICE_ACCOUNT_JSON_B64, "
        "or GOOGLE_SERVICE_ACCOUNT_FILE is missing"
    )


class GoogleSheetsSync:
    def __init__(self, app_tz, logger):
        self.app_tz = app_tz
        self.logger = logger
        self.enabled = env_bool("SHEETS_ENABLED", True)
        self.folder_id = os.getenv("GOOGLE_DRIVE_FOLDER_ID")
        self._drive = None
        self._sheets = None
        self._layout_ready = set()

    def is_ready(self):
        return self.enabled and bool(self.folder_id)

    def _services(self):
        if self._drive and self._sheets:
            return self._drive, self._sheets

        if not self.folder_id:
            raise RuntimeError("GOOGLE_DRIVE_FOLDER_ID is missing")

        try:
            from googleapiclient.discovery import build
        except ImportError as exc:
            raise RuntimeError(
                "google-api-python-client is not installed. "
                "Add it to requirements.txt."
            ) from exc

        credentials = build_credentials()
        self._drive = build("drive", "v3", credentials=credentials, cache_discovery=False)
        self._sheets = build("sheets", "v4", credentials=credentials, cache_discovery=False)
        return self._drive, self._sheets

    def _find_spreadsheet(self, selected_date):
        drive, _ = self._services()
        title = month_file_name(selected_date)
        escaped_title = title.replace("\\", "\\\\").replace("'", "\\'")
        query = (
            f"name = '{escaped_title}' "
            "and mimeType = 'application/vnd.google-apps.spreadsheet' "
            f"and '{self.folder_id}' in parents and trashed = false"
        )
        result = drive.files().list(
            q=query,
            spaces="drive",
            fields="files(id, name)",
            pageSize=10,
        ).execute()

        files = result.get("files", [])
        if files:
            return files[0]["id"]
        return None

    def _find_or_create_spreadsheet(self, selected_date):
        spreadsheet_id = self._find_spreadsheet(selected_date)
        if spreadsheet_id:
            return spreadsheet_id

        drive, _ = self._services()
        title = month_file_name(selected_date)
        created = drive.files().create(
            body={
                "name": title,
                "mimeType": "application/vnd.google-apps.spreadsheet",
                "parents": [self.folder_id],
            },
            fields="id",
        ).execute()
        spreadsheet_id = created["id"]
        self.logger.info("Created Google spreadsheet %s for %s", spreadsheet_id, title)
        return spreadsheet_id

    def _spreadsheet_meta(self, spreadsheet_id):
        _, sheets = self._services()
        return sheets.spreadsheets().get(
            spreadsheetId=spreadsheet_id,
            fields="sheets(properties(sheetId,title,index,gridProperties))",
        ).execute()

    def _ensure_sheet(self, spreadsheet_id, title):
        _, sheets = self._services()
        meta = self._spreadsheet_meta(spreadsheet_id)
        existing = {
            item["properties"]["title"]: item["properties"]["sheetId"]
            for item in meta.get("sheets", [])
        }
        if title in existing:
            return existing[title]

        if "Sheet1" in existing and len(existing) == 1:
            sheet_id = existing["Sheet1"]
            sheets.spreadsheets().batchUpdate(
                spreadsheetId=spreadsheet_id,
                body={
                    "requests": [
                        {
                            "updateSheetProperties": {
                                "properties": {"sheetId": sheet_id, "title": title},
                                "fields": "title",
                            }
                        }
                    ]
                },
            ).execute()
            return sheet_id

        response = sheets.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={"requests": [{"addSheet": {"properties": {"title": title}}}]},
        ).execute()
        return response["replies"][0]["addSheet"]["properties"]["sheetId"]

    def _write_values(self, spreadsheet_id, title, values):
        _, sheets = self._services()
        a1_title = worksheet_title_for_a1(title)
        sheets.spreadsheets().values().clear(
            spreadsheetId=spreadsheet_id,
            range=f"{a1_title}!A:Z",
            body={},
        ).execute()
        sheets.spreadsheets().values().update(
            spreadsheetId=spreadsheet_id,
            range=f"{a1_title}!A1",
            valueInputOption="USER_ENTERED",
            body={"values": values},
        ).execute()

    def _format_table(self, spreadsheet_id, sheet_id, row_count, col_count, open_row_indexes=None):
        _, sheets = self._services()
        open_row_indexes = open_row_indexes or []
        reset_rows = max(row_count + 20, 200)
        requests = [
            {
                "updateSheetProperties": {
                    "properties": {
                        "sheetId": sheet_id,
                        "gridProperties": {"frozenRowCount": 1},
                    },
                    "fields": "gridProperties.frozenRowCount",
                }
            },
            {
                "repeatCell": {
                    "range": {
                        "sheetId": sheet_id,
                        "startRowIndex": 0,
                        "endRowIndex": reset_rows,
                        "startColumnIndex": 0,
                        "endColumnIndex": 26,
                    },
                    "cell": {
                        "userEnteredFormat": {
                            "backgroundColor": {"red": 1, "green": 1, "blue": 1},
                            "textFormat": {
                                "bold": False,
                                "underline": False,
                                "foregroundColor": {"red": 0, "green": 0, "blue": 0},
                            },
                        }
                    },
                    "fields": "userEnteredFormat(backgroundColor,textFormat)",
                }
            },
            {
                "repeatCell": {
                    "range": {
                        "sheetId": sheet_id,
                        "startRowIndex": 0,
                        "endRowIndex": 1,
                        "startColumnIndex": 0,
                        "endColumnIndex": col_count,
                    },
                    "cell": {
                        "userEnteredFormat": {
                            "backgroundColor": {"red": 1, "green": 1, "blue": 1},
                            "textFormat": {
                                "bold": True,
                                "underline": False,
                                "foregroundColor": {"red": 0, "green": 0, "blue": 0},
                            },
                        }
                    },
                    "fields": "userEnteredFormat(backgroundColor,textFormat)",
                }
            },
            {
                "setBasicFilter": {
                    "filter": {
                        "range": {
                            "sheetId": sheet_id,
                            "startRowIndex": 0,
                            "endRowIndex": max(row_count, 1),
                            "startColumnIndex": 0,
                            "endColumnIndex": col_count,
                        }
                    }
                }
            },
            {
                "autoResizeDimensions": {
                    "dimensions": {
                        "sheetId": sheet_id,
                        "dimension": "COLUMNS",
                        "startIndex": 0,
                        "endIndex": col_count,
                    }
                }
            },
        ]

        for row_index in open_row_indexes:
            requests.append(
                {
                    "repeatCell": {
                        "range": {
                            "sheetId": sheet_id,
                            "startRowIndex": row_index,
                            "endRowIndex": row_index + 1,
                            "startColumnIndex": 4,
                            "endColumnIndex": 5,
                        },
                        "cell": {
                            "userEnteredFormat": {
                                "backgroundColor": {
                                    "red": 0.95,
                                    "green": 0.24,
                                    "blue": 0.22,
                                },
                                "textFormat": {
                                    "foregroundColor": {"red": 1, "green": 1, "blue": 1},
                                    "bold": True,
                                    "underline": False,
                                },
                            }
                        },
                        "fields": "userEnteredFormat(backgroundColor,textFormat)",
                    }
                }
            )

        sheets.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={"requests": requests},
        ).execute()

    def _sheet_id(self, spreadsheet_id, title):
        meta = self._spreadsheet_meta(spreadsheet_id)
        for item in meta.get("sheets", []):
            props = item["properties"]
            if props["title"] == title:
                return props["sheetId"]
        return None

    def _sheet_ids_by_title(self, spreadsheet_id):
        meta = self._spreadsheet_meta(spreadsheet_id)
        return {
            item["properties"]["title"]: item["properties"]["sheetId"]
            for item in meta.get("sheets", [])
        }

    def _ensure_headers(self, spreadsheet_id, title, headers):
        sheet_id = self._ensure_sheet(spreadsheet_id, title)
        values = self._read_values(spreadsheet_id, title)
        if values:
            return sheet_id
        self._write_values(spreadsheet_id, title, [headers])
        self._format_table(spreadsheet_id, sheet_id, 1, len(headers))
        return sheet_id

    def ensure_month_layout(self, selected_date):
        if not self.is_ready():
            return None

        spreadsheet_id = self._find_or_create_spreadsheet(selected_date)
        cache_key = month_file_name(selected_date)
        if cache_key in self._layout_ready:
            return spreadsheet_id

        self._ensure_headers(spreadsheet_id, "Итог месяца", SUMMARY_HEADERS)

        first_day, last_day = month_bounds(selected_date)
        current = first_day
        while current <= last_day:
            self._ensure_headers(spreadsheet_id, day_sheet_name(current), DAY_HEADERS)
            current += timedelta(days=1)

        self._layout_ready.add(cache_key)
        return spreadsheet_id

    def _normalize_day_rows(self, selected_date, rows):
        date_label = selected_date.strftime("%d.%m.%Y")
        normalized = []
        for row in rows:
            padded = (row + [""] * len(DAY_HEADERS))[:len(DAY_HEADERS)]
            if not any(str(cell).strip() for cell in padded):
                continue
            if not padded[2]:
                padded[2] = date_label
            normalized.append(padded)
        normalized.sort(key=lambda row: (
            str(row[0]).lower(),
            str(row[1]).lower(),
            str(row[3]),
            str(row[4]),
        ))
        return normalized

    def _values_to_day_rows(self, values):
        if len(values) <= 1:
            return []

        rows = []
        for row in values[1:]:
            padded = (row + [""] * len(DAY_HEADERS))[:len(DAY_HEADERS)]
            if any(str(cell).strip() for cell in padded):
                rows.append(padded)
        return rows

    def _read_day_rows_in_spreadsheet(self, spreadsheet_id, titles, selected_date):
        title = day_sheet_name(selected_date)
        sheet_id = titles.get(title)
        if not sheet_id:
            return None, []

        values = self._read_values(spreadsheet_id, title)
        return sheet_id, self._values_to_day_rows(values)

    def _read_day_rows(self, selected_date, create=False):
        spreadsheet_id = (
            self.ensure_month_layout(selected_date)
            if create else self._find_spreadsheet(selected_date)
        )
        if not spreadsheet_id:
            return None, None, []

        title = day_sheet_name(selected_date)
        if create:
            sheet_id = self._ensure_sheet(spreadsheet_id, title)
        else:
            sheet_id = self._sheet_id(spreadsheet_id, title)
            if not sheet_id:
                return spreadsheet_id, None, []

        values = self._read_values(spreadsheet_id, title)
        return spreadsheet_id, sheet_id, self._values_to_day_rows(values)

    def _write_day_rows(self, selected_date, rows):
        spreadsheet_id = self.ensure_month_layout(selected_date)
        title = day_sheet_name(selected_date)
        sheet_id = self._ensure_sheet(spreadsheet_id, title)

        rows = self._normalize_day_rows(selected_date, rows)
        values = [DAY_HEADERS] + rows
        open_rows = [
            idx + 1
            for idx, row in enumerate(rows)
            if not str(row[4]).strip()
        ]

        self._write_values(spreadsheet_id, title, values)
        self._format_table(spreadsheet_id, sheet_id, len(values), len(DAY_HEADERS), open_rows)
        return spreadsheet_id

    def _row_clock_in_dt(self, row):
        row_date = parse_date_label(row[2])
        clock_minutes = parse_clock_minutes(row[3])
        if not row_date or clock_minutes is None:
            return None
        return datetime(
            row_date.year,
            row_date.month,
            row_date.day,
            clock_minutes // 60,
            clock_minutes % 60,
            tzinfo=self.app_tz,
        )

    def _recent_dates(self, end_date, days=31):
        for offset in range(days):
            yield end_date - timedelta(days=offset)

    def _close_employee_open_rows(self, employee, close_dt, days_back=31, close_all=True):
        close_local = close_dt.astimezone(self.app_tz)
        close_label = close_local.strftime("%H:%M")
        candidates = []
        dates_by_month = defaultdict(list)
        rows_by_date = {}

        for selected_date in self._recent_dates(close_local.date(), days_back):
            dates_by_month[selected_date.replace(day=1)].append(selected_date)

        for month_date, selected_dates in sorted(dates_by_month.items(), reverse=True):
            spreadsheet_id = self._find_spreadsheet(month_date)
            if not spreadsheet_id:
                continue
            titles = self._sheet_ids_by_title(spreadsheet_id)

            for selected_date in selected_dates:
                sheet_id, rows = self._read_day_rows_in_spreadsheet(
                    spreadsheet_id, titles, selected_date
                )
                if not sheet_id or not rows:
                    continue

                for row in rows:
                    if normalize_name(row[1]) != normalize_name(employee):
                        continue
                    if str(row[4]).strip():
                        continue

                    clock_in_dt = self._row_clock_in_dt(row)
                    if not clock_in_dt or clock_in_dt > close_local:
                        continue

                    rows_by_date[selected_date] = rows
                    candidates.append((clock_in_dt, selected_date, row))

        candidates.sort(key=lambda item: item[0])
        targets = candidates if close_all else candidates[-1:]
        changed_dates = set()
        closed = []

        for clock_in_dt, selected_date, row in targets:
            duration = max(0, int((close_local - clock_in_dt).total_seconds() // 60))
            row[4] = close_label
            row[5] = fmt_duration_label(duration)
            changed_dates.add(selected_date)
            closed.append(
                {
                    "date": selected_date,
                    "clock_in": clock_in_dt,
                    "clock_out": close_local,
                    "duration_minutes": duration,
                }
            )

        for selected_date in sorted(changed_dates):
            self._write_day_rows(selected_date, rows_by_date[selected_date])

        closed.sort(key=lambda item: item["clock_in"])
        return closed

    def record_clock_in(self, clock_dt, full_name, department, location=""):
        if not self.is_ready():
            raise RuntimeError("Google Sheets is not configured")

        local_dt = clock_dt.astimezone(self.app_tz)
        selected_date = local_dt.date()
        full_name = full_name or "Без имени"
        department = department or "Без отдела"

        closed = self._close_employee_open_rows(full_name, local_dt)
        _, _, rows = self._read_day_rows(selected_date, create=True)
        rows.append([
            department,
            full_name,
            selected_date.strftime("%d.%m.%Y"),
            local_dt.strftime("%H:%M"),
            "",
            "",
        ])
        self._write_day_rows(selected_date, rows)

        changed_months = {selected_date.replace(day=1)}
        changed_months.update(item["date"].replace(day=1) for item in closed)
        for month_date in sorted(changed_months):
            self.sync_month_summary_from_sheets(month_date)
        return None

    def record_clock_out(self, clock_dt, full_name, department="", location=""):
        if not self.is_ready():
            raise RuntimeError("Google Sheets is not configured")

        full_name = full_name or "Без имени"
        closed = self._close_employee_open_rows(full_name, clock_dt, close_all=False)
        changed_months = {item["date"].replace(day=1) for item in closed}
        for month_date in sorted(changed_months):
            self.sync_month_summary_from_sheets(month_date)

        if not closed:
            self.logger.warning("No open Google Sheets shift found for %s", full_name)
            return None
        return closed[-1]["duration_minutes"]

    def auto_close_stale_sessions(self, cutoff_date, auto_minutes):
        if not self.is_ready():
            return 0

        first_current, _ = month_bounds(cutoff_date)
        previous_month_day = first_current - timedelta(days=1)
        first_previous, _ = month_bounds(previous_month_day)
        month_starts = sorted({first_previous, first_current})

        closed_count = 0
        changed_dates = set()

        for month_start in month_starts:
            spreadsheet_id = self._find_spreadsheet(month_start)
            if not spreadsheet_id:
                continue
            titles = self._sheet_ids_by_title(spreadsheet_id)

            first_day, last_day = month_bounds(month_start)
            current = first_day
            while current <= min(last_day, cutoff_date - timedelta(days=1)):
                sheet_id, rows = self._read_day_rows_in_spreadsheet(
                    spreadsheet_id, titles, current
                )
                if not sheet_id or not rows:
                    current += timedelta(days=1)
                    continue

                changed = False
                for row in rows:
                    if str(row[4]).strip():
                        continue
                    clock_in_dt = self._row_clock_in_dt(row)
                    if not clock_in_dt:
                        continue

                    clock_out_dt = clock_in_dt + timedelta(minutes=auto_minutes)
                    row[4] = clock_out_dt.astimezone(self.app_tz).strftime("%H:%M")
                    row[5] = fmt_duration_label(auto_minutes)
                    closed_count += 1
                    changed = True

                if changed:
                    self._write_day_rows(current, rows)
                    changed_dates.add(current)
                current += timedelta(days=1)

        for month_date in sorted({d.replace(day=1) for d in changed_dates}):
            self.sync_month_summary_from_sheets(month_date)

        if closed_count:
            self.logger.info("Auto-closed %s stale Google Sheets shift(s)", closed_count)
        return closed_count

    def sync_month_summary_from_sheets(self, selected_date):
        spreadsheet_id = self.ensure_month_layout(selected_date)
        if not spreadsheet_id:
            return None

        meta = self._spreadsheet_meta(spreadsheet_id)
        titles = {
            item["properties"]["title"]
            for item in meta.get("sheets", [])
        }

        first_day, last_day = month_bounds(selected_date)
        rows = []
        current = first_day
        while current <= last_day:
            title = day_sheet_name(current)
            if title not in titles:
                current += timedelta(days=1)
                continue

            values = self._read_values(spreadsheet_id, title)
            for row in values[1:]:
                padded = (row + [""] * len(DAY_HEADERS))[:len(DAY_HEADERS)]
                if not any(str(cell).strip() for cell in padded):
                    continue
                rows.append(
                    {
                        "duration_minutes": parse_duration_label(padded[5]),
                        "employees": {
                            "full_name": padded[1],
                            "department": padded[0],
                            "position": padded[0],
                            "location": "",
                        },
                    }
                )
            current += timedelta(days=1)
        return self.sync_month_summary(selected_date, rows)

    def _session_to_day_row(self, selected_date, item):
        emp = item.get("employees") or {}
        department = emp.get("department") or emp.get("position") or "Без отдела"
        employee = emp.get("full_name") or "Без имени"

        clock_in_dt = parse_timestamp(item.get("clock_in_time"), self.app_tz)
        clock_out_dt = parse_timestamp(item.get("clock_out_time"), self.app_tz)

        clock_in = clock_in_dt.astimezone(self.app_tz).strftime("%H:%M") if clock_in_dt else ""
        clock_out = clock_out_dt.astimezone(self.app_tz).strftime("%H:%M") if clock_out_dt else ""

        duration = item.get("duration_minutes")
        duration_label = fmt_duration_label(duration)

        return [
            department,
            employee,
            selected_date.strftime("%d.%m.%Y"),
            clock_in,
            clock_out,
            duration_label,
        ]

    def sync_day(self, selected_date, sessions):
        if not self.is_ready():
            return None

        spreadsheet_id = self._find_or_create_spreadsheet(selected_date)
        title = day_sheet_name(selected_date)
        sheet_id = self._ensure_sheet(spreadsheet_id, title)

        rows = [self._session_to_day_row(selected_date, item) for item in sessions]
        rows.sort(key=lambda row: (row[0].lower(), row[1].lower(), row[3]))
        values = [DAY_HEADERS] + rows
        open_rows = [
            idx + 1
            for idx, row in enumerate(rows)
            if not row[4]
        ]

        self._write_values(spreadsheet_id, title, values)
        self._format_table(spreadsheet_id, sheet_id, len(values), len(DAY_HEADERS), open_rows)
        return spreadsheet_id

    def sync_month_summary(self, selected_date, sessions):
        if not self.is_ready():
            return None

        spreadsheet_id = self._find_or_create_spreadsheet(selected_date)
        title = "Итог месяца"
        sheet_id = self._ensure_sheet(spreadsheet_id, title)

        totals = defaultdict(lambda: {"shifts": 0, "minutes": 0})
        for item in sessions:
            emp = item.get("employees") or {}
            department = emp.get("department") or emp.get("position") or "Без отдела"
            employee = emp.get("full_name") or "Без имени"
            duration = item.get("duration_minutes")
            if duration is None:
                continue
            key = (department, employee)
            totals[key]["shifts"] += 1
            totals[key]["minutes"] += int(duration)

        rows = []
        for (department, employee), data in totals.items():
            rows.append([
                department,
                employee,
                data["shifts"],
                fmt_duration_label(data["minutes"]),
            ])
        rows.sort(key=lambda row: (row[0].lower(), row[1].lower()))

        values = [SUMMARY_HEADERS] + rows
        self._write_values(spreadsheet_id, title, values)
        self._format_table(spreadsheet_id, sheet_id, len(values), len(SUMMARY_HEADERS))
        return spreadsheet_id

    def _read_values(self, spreadsheet_id, title):
        _, sheets = self._services()
        a1_title = worksheet_title_for_a1(title)
        result = sheets.spreadsheets().values().get(
            spreadsheetId=spreadsheet_id,
            range=f"{a1_title}!A:F",
        ).execute()
        return result.get("values", [])

    def day_rows_for_bot(self, selected_date):
        if not self.is_ready():
            return []
        spreadsheet_id = self._find_spreadsheet(selected_date)
        if not spreadsheet_id:
            return []
        meta = self._spreadsheet_meta(spreadsheet_id)
        titles = {
            item["properties"]["title"]
            for item in meta.get("sheets", [])
        }
        if day_sheet_name(selected_date) not in titles:
            return []
        values = self._read_values(spreadsheet_id, day_sheet_name(selected_date))
        if len(values) <= 1:
            return []

        rows = []
        for row in values[1:]:
            padded = row + [""] * (len(DAY_HEADERS) - len(row))
            duration_minutes = parse_duration_label(padded[5])
            clock_out = padded[4]

            rows.append(
                {
                    "clock_in_time": padded[3],
                    "clock_out_time": clock_out,
                    "duration_minutes": duration_minutes,
                    "date": padded[2],
                    "date_label": padded[2],
                    "source": "google_sheets",
                    "employees": {
                        "full_name": padded[1],
                        "department": padded[0],
                        "position": padded[0],
                        "location": "",
                    },
                    "status": "Закрыта" if clock_out else "Открыта",
                }
            )
        return rows

    def range_rows_for_bot(self, date_from, date_to):
        rows = []
        current = date_from
        while current <= date_to:
            rows.extend(self.day_rows_for_bot(current))
            current += timedelta(days=1)
        return rows
