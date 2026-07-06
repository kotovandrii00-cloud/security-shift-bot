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
        self.enabled = env_bool("SHEETS_ENABLED", False)
        self.folder_id = os.getenv("GOOGLE_DRIVE_FOLDER_ID")
        self._drive = None
        self._sheets = None

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
        reset_rows = max(row_count, 2)
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
