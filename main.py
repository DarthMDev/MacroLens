"""MacroLens FastMCP server.

This app exposes two main tools that work with the user's Google Sheets:
- log_food: adds a row to "MacroLens Tracker"
- get_food_log: grabs recent rows and formats them as a Markdown table

Also has a Skybridge widget template at ui://widget.html.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import math
import os
from pathlib import Path
import re
from typing import Any

from dotenv import load_dotenv

# FastMCP gives us reliable access to the validated upstream access token
# (for Streamable HTTP, including after refresh).
try:  
    from fastmcp.server.dependencies import get_access_token 
except ImportError:  
    get_access_token = None

# Try using the "fastmcp" package name if it's installed.
# Otherwise fall back to the official MCP Python SDK import path.
try:  # pragma: no cover
    from fastmcp import Context, FastMCP 
except ImportError:  # pragma: no cover
    from mcp.server.fastmcp import Context, FastMCP


def _build_auth_provider():
    """Builds an auth provider for ChatGPT's connector OAuth.

    For Google Sheets access, the easiest way to get a real Google access token
    into tool invocations is running an OAuth proxy on this MCP server.

    You'll need to set these environment variables (I recommend using a `.env` file):
    - FASTMCP_SERVER_AUTH_GOOGLE_CLIENT_ID
    - FASTMCP_SERVER_AUTH_GOOGLE_CLIENT_SECRET
    - FASTMCP_SERVER_AUTH_GOOGLE_BASE_URL (your public https URL, like an ngrok link)

    If you don't configure these, the server just runs unauthenticated.
    """

    try:
        from fastmcp.server.auth.providers.google import GoogleProvider
    except ImportError:
        return None

    client_id = os.getenv("FASTMCP_SERVER_AUTH_GOOGLE_CLIENT_ID")
    client_secret = os.getenv("FASTMCP_SERVER_AUTH_GOOGLE_CLIENT_SECRET")
    base_url = os.getenv("FASTMCP_SERVER_AUTH_GOOGLE_BASE_URL")

    # If any of these are missing, we can't set up OAuth
    if not client_id or not client_secret or not base_url:
        return None

    return GoogleProvider(
        client_id=client_id,
        client_secret=client_secret,
        base_url=base_url,
        required_scopes=[
            "openid",
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive.file",
        ],
        # Note: When using an ngrok URL like https://xxxx.ngrok-free.dev with an MCP endpoint
        # mounted at /mcp, keeping issuer_url at the origin helps avoid discovery 404s.
        issuer_url=base_url,
    )


# Google Sheets stuff
import gspread
from google.oauth2.credentials import Credentials


load_dotenv()


SHEET_TITLE = "MacroLens Tracker"

# Scopes aren't strictly required for an already-scoped access token,
# but including them helps with google-auth compatibility.
GOOGLE_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.file",
]


@dataclass(frozen=True)
class FoodLogRow:
    timestamp: str
    item_name: str
    calories: float
    protein: float
    carbs: float
    fat: float


mcp = FastMCP(
    "MacroLens",
    stateless_http=True,
    json_response=True,
    auth=_build_auth_provider(),
)


def _require_oauth_token(ctx: Context) -> str:
    # Some MCP contexts expose the token directly on ctx.auth.token.
    token = getattr(getattr(ctx, "auth", None), "token", None)
    if isinstance(token, str) and token:
        return token

    # FastMCP Context doesn't expose auth directly, so use the dependency helper instead.
    if get_access_token is not None:
        access = get_access_token()
        if access is not None and isinstance(getattr(access, "token", None), str):
            return access.token 

    raise ValueError(
        "Missing Google OAuth access token. In ChatGPT, open the MacroLens connector and click Connect, then try again."
    )


def _get_gspread_client(ctx: Context) -> gspread.Client:
    access_token = _require_oauth_token(ctx)
    creds = Credentials(token=access_token, scopes=GOOGLE_SCOPES)
    return gspread.authorize(creds)


def _open_or_create_tracker(ctx: Context) -> tuple[gspread.Spreadsheet, gspread.Worksheet]:
    client = _get_gspread_client(ctx)
    
    # Try opening the sheet, create it if it doesn't exist
    try:
        spreadsheet = client.open(SHEET_TITLE)
    except gspread.SpreadsheetNotFound:
        spreadsheet = client.create(SHEET_TITLE)

    worksheet = spreadsheet.sheet1

    # Make sure the header row exists.
    header = [
        "timestamp",
        "item_name",
        "calories",
        "protein",
        "carbs",
        "fat",
    ]
    existing = worksheet.get_values("1:1")
    if not existing or not existing[0] or [c.strip() for c in existing[0]] != header:
        worksheet.update("A1:F1", [header])

    return spreadsheet, worksheet


def _spreadsheet_link(spreadsheet: gspread.Spreadsheet) -> str | None:
    url = getattr(spreadsheet, "url", None)
    if isinstance(url, str) and url:
        return url

    # Fallback: try constructing a standard Sheets URL from the sheet ID
    sheet_id = getattr(spreadsheet, "id", None) or getattr(spreadsheet, "key", None)
    if isinstance(sheet_id, str) and sheet_id:
        return f"https://docs.google.com/spreadsheets/d/{sheet_id}/edit"

    return None


def _open_or_create_tracker_sheet(ctx: Context) -> gspread.Worksheet:
    _, worksheet = _open_or_create_tracker(ctx)
    return worksheet


def _format_timestamp(dt: datetime) -> str:
    # Format we want: 2026-01-02 10:30 pm
    # Use %I then strip the possible leading zero.
    s = dt.strftime("%Y-%m-%d %I:%M %p")
    # Clean up the leading zero from the hour and lowercase am/pm
    date_part, time_part, ampm = s.split(" ")
    time_part = time_part.lstrip("0")
    return f"{date_part} {time_part} {ampm.lower()}"


def _extract_user_timezone(ctx: Context | None) -> timezone | Any:
    """Timezone extraction from ChatGPT tool invocation context.

    ChatGPT can give us an IANA timezone name (preferred) and/or an offset.
    We try multiple shapes since different runtimes expose metadata differently.
    """

    # zoneinfo is in stdlib on Python 3.9+, but let's keep a safe fallback just in case
    try:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError  # type: ignore
    except ImportError: 
        ZoneInfo = None
        ZoneInfoNotFoundError = KeyError

    def _as_dict(obj: Any) -> dict[str, Any] | None:
        return obj if isinstance(obj, dict) else None

    candidates: list[dict[str, Any]] = []
    if ctx is not None:
        # Known-ish attribute names across different MCP/FastMCP implementations
        for attr in ("meta", "metadata", "request", "raw_request", "session", "state"):
            v = getattr(ctx, attr, None)
            d = _as_dict(v)
            if d is not None:
                candidates.append(d)

        # Some contexts are themselves dict-like
        dctx = _as_dict(ctx)
        if dctx is not None:
            candidates.append(dctx)

    def _find_timezone_name(d: dict[str, Any]) -> str | None:
        # Possible locations where timezone might be hiding:
        # - d["timezone"]
        # - d["openai/userLocation"]["timezone"]
        # - d["openai"]["userLocation"]["timezone"]
        # - d["_meta"]["openai/userLocation"]["timezone"]
        # - d["_meta"]["openai"]["userLocation"]["timezone"]
        direct = d.get("timezone")
        if isinstance(direct, str) and direct:
            return direct

        def _tz_from(container: dict[str, Any]) -> str | None:
            ul = container.get("openai/userLocation")
            if isinstance(ul, dict):
                tzname = ul.get("timezone")
                if isinstance(tzname, str) and tzname:
                    return tzname
            openai = container.get("openai")
            if isinstance(openai, dict):
                ul2 = openai.get("userLocation")
                if isinstance(ul2, dict):
                    tzname = ul2.get("timezone")
                    if isinstance(tzname, str) and tzname:
                        return tzname
            return None

        tzname = _tz_from(d)
        if tzname:
            return tzname

        meta = d.get("_meta")
        if isinstance(meta, dict):
            tzname = _tz_from(meta)
            if tzname:
                return tzname

        return None

    def _find_offset_minutes(d: dict[str, Any]) -> int | None:
        for key in ("timezone_offset_minutes", "timezoneOffsetMinutes", "tz_offset_minutes"):
            v = d.get(key)
            if isinstance(v, (int, float)):
                return int(v)
        meta = d.get("_meta")
        if isinstance(meta, dict):
            v = meta.get("timezone_offset_minutes")
            if isinstance(v, (int, float)):
                return int(v)
        return None

    tz_name: str | None = None
    offset_minutes: int | None = None
    for d in candidates:
        tz_name = tz_name or _find_timezone_name(d)
        offset_minutes = offset_minutes if offset_minutes is not None else _find_offset_minutes(d)

    if tz_name and ZoneInfo is not None:
        try:
            return ZoneInfo(tz_name)
        except ZoneInfoNotFoundError:
            pass

    # Offset fallback: some clients report minutes *behind* UTC as positive
    if offset_minutes is not None and abs(offset_minutes) <= 14 * 60:
        # Example: New York might come in as +300 (minutes behind UTC)
        offset = -offset_minutes if offset_minutes > 0 else offset_minutes
        return timezone(timedelta(minutes=offset))

    # If we couldn't figure it out, just default to UTC
    return timezone.utc


def _normalize_user_datetime(user_input: str | None, ctx: Context | None) -> tuple[datetime, str]:
    """Parse a user-friendly date/time string.

    If time is left out, we just store the date as YYYY-MM-DD.
    If time is included, we store it as YYYY-MM-DD h:mm am/pm.

    If the user doesn't specify a timezone/offset, we interpret the datetime in
    their local timezone (from ChatGPT metadata) when we can figure it out.
    """

    user_tz = _extract_user_timezone(ctx)
    now_local = datetime.now(tz=user_tz).replace(second=0, microsecond=0)
    now_local_naive = now_local.replace(tzinfo=None)

    if user_input is None or not str(user_input).strip():
        dt = now_local_naive
        return dt, _format_timestamp(dt)

    raw = str(user_input).strip()
    lower = raw.lower()

    # Handle relative phrases. Allow exact matches and common natural-language wrappers
    if re.search(r"\btoday\b", lower):
        return now_local_naive, now_local_naive.strftime("%Y-%m-%d")
    if re.search(r"\bnow\b", lower):
        return now_local_naive, _format_timestamp(now_local_naive)

    day = now_local_naive.date()

    # Handle phrases like "at 8am today" / "today at 8:15 pm" / "at 8am"
    time_match = re.search(
        r"\b(?:at\s*)?(?P<h>\d{1,2})(?::(?P<m>\d{2}))?\s*(?P<ampm>am|pm)\b",
        lower,
    )
    if time_match:
        hour = int(time_match.group("h"))
        minute = int(time_match.group("m") or "0")
        ampm = time_match.group("ampm")

        if not (1 <= hour <= 12) or not (0 <= minute <= 59):
            raise ValueError("Invalid time. Try something like 8am or 8:30pm.")

        hour24 = hour % 12
        if ampm == "pm":
            hour24 += 12

        # If user doesn't mention a date, assume today in their timezone
        dt = datetime.combine(day, datetime.min.time()).replace(hour=hour24, minute=minute)
        return dt, _format_timestamp(dt)

    # Relative parts of the day
    if "morning" in lower:
        dt = datetime.combine(day, datetime.min.time()).replace(hour=9, minute=0)
        return dt, _format_timestamp(dt)
    if "afternoon" in lower:
        dt = datetime.combine(day, datetime.min.time()).replace(hour=13, minute=0)
        return dt, _format_timestamp(dt)
    if "evening" in lower:
        dt = datetime.combine(day, datetime.min.time()).replace(hour=19, minute=0)
        return dt, _format_timestamp(dt)
    if "tonight" in lower:
        dt = datetime.combine(day, datetime.min.time()).replace(hour=21, minute=0)
        return dt, _format_timestamp(dt)

    # Normalize am/pm token casing for strptime
    cleaned = raw.replace("a.m.", "am").replace("p.m.", "pm")
    cleaned = cleaned.replace("AM", "am").replace("PM", "pm")
    cleaned_upper_ampm = cleaned.replace(" am", " AM").replace(" pm", " PM")

    # Try ISO parsing first (supports offsets if present)
    try:
        # fromisoformat accepts "YYYY-MM-DD" and "YYYY-MM-DD HH:MM" as well
        iso_candidate = cleaned.replace(" ", "T") if "T" not in cleaned and ":" in cleaned and "-" in cleaned[:10] else cleaned
        parsed = datetime.fromisoformat(iso_candidate)
        if parsed.tzinfo is not None:
            # Convert to the user's timezone, then store native local time
            parsed = parsed.astimezone(user_tz).replace(tzinfo=None)
        else:
            # No offset provided: interpret as local time
            parsed = parsed.replace(tzinfo=None)

        # Check if time was provided
        if ":" in cleaned:
            dt = parsed.replace(tzinfo=None)
            return dt, _format_timestamp(dt)
        dt = parsed.replace(tzinfo=None)
        return dt, dt.strftime("%Y-%m-%d")
    except ValueError:
        pass

    # Supported user-friendly formats
    formats_with_time = [
        "%Y-%m-%d %H:%M",  # 2026-01-02 22:30
        "%Y-%m-%d %I:%M %p",  # 2026-01-02 10:30 PM
        "%m/%d/%Y %H:%M",  # 01/02/2026 22:30
        "%m/%d/%Y %I:%M %p",  # 01/02/2026 10:30 PM
    ]
    formats_date_only = [
        "%Y-%m-%d",  # 2026-01-02
        "%m/%d/%Y",  # 01/02/2026
    ]

    for fmt in formats_with_time:
        try:
            dt = datetime.strptime(cleaned_upper_ampm, fmt).replace(second=0, microsecond=0)
            return dt, _format_timestamp(dt)
        except ValueError:
            continue

    for fmt in formats_date_only:
        try:
            dt = datetime.strptime(cleaned_upper_ampm, fmt).replace(second=0, microsecond=0)
            return dt, dt.strftime("%Y-%m-%d")
        except ValueError:
            continue

    raise ValueError(
        "Couldn't recognize that date/time format. Please use one of these and try again:\n"
        "- YYYY-MM-DD (e.g. 2026-01-02)\n"
        "- YYYY-MM-DD HH:MM (24h, e.g. 2026-01-02 22:30)\n"
        "- YYYY-MM-DD h:MM am/pm (e.g. 2026-01-02 10:30 pm)\n"
        "- MM/DD/YYYY (e.g. 01/02/2026)\n"
        "- MM/DD/YYYY HH:MM (24h, e.g. 01/02/2026 22:30)\n"
        "- MM/DD/YYYY h:MM am/pm (e.g. 01/02/2026 10:30 pm)\n"
        "You can also just type: today, now"
    )


def _parse_timestamp(ts: str) -> datetime | None:
    ts = (ts or "").strip()
    if not ts:
        return None
    try:
        if ts.endswith("Z"):
            parsed = datetime.fromisoformat(ts.replace("Z", "+00:00")).replace(tzinfo=None)
        elif "T" in ts or ":" in ts:
            parsed = datetime.fromisoformat(ts.replace(" ", "T")).replace(tzinfo=None)
        else:
            # Date-only case
            parsed = datetime.strptime(ts, "%Y-%m-%d").replace(tzinfo=None)
    except ValueError:
        # Try human-friendly timestamp with am/pm
        try:
            return datetime.strptime(ts, "%Y-%m-%d %I:%M %p").replace(tzinfo=None)
        except ValueError:
            return None

    return parsed


def _is_recent_duplicate_log(
    worksheet: gspread.Worksheet,
    *,
    new_timestamp: str,
    new_item_name: str,
    new_calories: float,
    new_protein: float,
    new_carbs: float,
    new_fat: float,
    window_seconds: int = 180,
    lookback_rows: int = 10,
) -> bool:
    """Dedupe for common retry/double-invocation scenarios.

    ChatGPT / clients might retry tool calls, and Streamable HTTP can sometimes result
    in duplicate invocations on transient network issues. This helps avoid double-appending
    the same food within a short window without needing extra sheet columns.
    """

    try:
        values = worksheet.get_all_values()
    except (gspread.GSpreadException, OSError, ValueError):
        return False

    if not values or len(values) < 2:
        return False

    data_rows = values[1:]
    if not data_rows:
        return False

    new_dt = _parse_timestamp(new_timestamp)
    if new_dt is None:
        return False

    normalized_name = new_item_name.strip().lower()

    # Check the last few rows for duplicates
    for r in data_rows[-lookback_rows:]:
        if len(r) < 6:
            continue
        ts = r[0].strip()
        item = r[1].strip().lower()
        if item != normalized_name:
            continue

        existing_dt = _parse_timestamp(ts)
        if existing_dt is None:
            continue

        if abs((new_dt - existing_dt).total_seconds()) > window_seconds:
            continue

        try:
            calories = float(r[2])
            protein = float(r[3])
            carbs = float(r[4])
            fat = float(r[5])
        except (TypeError, ValueError):
            continue

        # Compare macro values with some tolerance
        if (
            math.isclose(calories, new_calories, rel_tol=0.0, abs_tol=0.05)
            and math.isclose(protein, new_protein, rel_tol=0.0, abs_tol=0.05)
            and math.isclose(carbs, new_carbs, rel_tol=0.0, abs_tol=0.05)
            and math.isclose(fat, new_fat, rel_tol=0.0, abs_tol=0.05)
        ):
            return True

    return False


def _to_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Expected a number, got {value!r}") from exc


def _markdown_table(rows: list[list[str]], headers: list[str]) -> str:
    def esc(cell: str) -> str:
        return (cell or "").replace("|", "\\|").replace("\n", " ")

    out: list[str] = []
    out.append("| " + " | ".join(esc(h) for h in headers) + " |")
    out.append("| " + " | ".join(["---"] * len(headers)) + " |")
    for row in rows:
        padded = (row + [""] * len(headers))[: len(headers)]
        out.append("| " + " | ".join(esc(c) for c in padded) + " |")
    return "\n".join(out)


@mcp.resource(
    "ui://widget.html",
    name="macrolens_widget",
    title="MacroLens",
    description="MacroLens inline widget",
    mime_type="text/html+skybridge",
)
def widget_html() -> str:
    """Skybridge HTML template for the ChatGPT widget."""
    widget_path = Path(__file__).parent / "assets" / "widget.html"
    return widget_path.read_text(encoding="utf-8")


@mcp.tool(
    title="Start meal log (attach photo)",
    description=(
        "Use this FIRST when the user wants to log a meal (e.g., "
        "'I want to log my lunch', 'log my breakfast', 'log dinner', 'track my meal') "
        "and they haven't provided a photo or nutrition label yet. "
        "It shows a widget with a 'Snap Food' button and asks them to attach a photo. "
        "After the user attaches a photo or gives text details, go ahead and analyze it, then call Log food."
    ),
    annotations={
        "readOnlyHint": True,
        "openWorldHint": True,
        "destructiveHint": False,
    },
    meta={
        "openai/outputTemplate": "ui://widget.html",
    },
)
def show_widget(ctx: Context, meal: str | None = None) -> str:
    """Render the MacroLens widget."""
    _ = ctx
    meal_part = f" for {meal.strip()}" if isinstance(meal, str) and meal.strip() else ""
    return (
        f"Attach a photo of the meal{meal_part} (or a nutrition label). "
        "You can also just type what you ate and give me approximate portions."
    )


@mcp.tool(
    title="Log food",
    description=(
        "Append a food item with macros to the user's MacroLens Google Sheet. "
        "Optional: include logged_at (date/time). Accepted examples: "
        "today, now, this morning, this afternoon, this evening, tonight, "
        "2026-01-02, 2026-01-02 22:30, 2026-01-02 10:30 pm, "
        "01/02/2026, 01/02/2026 22:30, 01/02/2026 10:30 pm."
    ),
    annotations={
        "readOnlyHint": False,
        "openWorldHint": True,
        "destructiveHint": False,
    },
    meta={
        "openai/toolInvocation/invoking": "Logging food…",
        "openai/toolInvocation/invoked": "Logged.",
    },
)
def log_food(
    item_name: str,
    calories: float,
    protein: float,
    carbs: float,
    fat: float,
    ctx: Context,
    logged_at: str | None = None,
) -> str:
    """Append a food log row to the user's "MacroLens Tracker" sheet.

    If `logged_at` is provided, it'll be parsed from common date/time formats.
    If not provided, we just use the current time in the user's timezone (when available).
    """
    spreadsheet, worksheet = _open_or_create_tracker(ctx)

    _, ts = _normalize_user_datetime(logged_at, ctx)

    normalized_item = item_name.strip()
    calories_f = _to_float(calories)
    protein_f = _to_float(protein)
    carbs_f = _to_float(carbs)
    fat_f = _to_float(fat)

    # Check for recent duplicates before logging
    if _is_recent_duplicate_log(
        worksheet,
        new_timestamp=ts,
        new_item_name=normalized_item,
        new_calories=calories_f,
        new_protein=protein_f,
        new_carbs=carbs_f,
        new_fat=fat_f,
    ):
        sheet_url = _spreadsheet_link(spreadsheet)
        link_line = f"\n\nSpreadsheet: {sheet_url}" if isinstance(sheet_url, str) and sheet_url else ""
        # Silent de-dupe: just return the standard success message
        return f"Logged: {normalized_item} ({calories_f} kcal, P {protein_f} / C {carbs_f} / F {fat_f}){link_line}"

    row = FoodLogRow(
        timestamp=ts,
        item_name=normalized_item,
        calories=calories_f,
        protein=protein_f,
        carbs=carbs_f,
        fat=fat_f,
    )

    worksheet.append_row(
        [
            row.timestamp,
            row.item_name,
            row.calories,
            row.protein,
            row.carbs,
            row.fat,
        ],
        value_input_option="USER_ENTERED",
    )

    sheet_url = _spreadsheet_link(spreadsheet)
    link_line = f"\n\nSpreadsheet: {sheet_url}" if isinstance(sheet_url, str) and sheet_url else ""
    return (
        f"Logged: {row.item_name} ({row.calories} kcal, P {row.protein} / C {row.carbs} / F {row.fat})"
        f"{link_line}"
    )


@mcp.tool(
    title="Get food log",
    description="Get the last N days of food logs as a Markdown table.",
    annotations={
        "readOnlyHint": True,
        "openWorldHint": True,
        "destructiveHint": False,
    },
    meta={
        "openai/toolInvocation/invoking": "Fetching your log…",
        "openai/toolInvocation/invoked": "Log ready.",
    },
)
def get_food_log(days: int = 1, ctx: Context | None = None) -> str:
    """Return the last N days of logs as a Markdown table."""
    if ctx is None:
        raise ValueError("Context is required")

    if days < 1:
        raise ValueError("days must be >= 1")

    worksheet = _open_or_create_tracker_sheet(ctx)
    values = worksheet.get_all_values()

    if not values or len(values) < 2:
        return "(No food logs yet.)"

    header = [c.strip() for c in values[0]]
    data_rows = values[1:]

    # Column order we're expecting; fall back gracefully if the sheet got edited
    col_map: dict[str, int] = {name: idx for idx, name in enumerate(header)}
    required_cols = ["timestamp", "item_name", "calories", "protein", "carbs", "fat"]
    if not all(c in col_map for c in required_cols):
        # Just return the raw sheet as a table
        return _markdown_table(data_rows[-50:], header)

    # Stored timestamps are native local times; use the user's local timezone baseline
    user_tz = _extract_user_timezone(ctx)
    now_local = datetime.now(tz=user_tz).replace(tzinfo=None)
    cutoff = now_local - timedelta(days=days)

    filtered: list[tuple[datetime, list[str]]] = []
    for r in data_rows:
        ts = (r[col_map["timestamp"]] if col_map["timestamp"] < len(r) else "").strip()
        if not ts:
            continue

        parsed = _parse_timestamp(ts)
        if parsed is None:
            continue

        if parsed >= cutoff:
            filtered.append(
                (
                    parsed,
                    [
                        ts,
                        r[col_map["item_name"]] if col_map["item_name"] < len(r) else "",
                        r[col_map["calories"]] if col_map["calories"] < len(r) else "",
                        r[col_map["protein"]] if col_map["protein"] < len(r) else "",
                        r[col_map["carbs"]] if col_map["carbs"] < len(r) else "",
                        r[col_map["fat"]] if col_map["fat"] < len(r) else "",
                    ],
                )
            )

    # Sort by timestamp, most recent first
    filtered.sort(key=lambda t: t[0], reverse=True)

    if not filtered:
        return f"(No logs in the last {days} day(s).)"

    table_rows = [row for _, row in filtered]
    return _markdown_table(table_rows, ["timestamp", "item_name", "calories", "protein", "carbs", "fat"])


if __name__ == "__main__":
    # Run with Streamable HTTP transport (recommended for Apps SDK)
    mcp.run(transport="streamable-http")