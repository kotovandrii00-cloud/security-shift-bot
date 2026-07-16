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
    "Обед",
    "Отработано",
    "План",
    "Переработка",
]

LEGACY_DAY_HEADERS = [
    "Отдел",
    "Сотрудник",
    "Дата",
    "Приход",
    "Уход",
    "Отработано",
    "План",
    "Переработка",
]

SUMMARY_HEADERS = [
    "Отдел",
    "Сотрудник",
    "Смен",
    "Факт за месяц",
    "План",
    "Переработка",
]

WEEKLY_HEADERS = [
    "Отдел",
    "Сотрудник",
    "Смен",
    "Факт за неделю",
    "План",
    "Переработка",
]

COL_DEPARTMENT = 0
COL_EMPLOYEE = 1
COL_DATE = 2
COL_CLOCK_IN = 3
COL_CLOCK_OUT = 4
COL_LUNCH = 5
COL_WORKED = 6
COL_PLAN = 7
COL_OVERTIME = 8

PLAN_MINUTES = 8 * 60

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


def week_sheet_name(start_date, end_date):
    return f"Неделя {start_date.strftime('%d.%m')}-{end_date.strftime('%d.%m')}"


def month_bounds(selected_date):
    first = selected_date.replace(day=1)
    if selected_date.month == 12:
        next_month = selected_date.replace(year=selected_date.year + 1, month=1, day=1)
    else:
        next_month = selected_date.replace(month=selected_date.month + 1, day=1)
    return first, next_month - timedelta(days=1)


def month_week_ranges(selected_date):
    first_day, last_day = month_bounds(selected_date)
    current = first_day
    ranges = []
    while current <= last_day:
        week_end = min(last_day, current + timedelta(days=6 - current.weekday()))
        ranges.append((current, week_end))
        current = week_end + timedelta(days=1)
    return ranges


def worksheet_title_for_a1(title):
    return "'" + title.replace("'", "''") + "'"


def fmt_duration_label(minutes):
    if minutes is None:
        return ""
    minutes = int(minutes)
    hours = minutes // 60
    mins = minutes % 60
    return f"{hours}ч {mins:02d}м"


def overtime_minutes(duration_minutes):
    if duration_minutes is None:
        return None
    return max(0, int(duration_minutes) - PLAN_MINUTES)


def fmt_overtime_label(minutes):
    if minutes is None or int(minutes) <= 0:
        return ""
    return fmt_duration_label(minutes)


def parse_duration_label(value):
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None

    normalized = text.replace(",", ".").lower()
    if normalized.endswith("м") and "ч" not in normalized:
        try:
            return int(float(normalized.replace("м", "").strip()))
        except ValueError:
            return None

    if "ч" in normalized:
        hours_part, _, rest = normalized.partition("ч")
        mins_part = rest.replace("м", "").strip() or "0"
        try:
            return int(float(hours_part.strip()) * 60) + int(float(mins_part))
        except ValueError:
            return None

    if ":" in normalized:
        parts = normalized.split(":")
        try:
            return int(parts[0].strip()) * 60 + int(parts[1].strip())
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
    parts = text.split(":")
    if len(parts) < 2:
        return None
    try:
        return int(parts[0]) * 60 + int(parts[1])
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
        self._spreadsheet_id_cache = {}
        self._meta_cache = {}

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
        if title in self._spreadsheet_id_cache:
            return self._spreadsheet_id_cache[title]

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
            spreadsheet_id = files[0]["id"]
            self._spreadsheet_id_cache[title] = spreadsheet_id
            return spreadsheet_id
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
        self._spreadsheet_id_cache[title] = spreadsheet_id
        self.logger.info("Created Google spreadsheet %s for %s", spreadsheet_id, title)
        return spreadsheet_id

    def _spreadsheet_meta(self, spreadsheet_id, refresh=False):
        if not refresh and spreadsheet_id in self._meta_cache:
            return self._meta_cache[spreadsheet_id]

        _, sheets = self._services()
        meta = sheets.spreadsheets().get(
            spreadsheetId=spreadsheet_id,
            fields="sheets(properties(sheetId,title,index,gridProperties))",
        ).execute()
        self._meta_cache[spreadsheet_id] = meta
        return meta

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
            self._meta_cache.pop(spreadsheet_id, None)
            return sheet_id

        response = sheets.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={"requests": [{"addSheet": {"properties": {"title": title}}}]},
        ).execute()
        self._meta_cache.pop(spreadsheet_id, None)
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

    def _format_table(
        self,
        spreadsheet_id,
        sheet_id,
        row_count,
        col_count,
        open_row_indexes=None,
        tab_color=None,
    ):
        _, sheets = self._services()
        open_row_indexes = open_row_indexes or []
        reset_rows = max(row_count + 20, 200)
        requests = [
            {
                "updateSheetProperties": {
                    "properties": {
                        "sheetId": sheet_id,
                        "gridProperties": {
                            "frozenRowCount": 1,
                            "hideGridlines": False,
                        },
                    },
                    "fields": "gridProperties.frozenRowCount,gridProperties.hideGridlines",
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
                            "backgroundColor": {
                                "red": 0.90,
                                "green": 0.97,
                                "blue": 0.92,
                            },
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

        if tab_color is not None:
            requests.append(
                {
                    "updateSheetProperties": {
                        "properties": {
                            "sheetId": sheet_id,
                            "tabColor": tab_color,
                            "tabColorStyle": {"rgbColor": tab_color},
                        },
                        "fields": "tabColor,tabColorStyle",
                    }
                }
            )

        for row_index in open_row_indexes:
            requests.append(
                {
                    "repeatCell": {
                        "range": {
                            "sheetId": sheet_id,
                            "startRowIndex": row_index,
                            "endRowIndex": row_index + 1,
                            "startColumnIndex": COL_CLOCK_OUT,
                            "endColumnIndex": COL_CLOCK_OUT + 1,
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

        try:
            sheets.spreadsheets().batchUpdate(
                spreadsheetId=spreadsheet_id,
                body={
                    "requests": [
                        {"clearBasicFilter": {"sheetId": sheet_id}},
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
                    ]
                },
            ).execute()
        except Exception as exc:
            self.logger.warning(
                "Could not reset Google Sheets basic filter for sheet %s: %s",
                sheet_id,
                exc,
            )

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

    def _read_values_batch(self, spreadsheet_id, titles):
        if not titles:
            return {}

        _, sheets = self._services()
        ranges = [f"{worksheet_title_for_a1(title)}!A:Z" for title in titles]
        result = sheets.spreadsheets().values().batchGet(
            spreadsheetId=spreadsheet_id,
            ranges=ranges,
        ).execute()

        values_by_title = {}
        for title, value_range in zip(titles, result.get("valueRanges", [])):
            values_by_title[title] = value_range.get("values", [])
        return values_by_title

    def _read_header_rows_batch(self, spreadsheet_id, titles):
        if not titles:
            return {}

        _, sheets = self._services()
        ranges = [f"{worksheet_title_for_a1(title)}!A1:Z1" for title in titles]
        result = sheets.spreadsheets().values().batchGet(
            spreadsheetId=spreadsheet_id,
            ranges=ranges,
        ).execute()

        headers_by_title = {}
        for title, value_range in zip(titles, result.get("valueRanges", [])):
            values = value_range.get("values", [])
            headers_by_title[title] = values[0] if values else []
        return headers_by_title

    def _ensure_headers(self, spreadsheet_id, title, headers):
        sheet_id = self._ensure_sheet(spreadsheet_id, title)
        values = self._read_values(spreadsheet_id, title)
        if values:
            return sheet_id
        self._write_values(spreadsheet_id, title, [headers])
        self._format_table(spreadsheet_id, sheet_id, 1, len(headers))
        return sheet_id

    def _tab_color_for_date(self, selected_date):
        if selected_date and selected_date.weekday() >= 5:
            return {"red": 0.25, "green": 0.25, "blue": 0.25}
        return None

    def ensure_month_layout(self, selected_date):
        if not self.is_ready():
            return None

        spreadsheet_id = self._find_or_create_spreadsheet(selected_date)
        cache_key = month_file_name(selected_date)
        if cache_key in self._layout_ready:
            return spreadsheet_id

        _, sheets = self._services()
        existing = self._sheet_ids_by_title(spreadsheet_id)
        first_day, last_day = month_bounds(selected_date)
        desired = [("Итог месяца", SUMMARY_HEADERS, None)]
        for week_start, week_end in month_week_ranges(selected_date):
            desired.append((week_sheet_name(week_start, week_end), WEEKLY_HEADERS, None))
        current = first_day
        while current <= last_day:
            desired.append((day_sheet_name(current), DAY_HEADERS, current))
            current += timedelta(days=1)

        requests = []
        header_updates = []
        structure_updates = []
        summary_title, summary_headers, _ = desired[0]

        if summary_title not in existing:
            if "Sheet1" in existing and len(existing) == 1:
                sheet_id = existing["Sheet1"]
                requests.append(
                    {
                        "updateSheetProperties": {
                            "properties": {"sheetId": sheet_id, "title": summary_title},
                            "fields": "title",
                        }
                    }
                )
                existing[summary_title] = sheet_id
                existing.pop("Sheet1", None)
            else:
                requests.append({"addSheet": {"properties": {"title": summary_title}}})
            header_updates.append((summary_title, summary_headers, None, True))

        for title, headers, tab_date in desired[1:]:
            if title in existing:
                continue
            requests.append({"addSheet": {"properties": {"title": title}}})
            header_updates.append((title, headers, tab_date, True))

        if requests:
            sheets.spreadsheets().batchUpdate(
                spreadsheetId=spreadsheet_id,
                body={"requests": requests},
            ).execute()
            self._meta_cache.pop(spreadsheet_id, None)
            existing = self._sheet_ids_by_title(spreadsheet_id)

        pending_header_titles = {title for title, _, _, _ in header_updates}
        existing_header_titles = [
            title
            for title, _, _ in desired
            if title in existing and title not in pending_header_titles
        ]
        headers_by_title = self._read_header_rows_batch(spreadsheet_id, existing_header_titles)
        for title, headers, tab_date in desired:
            if title not in headers_by_title:
                continue
            current_headers = (headers_by_title[title] + [""] * len(headers))[:len(headers)]
            if current_headers != headers:
                if (
                    tab_date is not None
                    and headers == DAY_HEADERS
                    and "Обед" not in headers_by_title[title]
                ):
                    sheet_id = existing.get(title)
                    if sheet_id is not None:
                        structure_updates.append(
                            {
                                "insertDimension": {
                                    "range": {
                                        "sheetId": sheet_id,
                                        "dimension": "COLUMNS",
                                        "startIndex": COL_LUNCH,
                                        "endIndex": COL_LUNCH + 1,
                                    },
                                    "inheritFromBefore": False,
                                }
                            }
                        )
                header_updates.append((title, headers, tab_date, False))

        if structure_updates:
            sheets.spreadsheets().batchUpdate(
                spreadsheetId=spreadsheet_id,
                body={"requests": structure_updates},
            ).execute()

        if header_updates:
            sheets.spreadsheets().values().batchUpdate(
                spreadsheetId=spreadsheet_id,
                body={
                    "valueInputOption": "USER_ENTERED",
                    "data": [
                        {
                            "range": f"{worksheet_title_for_a1(title)}!A1",
                            "values": [headers],
                        }
                        for title, headers, _, _ in header_updates
                    ],
                },
            ).execute()
            for title, headers, tab_date, needs_format in header_updates:
                if not needs_format:
                    continue
                sheet_id = existing.get(title)
                if sheet_id is not None:
                    self._format_table(
                        spreadsheet_id,
                        sheet_id,
                        1,
                        len(headers),
                        tab_color=self._tab_color_for_date(tab_date),
                    )

        sheet_property_requests = []
        for title, _, tab_date in desired:
            tab_color = self._tab_color_for_date(tab_date)
            sheet_id = existing.get(title)
            if sheet_id is None:
                continue
            properties = {
                "sheetId": sheet_id,
                "gridProperties": {"hideGridlines": False},
            }
            fields = ["gridProperties.hideGridlines"]
            if tab_color is not None:
                properties["tabColor"] = tab_color
                properties["tabColorStyle"] = {"rgbColor": tab_color}
                fields.extend(["tabColor", "tabColorStyle"])

            sheet_property_requests.append(
                {
                    "updateSheetProperties": {
                        "properties": properties,
                        "fields": ",".join(fields),
                    }
                }
            )

        if sheet_property_requests:
            sheets.spreadsheets().batchUpdate(
                spreadsheetId=spreadsheet_id,
                body={"requests": sheet_property_requests},
            ).execute()

        self._layout_ready.add(cache_key)
        return spreadsheet_id

    def _duration_from_day_row(self, row):
        clock_in = parse_clock_minutes(row[COL_CLOCK_IN])
        clock_out = parse_clock_minutes(row[COL_CLOCK_OUT])
        if clock_in is not None and clock_out is not None:
            duration = clock_out - clock_in
            if duration < 0:
                duration += 24 * 60
            lunch = parse_duration_label(row[COL_LUNCH]) or 0
            return max(0, duration - lunch)

        duration = parse_duration_label(row[COL_WORKED])
        if duration is not None:
            return duration
        return None

    def _normalize_day_row_metrics(self, row):
        if (
            str(row[COL_CLOCK_IN]).strip()
            or str(row[COL_CLOCK_OUT]).strip()
            or str(row[COL_WORKED]).strip()
        ):
            row[COL_PLAN] = fmt_duration_label(PLAN_MINUTES)

        duration = self._duration_from_day_row(row)
        if duration is None or not str(row[COL_CLOCK_OUT]).strip():
            row[COL_WORKED] = ""
            row[COL_OVERTIME] = ""
            return row

        row[COL_WORKED] = fmt_duration_label(duration)
        row[COL_OVERTIME] = fmt_overtime_label(overtime_minutes(duration))
        return row

    def _normalize_day_rows(self, selected_date, rows):
        date_label = selected_date.strftime("%d.%m.%Y")
        normalized = []
        for row in rows:
            padded = (row + [""] * len(DAY_HEADERS))[:len(DAY_HEADERS)]
            if not any(str(cell).strip() for cell in padded):
                continue
            if not padded[COL_DATE]:
                padded[COL_DATE] = date_label
            padded = self._normalize_day_row_metrics(padded)
            normalized.append(padded)
        normalized.sort(key=lambda row: (
            str(row[COL_DEPARTMENT]).lower(),
            str(row[COL_EMPLOYEE]).lower(),
            str(row[COL_CLOCK_IN]),
            str(row[COL_CLOCK_OUT]),
        ))
        return normalized

    def _values_to_day_rows(self, values, force_legacy=False):
        if len(values) <= 1:
            return []

        rows = []
        headers = values[0] if values else []
        legacy_layout = force_legacy or "Обед" not in headers
        for row in values[1:]:
            if legacy_layout:
                legacy = (row + [""] * len(LEGACY_DAY_HEADERS))[:len(LEGACY_DAY_HEADERS)]
                padded = [
                    legacy[0],
                    legacy[1],
                    legacy[2],
                    legacy[3],
                    legacy[4],
                    "",
                    legacy[5],
                    legacy[6],
                    legacy[7],
                ]
            else:
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

    def _write_day_rows_to_sheet(self, spreadsheet_id, title, sheet_id, selected_date, rows):
        rows = self._normalize_day_rows(selected_date, rows)
        values = [DAY_HEADERS] + rows
        open_rows = [
            idx + 1
            for idx, row in enumerate(rows)
            if not str(row[COL_CLOCK_OUT]).strip()
        ]

        self._write_values(spreadsheet_id, title, values)
        self._format_table(
            spreadsheet_id,
            sheet_id,
            len(values),
            len(DAY_HEADERS),
            open_rows,
            tab_color=self._tab_color_for_date(selected_date),
        )
        return spreadsheet_id

    def _write_day_rows(self, selected_date, rows):
        spreadsheet_id = self.ensure_month_layout(selected_date)
        title = day_sheet_name(selected_date)
        sheet_id = self._ensure_sheet(spreadsheet_id, title)
        return self._write_day_rows_to_sheet(
            spreadsheet_id,
            title,
            sheet_id,
            selected_date,
            rows,
        )

    def _row_clock_in_dt(self, row):
        row_date = parse_date_label(row[COL_DATE])
        clock_minutes = parse_clock_minutes(row[COL_CLOCK_IN])
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
            selected_titles = [
                day_sheet_name(selected_date)
                for selected_date in selected_dates
                if day_sheet_name(selected_date) in titles
            ]
            values_by_title = self._read_values_batch(spreadsheet_id, selected_titles)

            for selected_date in selected_dates:
                title = day_sheet_name(selected_date)
                sheet_id = titles.get(title)
                rows = self._values_to_day_rows(values_by_title.get(title, []))
                if not sheet_id or not rows:
                    continue

                for row in rows:
                    if normalize_name(row[COL_EMPLOYEE]) != normalize_name(employee):
                        continue
                    if str(row[COL_CLOCK_OUT]).strip():
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
            row[COL_CLOCK_OUT] = close_label
            row[COL_PLAN] = fmt_duration_label(PLAN_MINUTES)
            self._normalize_day_row_metrics(row)
            duration = self._duration_from_day_row(row)
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
            "",
            fmt_duration_label(PLAN_MINUTES),
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
            dates = []
            current = first_day
            while current <= min(last_day, cutoff_date - timedelta(days=1)):
                dates.append(current)
                current += timedelta(days=1)

            selected_titles = [
                day_sheet_name(selected_date)
                for selected_date in dates
                if day_sheet_name(selected_date) in titles
            ]
            values_by_title = self._read_values_batch(spreadsheet_id, selected_titles)

            for current in dates:
                title = day_sheet_name(current)
                sheet_id = titles.get(title)
                rows = self._values_to_day_rows(values_by_title.get(title, []))
                if not sheet_id or not rows:
                    continue

                changed = False
                for row in rows:
                    if str(row[COL_CLOCK_OUT]).strip():
                        continue
                    clock_in_dt = self._row_clock_in_dt(row)
                    if not clock_in_dt:
                        continue

                    lunch = parse_duration_label(row[COL_LUNCH]) or 0
                    clock_out_dt = clock_in_dt + timedelta(minutes=auto_minutes + lunch)
                    row[COL_CLOCK_OUT] = clock_out_dt.astimezone(self.app_tz).strftime("%H:%M")
                    row[COL_PLAN] = fmt_duration_label(PLAN_MINUTES)
                    self._normalize_day_row_metrics(row)
                    closed_count += 1
                    changed = True

                if changed:
                    self._write_day_rows(current, rows)
                    changed_dates.add(current)

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
        rows_by_date = defaultdict(list)
        day_titles = []
        date_by_title = {}
        current = first_day
        while current <= last_day:
            title = day_sheet_name(current)
            if title in titles:
                day_titles.append(title)
                date_by_title[title] = current
            current += timedelta(days=1)

        values_by_title = self._read_values_batch(spreadsheet_id, day_titles)

        for title in day_titles:
            selected_day = date_by_title[title]
            values = values_by_title.get(title, [])
            day_rows = self._values_to_day_rows(values)
            normalized_rows = self._normalize_day_rows(selected_day, day_rows)
            if normalized_rows != day_rows:
                self._write_day_rows_to_sheet(
                    spreadsheet_id,
                    title,
                    self._sheet_id(spreadsheet_id, title),
                    selected_day,
                    normalized_rows,
                )

            for padded in normalized_rows:
                if not any(str(cell).strip() for cell in padded):
                    continue
                if not str(padded[COL_CLOCK_OUT]).strip():
                    continue
                item = {
                    "duration_minutes": self._duration_from_day_row(padded),
                    "employees": {
                        "full_name": padded[COL_EMPLOYEE],
                        "department": padded[COL_DEPARTMENT],
                        "position": padded[COL_DEPARTMENT],
                        "location": "",
                    },
                }
                rows.append(item)
                rows_by_date[selected_day].append(item)

        spreadsheet_id = self.sync_month_summary(selected_date, rows)
        self.sync_week_summaries(selected_date, rows_by_date)
        return spreadsheet_id

    def _session_to_day_row(self, selected_date, item):
        emp = item.get("employees") or {}
        department = emp.get("department") or emp.get("position") or "Без отдела"
        employee = emp.get("full_name") or "Без имени"

        clock_in_dt = parse_timestamp(item.get("clock_in_time"), self.app_tz)
        clock_out_dt = parse_timestamp(item.get("clock_out_time"), self.app_tz)

        clock_in = clock_in_dt.astimezone(self.app_tz).strftime("%H:%M") if clock_in_dt else ""
        clock_out = clock_out_dt.astimezone(self.app_tz).strftime("%H:%M") if clock_out_dt else ""

        duration = item.get("duration_minutes")
        duration_label = fmt_duration_label(duration) if clock_out else ""
        plan_label = fmt_duration_label(PLAN_MINUTES)
        overtime_label = (
            fmt_overtime_label(overtime_minutes(duration))
            if duration is not None and clock_out
            else ""
        )

        return [
            department,
            employee,
            selected_date.strftime("%d.%m.%Y"),
            clock_in,
            clock_out,
            "",
            duration_label,
            plan_label,
            overtime_label,
        ]

    def sync_day(self, selected_date, sessions):
        if not self.is_ready():
            return None

        spreadsheet_id = self._find_or_create_spreadsheet(selected_date)
        title = day_sheet_name(selected_date)
        sheet_id = self._ensure_sheet(spreadsheet_id, title)

        rows = [self._session_to_day_row(selected_date, item) for item in sessions]
        self._write_day_rows_to_sheet(
            spreadsheet_id,
            title,
            sheet_id,
            selected_date,
            rows,
        )
        return spreadsheet_id

    def _summary_rows(self, sessions):
        totals = defaultdict(lambda: {"shifts": 0, "minutes": 0, "plan_minutes": 0})
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
            totals[key]["plan_minutes"] += PLAN_MINUTES

        rows = []
        for (department, employee), data in totals.items():
            period_overtime = max(0, data["minutes"] - data["plan_minutes"])
            rows.append([
                department,
                employee,
                data["shifts"],
                fmt_duration_label(data["minutes"]),
                fmt_duration_label(data["plan_minutes"]),
                fmt_overtime_label(period_overtime),
            ])
        rows.sort(key=lambda row: (row[0].lower(), row[1].lower()))
        return rows

    def _write_summary_table(self, spreadsheet_id, title, headers, sessions):
        sheet_id = self._ensure_sheet(spreadsheet_id, title)
        values = [headers] + self._summary_rows(sessions)
        self._write_values(spreadsheet_id, title, values)
        self._format_table(spreadsheet_id, sheet_id, len(values), len(headers))

    def sync_week_summaries(self, selected_date, rows_by_date):
        if not self.is_ready():
            return None

        spreadsheet_id = self.ensure_month_layout(selected_date)
        for week_start, week_end in month_week_ranges(selected_date):
            sessions = []
            current = week_start
            while current <= week_end:
                sessions.extend(rows_by_date.get(current, []))
                current += timedelta(days=1)

            self._write_summary_table(
                spreadsheet_id,
                week_sheet_name(week_start, week_end),
                WEEKLY_HEADERS,
                sessions,
            )
        return spreadsheet_id

    def sync_month_summary(self, selected_date, sessions):
        if not self.is_ready():
            return None

        spreadsheet_id = self._find_or_create_spreadsheet(selected_date)
        self._write_summary_table(spreadsheet_id, "Итог месяца", SUMMARY_HEADERS, sessions)
        return spreadsheet_id

    def _read_values(self, spreadsheet_id, title):
        _, sheets = self._services()
        a1_title = worksheet_title_for_a1(title)
        result = sheets.spreadsheets().values().get(
            spreadsheetId=spreadsheet_id,
            range=f"{a1_title}!A:Z",
        ).execute()
        return result.get("values", [])

    def _values_to_bot_rows(self, values):
        rows = []
        for padded in self._values_to_day_rows(values):
            clock_out = padded[COL_CLOCK_OUT]
            duration_minutes = self._duration_from_day_row(padded) if clock_out else None

            rows.append(
                {
                    "clock_in_time": padded[COL_CLOCK_IN],
                    "clock_out_time": clock_out,
                    "duration_minutes": duration_minutes,
                    "date": padded[COL_DATE],
                    "date_label": padded[COL_DATE],
                    "source": "google_sheets",
                    "employees": {
                        "full_name": padded[COL_EMPLOYEE],
                        "department": padded[COL_DEPARTMENT],
                        "position": padded[COL_DEPARTMENT],
                        "location": "",
                    },
                    "status": "Закрыта" if clock_out else "Открыта",
                }
            )
        return rows

    def day_rows_for_bot(self, selected_date):
        if not self.is_ready():
            return []
        spreadsheet_id = self._find_spreadsheet(selected_date)
        if not spreadsheet_id:
            return []
        titles = self._sheet_ids_by_title(spreadsheet_id)
        title = day_sheet_name(selected_date)
        if title not in titles:
            return []
        values = self._read_values(spreadsheet_id, title)
        return self._values_to_bot_rows(values)

    def range_rows_for_bot(self, date_from, date_to):
        rows = []
        dates_by_month = defaultdict(list)
        current = date_from
        while current <= date_to:
            dates_by_month[current.replace(day=1)].append(current)
            current += timedelta(days=1)

        for month_date, selected_dates in sorted(dates_by_month.items()):
            spreadsheet_id = self._find_spreadsheet(month_date)
            if not spreadsheet_id:
                continue
            titles = self._sheet_ids_by_title(spreadsheet_id)
            selected_titles = [
                day_sheet_name(selected_date)
                for selected_date in selected_dates
                if day_sheet_name(selected_date) in titles
            ]
            values_by_title = self._read_values_batch(spreadsheet_id, selected_titles)
            for title in selected_titles:
                rows.extend(self._values_to_bot_rows(values_by_title.get(title, [])))
        return rows
