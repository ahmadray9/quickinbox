import os
import re
import sqlite3
import imaplib
import email
import html
import json
import uuid
import hashlib
import hmac
from io import BytesIO
from email.header import decode_header
from datetime import datetime, timedelta, timezone, date, time
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st
from dotenv import load_dotenv
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak

load_dotenv()

APP_NAME = "QuickInbox"
DB_FILE = Path(os.getenv("DB_FILE", "platform.db"))

DOMAIN = os.getenv("MAIL_DOMAIN", "yourdomain.com").strip().lower()
IMAP_HOST = os.getenv("IMAP_HOST", "").strip()
IMAP_PORT = int(os.getenv("IMAP_PORT", "993"))
IMAP_USER = os.getenv("IMAP_USER", "").strip()
IMAP_PASSWORD = os.getenv("IMAP_PASSWORD", "")
INBOX_FOLDER = os.getenv("IMAP_FOLDER", "INBOX").strip() or "INBOX"

SUPER_ADMIN_USERNAME = os.getenv("SUPER_ADMIN_USERNAME", "").strip()
SUPER_ADMIN_PASSWORD = os.getenv("SUPER_ADMIN_PASSWORD", "")
APP_TIMEZONE_NAME = os.getenv("APP_TIMEZONE", "Asia/Beirut").strip() or "Asia/Beirut"
try:
    APP_TIMEZONE = ZoneInfo(APP_TIMEZONE_NAME)
except Exception:
    APP_TIMEZONE = timezone.utc
    APP_TIMEZONE_NAME = "UTC"

FACEBOOK_SIGNUP = "https://www.facebook.com/r.php"
INSTAGRAM_SIGNUP = "https://www.instagram.com/accounts/emailsignup/"

st.set_page_config(
    page_title=APP_NAME,
    page_icon="✉️",
    layout="wide",
    initial_sidebar_state="collapsed",
)


# -----------------------------------------------------------------------------
# Session state
# -----------------------------------------------------------------------------
if "active_email" not in st.session_state:
    st.session_state.active_email = ""
if "dark_mode" not in st.session_state:
    st.session_state.dark_mode = True
if "session_id" not in st.session_state:
    st.session_state.session_id = uuid.uuid4().hex
if "session_logged" not in st.session_state:
    st.session_state.session_logged = False
if "admin_authenticated" not in st.session_state:
    st.session_state.admin_authenticated = False
if "admin_failed_attempts" not in st.session_state:
    st.session_state.admin_failed_attempts = 0


# -----------------------------------------------------------------------------
# Database / analytics storage
# -----------------------------------------------------------------------------
def db_connection():
    DB_FILE.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB_FILE, check_same_thread=False)
    db.row_factory = sqlite3.Row

    # Active reservations. Expired rows are removed so an address can be reused.
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS inboxes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            email_address TEXT UNIQUE NOT NULL,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL
        )
        """
    )

    # Permanent reservation history for analytics. This table is never cleaned automatically.
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS inbox_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL,
            email_address TEXT NOT NULL,
            session_id TEXT,
            duration_hours INTEGER NOT NULL DEFAULT 24,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            UNIQUE(email_address, created_at)
        )
        """
    )

    # Message metadata only. Message bodies are deliberately not stored in analytics.
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS message_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fingerprint TEXT UNIQUE NOT NULL,
            email_address TEXT NOT NULL,
            sender TEXT,
            recipient TEXT,
            subject TEXT,
            message_date TEXT,
            observed_at TEXT NOT NULL
        )
        """
    )

    db.execute(
        """
        CREATE TABLE IF NOT EXISTS activity_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT,
            event_type TEXT NOT NULL,
            email_address TEXT,
            details_json TEXT,
            created_at TEXT NOT NULL
        )
        """
    )

    db.execute("CREATE INDEX IF NOT EXISTS idx_history_created ON inbox_history(created_at)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_history_email ON inbox_history(email_address)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_messages_observed ON message_history(observed_at)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_messages_email ON message_history(email_address)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_events_created ON activity_events(created_at)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_events_type ON activity_events(event_type)")

    # Backfill any currently active reservations that predate analytics history.
    db.execute(
        """
        INSERT OR IGNORE INTO inbox_history(
            username, email_address, session_id, duration_hours, created_at, expires_at
        )
        SELECT
            username,
            email_address,
            NULL,
            MAX(1, ROUND((julianday(expires_at) - julianday(created_at)) * 24)),
            created_at,
            expires_at
        FROM inboxes
        """
    )
    db.commit()
    return db


conn = db_connection()


def utc_now():
    return datetime.now(timezone.utc)


def local_now():
    return utc_now().astimezone(APP_TIMEZONE)


def iso_utc(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=APP_TIMEZONE)
    return value.astimezone(timezone.utc).isoformat()


def parse_utc(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except Exception:
        return None


def format_local(value, fmt="%Y-%m-%d %H:%M"):
    parsed = parse_utc(value)
    return parsed.astimezone(APP_TIMEZONE).strftime(fmt) if parsed else ""


def log_event(event_type: str, email_address: str | None = None, details=None, session_id: str | None = None):
    try:
        payload = json.dumps(details or {}, ensure_ascii=False, default=str)
        conn.execute(
            "INSERT INTO activity_events(session_id,event_type,email_address,details_json,created_at) VALUES(?,?,?,?,?)",
            (session_id or st.session_state.get("session_id", ""), event_type, email_address, payload, utc_now().isoformat()),
        )
        conn.commit()
    except Exception:
        # Analytics must never break the inbox experience.
        pass


def clean_expired_inboxes():
    conn.execute("DELETE FROM inboxes WHERE expires_at <= ?", (utc_now().isoformat(),))
    conn.commit()


if not st.session_state.session_logged:
    log_event("session_started", details={"app": APP_NAME})
    st.session_state.session_logged = True


def clean_username(value: str) -> str:
    value = (value or "").strip().lower()
    value = re.sub(r"[^a-z0-9._-]", "", value)
    value = value.strip("._-")
    return value[:40]


def reserve_inbox(username: str, hours: int = 24):
    clean_expired_inboxes()
    username = clean_username(username)

    if len(username) < 3:
        log_event("inbox_create_failed", details={"reason": "invalid_username"})
        return None, "Use at least 3 letters or numbers for the inbox name."

    address = f"{username}@{DOMAIN}"
    now = utc_now()
    expires_at = now + timedelta(hours=hours)

    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT INTO inboxes(username, email_address, created_at, expires_at) VALUES(?,?,?,?)",
            (username, address, now.isoformat(), expires_at.isoformat()),
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO inbox_history(
                username, email_address, session_id, duration_hours, created_at, expires_at
            ) VALUES(?,?,?,?,?,?)
            """,
            (
                username,
                address,
                st.session_state.get("session_id", ""),
                int(hours),
                now.isoformat(),
                expires_at.isoformat(),
            ),
        )
        conn.commit()
        log_event("inbox_created", address, {"duration_hours": int(hours)})
        return address, None
    except sqlite3.IntegrityError:
        conn.rollback()
        log_event("inbox_create_failed", address, {"reason": "already_reserved"})
        return None, "That inbox name is already reserved. Try another one."
    except Exception as exc:
        conn.rollback()
        log_event("inbox_create_failed", address, {"reason": "database_error", "error": str(exc)[:160]})
        return None, "Could not reserve the inbox right now. Please try again."



# -----------------------------------------------------------------------------
# Email helpers
# -----------------------------------------------------------------------------
def decode_mime(value):
    if not value:
        return ""

    output = ""
    for part, encoding in decode_header(value):
        if isinstance(part, bytes):
            output += part.decode(encoding or "utf-8", errors="replace")
        else:
            output += part
    return output


def strip_html(value: str) -> str:
    value = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", value)
    value = re.sub(r"(?s)<[^>]+>", " ", value)
    value = html.unescape(value)
    value = re.sub(r"[ \t]+", " ", value)
    value = re.sub(r"\n\s*\n\s*\n+", "\n\n", value)
    return value.strip()


def message_text(msg):
    if msg.is_multipart():
        html_fallback = ""

        for part in msg.walk():
            content_type = part.get_content_type()
            disposition = str(part.get("Content-Disposition", ""))

            if "attachment" in disposition.lower():
                continue

            payload = part.get_payload(decode=True)
            if not payload:
                continue

            charset = part.get_content_charset() or "utf-8"
            text = payload.decode(charset, errors="replace")

            if content_type == "text/plain":
                return text.strip()
            if content_type == "text/html" and not html_fallback:
                html_fallback = strip_html(text)

        return html_fallback

    payload = msg.get_payload(decode=True)
    if not payload:
        return ""

    charset = msg.get_content_charset() or "utf-8"
    text = payload.decode(charset, errors="replace")

    if msg.get_content_type() == "text/html":
        return strip_html(text)
    return text.strip()


def recipient_headers(msg):
    names = [
        "To",
        "Delivered-To",
        "Envelope-To",
        "X-Original-To",
        "X-Forwarded-To",
        "Resent-To",
    ]

    values = []
    for name in names:
        for raw_value in msg.get_all(name, []):
            values.append(decode_mime(raw_value))

    return " ".join(values)


def record_message_observation(address: str, msg):
    try:
        message_id = decode_mime(msg.get("Message-ID", "")).strip()
        sender = decode_mime(msg.get("From", ""))[:500]
        recipient = decode_mime(msg.get("To", ""))[:500] or address
        subject = (decode_mime(msg.get("Subject", "")) or "No subject")[:500]
        message_date = decode_mime(msg.get("Date", ""))[:300]
        fingerprint_source = "|".join([address.lower(), message_id, sender, recipient, subject, message_date])
        fingerprint = hashlib.sha256(fingerprint_source.encode("utf-8", errors="ignore")).hexdigest()
        before = conn.total_changes
        conn.execute(
            """
            INSERT OR IGNORE INTO message_history(
                fingerprint,email_address,sender,recipient,subject,message_date,observed_at
            ) VALUES(?,?,?,?,?,?,?)
            """,
            (fingerprint, address, sender, recipient, subject, message_date, utc_now().isoformat()),
        )
        conn.commit()
        inserted = conn.total_changes > before
        if inserted:
            log_event("message_observed", address, {"subject": subject[:160]})
        return inserted
    except Exception:
        return False


def sync_mailbox_metadata(max_messages=500):
    if not all([IMAP_HOST, IMAP_USER, IMAP_PASSWORD]):
        return 0, 0, "IMAP is not configured."

    mail = None
    scanned = 0
    inserted = 0
    try:
        mail = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
        mail.login(IMAP_USER, IMAP_PASSWORD)
        status, _ = mail.select(INBOX_FOLDER)
        if status != "OK":
            return 0, 0, f"Could not open IMAP folder: {INBOX_FOLDER}."
        status, data = mail.search(None, "ALL")
        if status != "OK" or not data:
            return 0, 0, "Could not read the mailbox."

        message_ids = data[0].split()[-int(max_messages):]
        address_pattern = re.compile(rf"[A-Za-z0-9._%+\-]+@{re.escape(DOMAIN)}", re.I)
        known_addresses = {row[0].lower() for row in conn.execute("SELECT DISTINCT email_address FROM inbox_history").fetchall()}

        for message_id in reversed(message_ids):
            status, message_data = mail.fetch(message_id, "(BODY.PEEK[HEADER])")
            if status != "OK" or not message_data or not message_data[0]:
                continue
            scanned += 1
            raw = message_data[0][1]
            msg = email.message_from_bytes(raw)
            recipients = recipient_headers(msg)
            matched = {m.lower() for m in address_pattern.findall(recipients)}
            for address in matched:
                if address in known_addresses:
                    if record_message_observation(address, msg):
                        inserted += 1

        log_event("admin_mailbox_sync", details={"headers_scanned": scanned, "new_messages": inserted})
        return scanned, inserted, None
    except imaplib.IMAP4.error:
        return scanned, inserted, "IMAP authentication failed."
    except Exception as exc:
        return scanned, inserted, f"Mailbox sync error: {exc}"
    finally:
        if mail is not None:
            try:
                mail.logout()
            except Exception:
                pass


def fetch_messages_for(address, limit=20):
    if not all([IMAP_HOST, IMAP_USER, IMAP_PASSWORD]):
        return [], "IMAP is not configured yet. Add your Hostinger mail settings to the .env file."

    messages = []
    mail = None

    try:
        mail = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
        mail.login(IMAP_USER, IMAP_PASSWORD)

        status, _ = mail.select(INBOX_FOLDER)
        if status != "OK":
            return [], f"Could not open the IMAP folder: {INBOX_FOLDER}."

        status, data = mail.search(None, "ALL")
        if status != "OK" or not data:
            return [], "Could not read the mailbox."

        # Limit the scan for performance, then filter locally by recipient headers.
        message_ids = data[0].split()[-250:]
        target = address.lower()

        for message_id in reversed(message_ids):
            status, message_data = mail.fetch(message_id, "(RFC822)")
            if status != "OK" or not message_data or not message_data[0]:
                continue

            raw = message_data[0][1]
            msg = email.message_from_bytes(raw)
            recipients = recipient_headers(msg).lower()

            if target not in recipients:
                continue

            record_message_observation(address, msg)

            messages.append(
                {
                    "from": decode_mime(msg.get("From", "")),
                    "to": decode_mime(msg.get("To", "")) or address,
                    "subject": decode_mime(msg.get("Subject", "")) or "No subject",
                    "date": decode_mime(msg.get("Date", "")),
                    "body": message_text(msg),
                }
            )

            if len(messages) >= limit:
                break

        return messages, None

    except imaplib.IMAP4.error:
        log_event("mail_fetch_error", address, {"reason": "authentication_failed"})
        return [], "IMAP authentication failed. Check IMAP_USER and IMAP_PASSWORD in your .env file."
    except Exception as exc:
        log_event("mail_fetch_error", address, {"reason": "connection_error", "error": str(exc)[:160]})
        return [], f"Mailbox connection error: {exc}"
    finally:
        if mail is not None:
            try:
                mail.logout()
            except Exception:
                pass


# -----------------------------------------------------------------------------
# Super Admin analytics
# -----------------------------------------------------------------------------
def admin_route_requested():
    try:
        return str(st.query_params.get("admin", "")).lower() in {"1", "true", "yes"}
    except Exception:
        return False


def period_bounds(period_name: str, custom_start=None, custom_end=None):
    now = local_now()
    if period_name == "Today":
        start_local = datetime.combine(now.date(), time.min, tzinfo=APP_TIMEZONE)
        end_local = start_local + timedelta(days=1)
    elif period_name == "This Week":
        start_date = now.date() - timedelta(days=now.weekday())
        start_local = datetime.combine(start_date, time.min, tzinfo=APP_TIMEZONE)
        end_local = start_local + timedelta(days=7)
    elif period_name == "This Month":
        start_local = datetime(now.year, now.month, 1, tzinfo=APP_TIMEZONE)
        if now.month == 12:
            end_local = datetime(now.year + 1, 1, 1, tzinfo=APP_TIMEZONE)
        else:
            end_local = datetime(now.year, now.month + 1, 1, tzinfo=APP_TIMEZONE)
    elif period_name == "Custom":
        start_d = custom_start or now.date()
        end_d = custom_end or start_d
        if end_d < start_d:
            start_d, end_d = end_d, start_d
        start_local = datetime.combine(start_d, time.min, tzinfo=APP_TIMEZONE)
        end_local = datetime.combine(end_d + timedelta(days=1), time.min, tzinfo=APP_TIMEZONE)
    else:
        return None, None, "All Time"

    label = f"{start_local.strftime('%d %b %Y')} — {(end_local - timedelta(seconds=1)).strftime('%d %b %Y')}"
    return start_local.astimezone(timezone.utc), end_local.astimezone(timezone.utc), label


def sql_period_clause(column: str, start_utc, end_utc):
    if start_utc is None or end_utc is None:
        return "", []
    return f" WHERE {column} >= ? AND {column} < ?", [start_utc.isoformat(), end_utc.isoformat()]


def rows_to_df(rows):
    return pd.DataFrame([dict(row) for row in rows]) if rows else pd.DataFrame()


def load_admin_data(start_utc, end_utc):
    history_where, history_params = sql_period_clause("created_at", start_utc, end_utc)
    message_where, message_params = sql_period_clause("observed_at", start_utc, end_utc)
    event_where, event_params = sql_period_clause("created_at", start_utc, end_utc)

    reservations = conn.execute(
        f"""
        SELECT h.*,
               (SELECT COUNT(*) FROM message_history m
                WHERE m.email_address = h.email_address
                  AND m.observed_at >= h.created_at
                  AND m.observed_at <= h.expires_at) AS message_count
        FROM inbox_history h
        {history_where}
        ORDER BY h.created_at DESC
        """,
        history_params,
    ).fetchall()

    messages = conn.execute(
        f"SELECT * FROM message_history {message_where} ORDER BY observed_at DESC",
        message_params,
    ).fetchall()

    events = conn.execute(
        f"SELECT * FROM activity_events {event_where} ORDER BY created_at DESC",
        event_params,
    ).fetchall()

    return rows_to_df(reservations), rows_to_df(messages), rows_to_df(events)


def prepare_reservations_df(df):
    if df.empty:
        return df
    out = df.copy()
    now = utc_now()
    out["Created At"] = out["created_at"].apply(format_local)
    out["Expires At"] = out["expires_at"].apply(format_local)
    out["Status"] = out["expires_at"].apply(lambda x: "Active" if (parse_utc(x) or now) > now else "Expired")
    out["Session"] = out["session_id"].fillna("").astype(str).str[:10]
    out = out.rename(
        columns={
            "username": "Username",
            "email_address": "Email Address",
            "duration_hours": "Duration (h)",
            "message_count": "Messages",
        }
    )
    return out[["Email Address", "Username", "Created At", "Expires At", "Status", "Duration (h)", "Messages", "Session"]]


def prepare_messages_df(df):
    if df.empty:
        return df
    out = df.copy()
    out["Observed At"] = out["observed_at"].apply(format_local)
    out = out.rename(columns={"email_address": "Inbox", "sender": "From", "subject": "Subject", "message_date": "Mail Date"})
    return out[["Observed At", "Inbox", "From", "Subject", "Mail Date"]]


def prepare_events_df(df):
    if df.empty:
        return df
    out = df.copy()
    out["Time"] = out["created_at"].apply(format_local)
    out["Session"] = out["session_id"].fillna("").astype(str).str[:10]
    out["Details"] = out["details_json"].fillna("{}")
    out = out.rename(columns={"event_type": "Event", "email_address": "Inbox"})
    return out[["Time", "Event", "Inbox", "Session", "Details"]]


def dataframe_csv_bytes(df):
    return df.to_csv(index=False).encode("utf-8-sig") if not df.empty else b""


def build_pdf_report(period_label, raw_reservations, raw_messages, raw_events):
    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=landscape(A4),
        rightMargin=12 * mm,
        leftMargin=12 * mm,
        topMargin=12 * mm,
        bottomMargin=12 * mm,
        title=f"QuickInbox Super Admin Report - {period_label}",
        author="QuickInbox",
    )
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle("QTitle", parent=styles["Title"], fontSize=20, leading=24, textColor=colors.HexColor("#1F2937"), alignment=TA_LEFT)
    h2 = ParagraphStyle("QH2", parent=styles["Heading2"], fontSize=12, leading=15, spaceBefore=8, spaceAfter=6)
    small = ParagraphStyle("QSmall", parent=styles["BodyText"], fontSize=7.5, leading=9.5)
    normal = ParagraphStyle("QNormal", parent=styles["BodyText"], fontSize=9, leading=12)

    now = utc_now()
    created_count = len(raw_reservations)
    active_count = 0
    if not raw_reservations.empty:
        active_count = sum((parse_utc(v) or now) > now for v in raw_reservations["expires_at"].tolist())
    expired_count = max(0, created_count - active_count)
    message_count = len(raw_messages)
    sessions = raw_reservations["session_id"].dropna().replace("", pd.NA).nunique() if not raw_reservations.empty else 0
    inboxes_with_messages = int((raw_reservations["message_count"] > 0).sum()) if not raw_reservations.empty else 0
    receive_rate = (inboxes_with_messages / created_count * 100) if created_count else 0

    story = [
        Paragraph("QuickInbox — Super Admin Analytics", title_style),
        Paragraph(f"Period: {html.escape(period_label)} | Time zone: {html.escape(APP_TIMEZONE_NAME)} | Generated: {html.escape(local_now().strftime('%Y-%m-%d %H:%M'))}", normal),
        Spacer(1, 5 * mm),
    ]

    kpi_data = [
        ["Inboxes created", "Active*", "Expired*", "Messages observed", "Unique sessions", "Inbox receive rate"],
        [str(created_count), str(active_count), str(expired_count), str(message_count), str(sessions), f"{receive_rate:.1f}%"],
    ]
    kpi = Table(kpi_data, colWidths=[42 * mm] * 6, repeatRows=1)
    kpi.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#EEF2FF")),
        ("TEXTCOLOR", (0, 0), (-1, -1), colors.HexColor("#111827")),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTNAME", (0, 1), (-1, 1), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, 0), 7),
        ("FONTSIZE", (0, 1), (-1, 1), 11),
        ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#CBD5E1")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]))
    story.extend([kpi, Spacer(1, 5 * mm)])

    # Daily breakdown
    story.append(Paragraph("Daily activity", h2))
    if raw_reservations.empty and raw_messages.empty:
        story.append(Paragraph("No activity in this period.", normal))
    else:
        creation_daily = {}
        for value in raw_reservations.get("created_at", pd.Series(dtype=str)).tolist():
            parsed = parse_utc(value)
            if parsed:
                key = parsed.astimezone(APP_TIMEZONE).strftime("%Y-%m-%d")
                creation_daily[key] = creation_daily.get(key, 0) + 1
        message_daily = {}
        for value in raw_messages.get("observed_at", pd.Series(dtype=str)).tolist():
            parsed = parse_utc(value)
            if parsed:
                key = parsed.astimezone(APP_TIMEZONE).strftime("%Y-%m-%d")
                message_daily[key] = message_daily.get(key, 0) + 1
        days = sorted(set(creation_daily) | set(message_daily))
        daily_data = [["Date", "Inboxes", "Messages"]] + [[d, creation_daily.get(d, 0), message_daily.get(d, 0)] for d in days]
        daily_table = Table(daily_data, colWidths=[55 * mm, 35 * mm, 35 * mm], repeatRows=1)
        daily_table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#F1F5F9")),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, -1), 8),
            ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#CBD5E1")),
            ("ALIGN", (1, 1), (-1, -1), "CENTER"),
        ]))
        story.append(daily_table)

    # Reservation detail
    story.extend([Spacer(1, 5 * mm), Paragraph("Reservation detail", h2)])
    prepared = prepare_reservations_df(raw_reservations)
    if prepared.empty:
        story.append(Paragraph("No reservations in this period.", normal))
    else:
        headers = list(prepared.columns)
        data = [[Paragraph(str(x), small) for x in headers]]
        for _, row in prepared.iterrows():
            data.append([Paragraph(html.escape(str(row[col])), small) for col in headers])
        tbl = Table(data, colWidths=[55*mm, 30*mm, 34*mm, 34*mm, 22*mm, 22*mm, 20*mm, 25*mm], repeatRows=1)
        tbl.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#E0E7FF")),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("GRID", (0, 0), (-1, -1), 0.2, colors.HexColor("#CBD5E1")),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F8FAFC")]),
        ]))
        story.append(tbl)

    # Message metadata detail
    story.extend([PageBreak(), Paragraph("Observed message metadata", h2), Paragraph("Message bodies are not stored in analytics. This section contains metadata only.", normal)])
    prepared_m = prepare_messages_df(raw_messages)
    if prepared_m.empty:
        story.append(Paragraph("No messages observed in this period.", normal))
    else:
        headers = list(prepared_m.columns)
        data = [[Paragraph(str(x), small) for x in headers]]
        for _, row in prepared_m.iterrows():
            data.append([Paragraph(html.escape(str(row[col])), small) for col in headers])
        tbl = Table(data, colWidths=[35*mm, 55*mm, 65*mm, 82*mm, 48*mm], repeatRows=1)
        tbl.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#E0E7FF")),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("GRID", (0, 0), (-1, -1), 0.2, colors.HexColor("#CBD5E1")),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F8FAFC")]),
        ]))
        story.append(tbl)

    # Event summary + full event detail
    story.extend([PageBreak(), Paragraph("Activity event log", h2)])
    prepared_e = prepare_events_df(raw_events)
    if prepared_e.empty:
        story.append(Paragraph("No activity events in this period.", normal))
    else:
        event_counts = prepared_e["Event"].value_counts().reset_index()
        event_counts.columns = ["Event", "Count"]
        ec_data = [["Event", "Count"]] + event_counts.values.tolist()
        ec = Table(ec_data, colWidths=[80*mm, 30*mm], repeatRows=1)
        ec.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#F1F5F9")),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#CBD5E1")),
            ("FONTSIZE", (0, 0), (-1, -1), 8),
        ]))
        story.extend([ec, Spacer(1, 4*mm)])
        headers = list(prepared_e.columns)
        data = [[Paragraph(str(x), small) for x in headers]]
        for _, row in prepared_e.iterrows():
            data.append([Paragraph(html.escape(str(row[col])), small) for col in headers])
        ev = Table(data, colWidths=[34*mm, 35*mm, 58*mm, 28*mm, 120*mm], repeatRows=1)
        ev.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#E0E7FF")),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("GRID", (0, 0), (-1, -1), 0.2, colors.HexColor("#CBD5E1")),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F8FAFC")]),
        ]))
        story.append(ev)

    story.extend([Spacer(1, 4 * mm), Paragraph("* Active/expired status is calculated at report generation time.", small)])
    doc.build(story)
    return buffer.getvalue()


def sqlite_backup_bytes():
    import tempfile
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
            temp_path = tmp.name
        target = sqlite3.connect(temp_path)
        with target:
            conn.backup(target)
        target.close()
        return Path(temp_path).read_bytes()
    finally:
        if temp_path:
            try:
                Path(temp_path).unlink(missing_ok=True)
            except Exception:
                pass


def render_admin_login():
    st.markdown(
        """
        <div class="qa-login-wrap">
            <div class="qa-login-badge">SUPER ADMIN</div>
            <h1>QuickInbox Control Center</h1>
            <p>Private analytics, operational activity, exports and system health.</p>
        </div>
        """,
        unsafe_allow_html=True,
    )

    if not SUPER_ADMIN_USERNAME or not SUPER_ADMIN_PASSWORD:
        st.error("Super Admin credentials are not configured. Add SUPER_ADMIN_USERNAME and SUPER_ADMIN_PASSWORD to the server .env file, then restart the container.")
        return

    with st.form("admin_login", clear_on_submit=False):
        username = st.text_input("Username")
        password = st.text_input("Password", type="password")
        submitted = st.form_submit_button("Sign in", type="primary", use_container_width=True)

    if submitted:
        user_ok = hmac.compare_digest(username.strip(), SUPER_ADMIN_USERNAME)
        pass_ok = hmac.compare_digest(password, SUPER_ADMIN_PASSWORD)
        if user_ok and pass_ok:
            st.session_state.admin_authenticated = True
            st.session_state.admin_failed_attempts = 0
            log_event("admin_login_success", details={"username": username.strip()})
            st.rerun()
        else:
            st.session_state.admin_failed_attempts += 1
            log_event("admin_login_failed", details={"username": username.strip(), "attempt": st.session_state.admin_failed_attempts})
            st.error("Invalid Super Admin credentials.")


def render_super_admin():
    st.markdown(
        """
        <style>
        .qa-login-wrap, .qa-head {
            padding: 28px;
            border: 1px solid var(--qb-border);
            border-radius: 20px;
            background: linear-gradient(135deg, var(--qb-surface), var(--qb-surface-2));
            box-shadow: 0 18px 45px var(--qb-shadow);
            margin-bottom: 20px;
        }
        .qa-login-wrap h1, .qa-head h1 { margin: 8px 0 6px; color: var(--qb-text); font-size: 34px; letter-spacing: -1px; }
        .qa-login-wrap p, .qa-head p { color: var(--qb-muted); margin: 0; }
        .qa-login-badge { display:inline-block; padding:6px 10px; border-radius:999px; background:var(--qb-soft-blue); color:var(--qb-accent); font-size:11px; font-weight:900; letter-spacing:1px; }
        .qa-kpi { padding: 18px; border: 1px solid var(--qb-border); border-radius: 16px; background: var(--qb-surface); min-height: 120px; }
        .qa-kpi-label { color: var(--qb-muted); font-size: 12px; font-weight: 800; text-transform: uppercase; letter-spacing:.7px; }
        .qa-kpi-value { color: var(--qb-text); font-size: 29px; font-weight: 900; margin-top: 8px; }
        .qa-kpi-note { color: var(--qb-muted); font-size: 12px; margin-top: 4px; }
        .qa-panel-title { color: var(--qb-text); font-size: 19px; font-weight: 900; margin: 10px 0 4px; }
        .qa-panel-sub { color: var(--qb-muted); font-size: 13px; margin-bottom: 12px; }
        </style>
        """,
        unsafe_allow_html=True,
    )

    if not st.session_state.admin_authenticated:
        render_admin_login()
        return

    clean_expired_inboxes()

    top_l, top_r = st.columns([8, 2], vertical_alignment="center")
    with top_l:
        st.markdown(
            f"""
            <div class="qa-head">
                <div class="qa-login-badge">SUPER ADMIN</div>
                <h1>QuickInbox Analytics</h1>
                <p>Live operational data · {html.escape(APP_TIMEZONE_NAME)} · Message bodies are not stored in analytics.</p>
            </div>
            """,
            unsafe_allow_html=True,
        )
    with top_r:
        if st.button("Log out", use_container_width=True):
            log_event("admin_logout", details={"username": SUPER_ADMIN_USERNAME})
            st.session_state.admin_authenticated = False
            st.rerun()

    st.markdown("### Reporting period")
    filter_col, refresh_col = st.columns([8, 2], vertical_alignment="bottom")
    with filter_col:
        period_name = st.radio("Period", ["Today", "This Week", "This Month", "Custom", "All Time"], horizontal=True, label_visibility="collapsed")
    custom_start = custom_end = None
    if period_name == "Custom":
        c1, c2 = st.columns(2)
        with c1:
            custom_start = st.date_input("From", value=local_now().date() - timedelta(days=6))
        with c2:
            custom_end = st.date_input("To", value=local_now().date())
    with refresh_col:
        if st.button("↻ Refresh", use_container_width=True):
            st.rerun()

    start_utc, end_utc, period_label = period_bounds(period_name, custom_start, custom_end)
    raw_reservations, raw_messages, raw_events = load_admin_data(start_utc, end_utc)

    now = utc_now()
    created_count = len(raw_reservations)
    active_in_period = sum((parse_utc(v) or now) > now for v in raw_reservations.get("expires_at", pd.Series(dtype=str)).tolist()) if not raw_reservations.empty else 0
    expired_in_period = created_count - active_in_period
    messages_count = len(raw_messages)
    unique_sessions = raw_reservations["session_id"].dropna().replace("", pd.NA).nunique() if not raw_reservations.empty else 0
    inboxes_with_messages = int((raw_reservations["message_count"] > 0).sum()) if not raw_reservations.empty else 0
    receive_rate = (inboxes_with_messages / created_count * 100) if created_count else 0.0
    global_active = conn.execute("SELECT COUNT(*) FROM inboxes WHERE expires_at > ?", (utc_now().isoformat(),)).fetchone()[0]
    total_lifetime = conn.execute("SELECT COUNT(*) FROM inbox_history").fetchone()[0]

    st.caption(f"Filter: {period_label} · Updated {local_now().strftime('%Y-%m-%d %H:%M:%S')}")

    sync_col, sync_note = st.columns([2.2, 7.8], vertical_alignment="center")
    with sync_col:
        if st.button("Sync mailbox metadata", use_container_width=True):
            with st.spinner("Scanning recent mailbox headers..."):
                scanned, new_messages, sync_error = sync_mailbox_metadata(500)
            if sync_error:
                st.error(sync_error)
            else:
                st.success(f"Scanned {scanned} recent message headers · {new_messages} new message records added.")
                st.rerun()
    with sync_note:
        st.caption("Scans the latest 500 IMAP message headers and records metadata for known QuickInbox addresses. Message bodies are not stored.")

    k1, k2, k3, k4 = st.columns(4)
    k5, k6, k7, k8 = st.columns(4)
    kpis = [
        (k1, "Created", created_count, "Reservations in selected period"),
        (k2, "Messages", messages_count, "Unique messages observed"),
        (k3, "Unique sessions", unique_sessions, "Browser sessions creating inboxes"),
        (k4, "Receive rate", f"{receive_rate:.1f}%", "Created inboxes that received mail"),
        (k5, "Active in period", active_in_period, "Selected reservations still active"),
        (k6, "Expired in period", expired_in_period, "Selected reservations already expired"),
        (k7, "Active now", global_active, "All currently reserved inboxes"),
        (k8, "Lifetime created", total_lifetime, "Since analytics history began"),
    ]
    for col, label, value, note in kpis:
        with col:
            st.markdown(f'<div class="qa-kpi"><div class="qa-kpi-label">{html.escape(str(label))}</div><div class="qa-kpi-value">{html.escape(str(value))}</div><div class="qa-kpi-note">{html.escape(str(note))}</div></div>', unsafe_allow_html=True)

    # Analysis data
    day_creation = pd.DataFrame(columns=["Date", "Inboxes"])
    day_messages = pd.DataFrame(columns=["Date", "Messages"])
    hour_df = pd.DataFrame(columns=["Hour", "Inboxes"])
    duration_df = pd.DataFrame(columns=["Duration", "Count"])

    if not raw_reservations.empty:
        times = pd.to_datetime(raw_reservations["created_at"], utc=True, errors="coerce").dt.tz_convert(APP_TIMEZONE_NAME)
        day_creation = times.dt.strftime("%Y-%m-%d").value_counts().sort_index().rename_axis("Date").reset_index(name="Inboxes")
        hour_df = times.dt.hour.value_counts().sort_index().rename_axis("Hour").reset_index(name="Inboxes")
        hour_df["Hour"] = hour_df["Hour"].apply(lambda h: f"{int(h):02d}:00")
        duration_df = raw_reservations["duration_hours"].value_counts().sort_index().rename_axis("Duration").reset_index(name="Count")
        duration_df["Duration"] = duration_df["Duration"].apply(lambda h: f"{int(h)}h")

    if not raw_messages.empty:
        mtimes = pd.to_datetime(raw_messages["observed_at"], utc=True, errors="coerce").dt.tz_convert(APP_TIMEZONE_NAME)
        day_messages = mtimes.dt.strftime("%Y-%m-%d").value_counts().sort_index().rename_axis("Date").reset_index(name="Messages")

    overview_tab, reservations_tab, messages_tab, activity_tab, exports_tab, system_tab = st.tabs([
        "Overview", "Reservations", "Messages", "Activity", "Exports", "System"
    ])

    with overview_tab:
        st.markdown('<div class="qa-panel-title">Activity over time</div><div class="qa-panel-sub">Inbox creation and observed email volume for the selected period.</div>', unsafe_allow_html=True)
        if day_creation.empty and day_messages.empty:
            st.info("No activity in this period yet.")
        else:
            combined = pd.merge(day_creation, day_messages, on="Date", how="outer").fillna(0).set_index("Date")
            st.line_chart(combined, use_container_width=True)

        c1, c2 = st.columns(2)
        with c1:
            st.markdown('<div class="qa-panel-title">Reservation duration</div>', unsafe_allow_html=True)
            if duration_df.empty:
                st.info("No reservation data.")
            else:
                st.bar_chart(duration_df.set_index("Duration"), use_container_width=True)
        with c2:
            st.markdown('<div class="qa-panel-title">Creation by hour</div>', unsafe_allow_html=True)
            if hour_df.empty:
                st.info("No hourly activity.")
            else:
                st.bar_chart(hour_df.set_index("Hour"), use_container_width=True)

        st.markdown('<div class="qa-panel-title">Event mix</div>', unsafe_allow_html=True)
        if raw_events.empty:
            st.info("No events in this period.")
        else:
            event_mix = raw_events["event_type"].value_counts().rename_axis("Event").reset_index(name="Count")
            st.bar_chart(event_mix.set_index("Event"), use_container_width=True)

    with reservations_tab:
        st.markdown('<div class="qa-panel-title">Inbox reservations</div><div class="qa-panel-sub">Every reservation created in the selected period, including status, duration, session and message count.</div>', unsafe_allow_html=True)
        table = prepare_reservations_df(raw_reservations)
        search = st.text_input("Search reservations", placeholder="email address or username", key="admin_reservation_search")
        if search and not table.empty:
            mask = table.astype(str).apply(lambda col: col.str.contains(search, case=False, na=False)).any(axis=1)
            table = table[mask]
        st.dataframe(table, use_container_width=True, hide_index=True, height=520)

    with messages_tab:
        st.markdown('<div class="qa-panel-title">Observed message metadata</div><div class="qa-panel-sub">Sender, subject and timing. Message bodies are intentionally not stored in analytics.</div>', unsafe_allow_html=True)
        table = prepare_messages_df(raw_messages)
        search = st.text_input("Search messages", placeholder="inbox, sender or subject", key="admin_message_search")
        if search and not table.empty:
            mask = table.astype(str).apply(lambda col: col.str.contains(search, case=False, na=False)).any(axis=1)
            table = table[mask]
        st.dataframe(table, use_container_width=True, hide_index=True, height=520)

    with activity_tab:
        st.markdown('<div class="qa-panel-title">Activity log</div><div class="qa-panel-sub">Operational events such as sessions, inbox creation, email observations, refreshes and admin logins.</div>', unsafe_allow_html=True)
        table = prepare_events_df(raw_events)
        event_options = ["All"] + sorted(table["Event"].dropna().unique().tolist()) if not table.empty else ["All"]
        selected_event = st.selectbox("Event type", event_options)
        if selected_event != "All" and not table.empty:
            table = table[table["Event"] == selected_event]
        st.dataframe(table, use_container_width=True, hide_index=True, height=540)

    with exports_tab:
        st.markdown('<div class="qa-panel-title">Filtered exports</div><div class="qa-panel-sub">Every export below uses the reporting period selected above.</div>', unsafe_allow_html=True)
        pdf = build_pdf_report(period_label, raw_reservations, raw_messages, raw_events)
        filename_period = re.sub(r"[^A-Za-z0-9_-]+", "_", period_label).strip("_") or "all_time"
        st.download_button(
            "Download PDF report",
            data=pdf,
            file_name=f"QuickInbox_Admin_Report_{filename_period}.pdf",
            mime="application/pdf",
            type="primary",
            use_container_width=True,
        )
        e1, e2, e3 = st.columns(3)
        with e1:
            st.download_button("Reservations CSV", dataframe_csv_bytes(prepare_reservations_df(raw_reservations)), f"quickinbox_reservations_{filename_period}.csv", "text/csv", use_container_width=True)
        with e2:
            st.download_button("Messages CSV", dataframe_csv_bytes(prepare_messages_df(raw_messages)), f"quickinbox_messages_{filename_period}.csv", "text/csv", use_container_width=True)
        with e3:
            st.download_button("Activity CSV", dataframe_csv_bytes(prepare_events_df(raw_events)), f"quickinbox_activity_{filename_period}.csv", "text/csv", use_container_width=True)

    with system_tab:
        st.markdown('<div class="qa-panel-title">System health & backup</div><div class="qa-panel-sub">Operational checks for the running QuickInbox deployment.</div>', unsafe_allow_html=True)
        db_size = DB_FILE.stat().st_size if DB_FILE.exists() else 0
        last_event_row = conn.execute("SELECT event_type, created_at FROM activity_events ORDER BY id DESC LIMIT 1").fetchone()
        health = pd.DataFrame([
            {"Check": "Database", "Status": "OK" if DB_FILE.exists() else "Missing", "Detail": str(DB_FILE)},
            {"Check": "Database size", "Status": "Info", "Detail": f"{db_size / 1024:.1f} KB"},
            {"Check": "IMAP configuration", "Status": "Configured" if all([IMAP_HOST, IMAP_USER, IMAP_PASSWORD]) else "Incomplete", "Detail": f"{IMAP_HOST}:{IMAP_PORT}"},
            {"Check": "Mail domain", "Status": "Configured" if DOMAIN != "yourdomain.com" else "Incomplete", "Detail": DOMAIN},
            {"Check": "Application timezone", "Status": "OK", "Detail": APP_TIMEZONE_NAME},
            {"Check": "Last event", "Status": last_event_row["event_type"] if last_event_row else "None", "Detail": format_local(last_event_row["created_at"]) if last_event_row else ""},
        ])
        st.dataframe(health, use_container_width=True, hide_index=True)
        st.download_button(
            "Download SQLite database backup",
            data=sqlite_backup_bytes(),
            file_name=f"quickinbox_backup_{local_now().strftime('%Y%m%d_%H%M%S')}.db",
            mime="application/octet-stream",
            use_container_width=True,
        )
        st.info("Analytics history starts when this version is deployed. Previously deleted expired reservations cannot be reconstructed from the old database.")


# -----------------------------------------------------------------------------
# Theme / CSS
# -----------------------------------------------------------------------------
def apply_theme(dark_mode: bool):
    if dark_mode:
        colors = {
            "bg": "#080B12",
            "surface": "#0F1420",
            "surface2": "#141B29",
            "surface3": "#192234",
            "text": "#F8FAFC",
            "muted": "#94A3B8",
            "border": "#273349",
            "input": "#111827",
            "accent": "#7C83FF",
            "accent2": "#5B61F6",
            "hero1": "#111A31",
            "hero2": "#2D285F",
            "soft_blue": "rgba(124,131,255,.16)",
            "success": "#38D39F",
            "danger": "#FF626D",
            "shadow": "rgba(0,0,0,.38)",
        }
    else:
        colors = {
            "bg": "#F6F8FC",
            "surface": "#FFFFFF",
            "surface2": "#F8FAFD",
            "surface3": "#EEF2F8",
            "text": "#111827",
            "muted": "#64748B",
            "border": "#D9E1EC",
            "input": "#FFFFFF",
            "accent": "#6269F4",
            "accent2": "#4F46E5",
            "hero1": "#1B2744",
            "hero2": "#3A327F",
            "soft_blue": "rgba(98,105,244,.10)",
            "success": "#148A65",
            "danger": "#E64955",
            "shadow": "rgba(15,23,42,.09)",
        }

    st.markdown(
        f"""
        <style>
        :root {{
            --qb-bg: {colors['bg']};
            --qb-surface: {colors['surface']};
            --qb-surface-2: {colors['surface2']};
            --qb-surface-3: {colors['surface3']};
            --qb-text: {colors['text']};
            --qb-muted: {colors['muted']};
            --qb-border: {colors['border']};
            --qb-input: {colors['input']};
            --qb-accent: {colors['accent']};
            --qb-accent-2: {colors['accent2']};
            --qb-hero-1: {colors['hero1']};
            --qb-hero-2: {colors['hero2']};
            --qb-soft-blue: {colors['soft_blue']};
            --qb-success: {colors['success']};
            --qb-danger: {colors['danger']};
            --qb-shadow: {colors['shadow']};
        }}

        html {{ scroll-behavior: smooth; }}

        .stApp {{
            background:
                radial-gradient(circle at 12% 0%, var(--qb-soft-blue), transparent 28%),
                var(--qb-bg);
            color: var(--qb-text);
        }}

        [data-testid="stHeader"] {{
            background: transparent;
        }}

        [data-testid="stToolbar"] {{
            right: 1.2rem;
        }}

        .main .block-container,
        [data-testid="stMainBlockContainer"] {{
            max-width: 1180px;
            padding-top: 1.6rem;
            padding-bottom: 4rem;
            margin: 0 auto;
        }}

        .stApp,
        .stApp p,
        .stApp div,
        .stApp label,
        .stApp input,
        .stApp textarea,
        .stApp button {{
            font-family: Inter, "Segoe UI", Arial, sans-serif;
        }}

        /* Keep Streamlit / Material icon glyphs as icons, not literal text like "arrow_down". */
        .material-symbols-rounded,
        .material-symbols-outlined,
        .material-icons,
        [data-testid="stExpanderToggleIcon"] {{
            font-family: "Material Symbols Rounded", "Material Symbols Outlined", "Material Icons" !important;
            font-weight: normal !important;
            font-style: normal !important;
            line-height: 1 !important;
            letter-spacing: normal !important;
            text-transform: none !important;
            white-space: nowrap !important;
            word-wrap: normal !important;
            direction: ltr !important;
            -webkit-font-feature-settings: "liga" !important;
            -webkit-font-smoothing: antialiased !important;
        }}

        /* Fix text that was visually too small */
        .stApp p {{
            font-size: 15px;
            line-height: 1.65;
        }}
        .stApp label {{
            font-size: 14px !important;
            font-weight: 700 !important;
            color: var(--qb-text) !important;
        }}
        .stApp small {{
            font-size: 13px;
        }}

        .qb-brand-row {{
            display: flex;
            align-items: center;
            justify-content: space-between;
            margin: 2px 0 18px;
        }}
        .qb-brand {{
            font-size: 22px;
            line-height: 1;
            font-weight: 900;
            letter-spacing: -0.7px;
            color: var(--qb-text);
        }}
        .qb-brand span {{ color: var(--qb-accent); }}
        .qb-brand-sub {{
            margin-top: 7px;
            color: var(--qb-muted);
            font-size: 13px;
            font-weight: 500;
        }}

        .qb-hero {{
            position: relative;
            overflow: hidden;
            min-height: 265px;
            display: flex;
            align-items: center;
            padding: 46px 48px;
            margin-bottom: 20px;
            border: 1px solid rgba(255,255,255,.08);
            border-radius: 24px;
            color: #fff;
            background:
                radial-gradient(circle at 86% 20%, rgba(124,131,255,.34), transparent 33%),
                linear-gradient(135deg, var(--qb-hero-1), var(--qb-hero-2));
            box-shadow: 0 26px 70px var(--qb-shadow);
        }}
        .qb-hero::after {{
            content: "";
            position: absolute;
            width: 280px;
            height: 280px;
            border: 1px solid rgba(255,255,255,.08);
            border-radius: 50%;
            right: -80px;
            top: -120px;
        }}
        .qb-kicker {{
            width: max-content;
            padding: 7px 11px;
            margin-bottom: 17px;
            border-radius: 999px;
            background: rgba(255,255,255,.12);
            border: 1px solid rgba(255,255,255,.14);
            font-size: 11px !important;
            font-weight: 800;
            letter-spacing: 1px;
            text-transform: uppercase;
        }}
        .qb-hero h1 {{
            max-width: 720px;
            margin: 0 0 13px;
            font-size: clamp(34px, 4vw, 54px);
            line-height: 1.02;
            letter-spacing: -2px;
            color: #fff !important;
        }}
        .qb-hero p {{
            max-width: 760px;
            margin: 0;
            color: rgba(255,255,255,.76) !important;
            font-size: 16px;
            line-height: 1.65;
        }}

        .qb-steps {{
            display: grid;
            grid-template-columns: repeat(4, minmax(0,1fr));
            gap: 12px;
            margin: 20px 0 28px;
        }}
        .qb-step {{
            min-height: 124px;
            padding: 18px;
            border-radius: 16px;
            border: 1px solid var(--qb-border);
            background: var(--qb-surface);
            box-shadow: 0 10px 30px var(--qb-shadow);
        }}
        .qb-step-num {{
            width: 27px;
            height: 27px;
            display: grid;
            place-items: center;
            margin-bottom: 15px;
            border-radius: 8px;
            color: #fff;
            background: var(--qb-accent);
            font-size: 12px;
            font-weight: 900;
        }}
        .qb-step strong {{
            display: block;
            margin-bottom: 5px;
            color: var(--qb-text);
            font-size: 15px;
        }}
        .qb-step span {{
            color: var(--qb-muted);
            font-size: 13px;
            line-height: 1.5;
        }}

        .qb-section-title {{
            margin: 32px 0 7px;
            color: var(--qb-text);
            font-size: 25px;
            font-weight: 900;
            letter-spacing: -0.6px;
        }}
        .qb-section-sub {{
            margin-bottom: 17px;
            color: var(--qb-muted);
            font-size: 14px;
        }}

        .qb-panel {{
            padding: 22px;
            border: 1px solid var(--qb-border);
            border-radius: 18px;
            background: var(--qb-surface);
            box-shadow: 0 12px 34px var(--qb-shadow);
        }}

        .qb-email-card {{
            margin: 19px 0 22px;
            padding: 24px;
            border: 1px solid var(--qb-accent);
            border-radius: 18px;
            text-align: center;
            background: var(--qb-soft-blue);
            box-shadow: 0 12px 36px var(--qb-shadow);
        }}
        .qb-email-label {{
            margin-bottom: 8px;
            color: var(--qb-muted);
            font-size: 11px;
            font-weight: 800;
            letter-spacing: .9px;
            text-transform: uppercase;
        }}
        .qb-email-address {{
            direction: ltr;
            color: var(--qb-text);
            font-size: clamp(22px, 3vw, 32px);
            line-height: 1.3;
            font-weight: 900;
            letter-spacing: -.6px;
            word-break: break-word;
        }}

        .qb-platform-card {{
            min-height: 168px;
            padding: 22px;
            border: 1px solid var(--qb-border);
            border-radius: 18px;
            background: var(--qb-surface);
            box-shadow: 0 10px 30px var(--qb-shadow);
        }}
        .qb-platform-card.instagram {{
            background:
                linear-gradient(145deg, rgba(236,72,153,.10), transparent 60%),
                var(--qb-surface);
        }}
        .qb-platform-card.facebook {{
            background:
                linear-gradient(145deg, rgba(59,130,246,.10), transparent 60%),
                var(--qb-surface);
        }}
        .qb-platform-icon {{
            width: 38px;
            height: 38px;
            display: grid;
            place-items: center;
            margin-bottom: 18px;
            border-radius: 11px;
            background: var(--qb-surface-3);
            font-size: 19px;
        }}
        .qb-platform-card strong {{
            display: block;
            margin-bottom: 7px;
            color: var(--qb-text);
            font-size: 18px;
        }}
        .qb-platform-card span {{
            color: var(--qb-muted);
            font-size: 13px;
            line-height: 1.55;
        }}

        .qb-info {{
            padding: 14px 16px;
            margin: 17px 0 6px;
            border: 1px solid rgba(124,131,255,.45);
            border-radius: 12px;
            color: var(--qb-text);
            background: var(--qb-soft-blue);
            font-size: 13px;
            line-height: 1.6;
        }}

        .qb-recent {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            gap: 14px;
            padding: 14px 16px;
            margin-bottom: 9px;
            border: 1px solid var(--qb-border);
            border-radius: 12px;
            background: var(--qb-surface);
        }}
        .qb-recent-address {{
            direction: ltr;
            color: var(--qb-text);
            font-size: 14px;
            font-weight: 800;
            word-break: break-all;
        }}
        .qb-recent-meta {{
            color: var(--qb-muted);
            font-size: 12px;
            white-space: nowrap;
        }}

        /* Inputs */
        div[data-testid="stTextInput"] input,
        div[data-testid="stSelectbox"] [data-baseweb="select"] > div {{
            min-height: 48px;
            border: 1px solid var(--qb-border) !important;
            border-radius: 11px !important;
            color: var(--qb-text) !important;
            background: var(--qb-input) !important;
            font-size: 15px !important;
        }}
        div[data-testid="stTextInput"] input::placeholder {{
            color: var(--qb-muted) !important;
        }}

        /* Buttons */
        div.stButton > button,
        [data-testid="stLinkButton"] a {{
            min-height: 48px;
            border-radius: 11px !important;
            font-size: 14px !important;
            font-weight: 800 !important;
            transition: transform .18s ease, box-shadow .18s ease, border-color .18s ease;
        }}
        div.stButton > button:hover,
        [data-testid="stLinkButton"] a:hover {{
            transform: translateY(-1px);
        }}
        div.stButton > button[kind="primary"] {{
            border-color: transparent !important;
            color: #fff !important;
            background: linear-gradient(135deg, var(--qb-accent), var(--qb-accent-2)) !important;
            box-shadow: 0 10px 24px rgba(91,97,246,.25);
        }}
        [data-testid="stLinkButton"] a {{
            border-color: var(--qb-border) !important;
            color: var(--qb-text) !important;
            background: var(--qb-surface-2) !important;
        }}

        /* Status boxes */
        [data-testid="stAlert"] {{
            border-radius: 12px !important;
            font-size: 14px !important;
        }}

        /* Expanders / inbox */
        [data-testid="stExpander"] {{
            margin-bottom: 10px;
            border: 1px solid var(--qb-border) !important;
            border-radius: 12px !important;
            background: var(--qb-surface) !important;
        }}
        [data-testid="stExpander"] summary {{
            min-height: 52px;
            font-size: 14px !important;
            font-weight: 800 !important;
            color: var(--qb-text) !important;
        }}
        textarea {{
            color: var(--qb-text) !important;
            background: var(--qb-input) !important;
            border-color: var(--qb-border) !important;
            font-size: 14px !important;
            line-height: 1.7 !important;
        }}

        /* Inbox message body: disabled/read-only should remain high-contrast. */
        textarea:disabled,
        textarea[disabled] {{
            opacity: 1 !important;
            color: var(--qb-text) !important;
            -webkit-text-fill-color: var(--qb-text) !important;
            background: var(--qb-surface-2) !important;
            border: 1px solid var(--qb-border) !important;
            cursor: text !important;
        }}

        [data-testid="stTextArea"] textarea:disabled {{
            opacity: 1 !important;
            color: var(--qb-text) !important;
            -webkit-text-fill-color: var(--qb-text) !important;
        }}

        [data-testid="stExpander"] [data-testid="stMarkdownContainer"] p,
        [data-testid="stExpander"] label {{
            color: var(--qb-text) !important;
            opacity: 1 !important;
        }}

        code {{
            font-size: 13px !important;
        }}

        hr {{
            border-color: var(--qb-border) !important;
        }}

        @media (max-width: 850px) {{
            .main .block-container,
            [data-testid="stMainBlockContainer"] {{
                padding-left: 16px;
                padding-right: 16px;
            }}
            .qb-hero {{
                min-height: auto;
                padding: 34px 26px;
                border-radius: 19px;
            }}
            .qb-hero h1 {{
                font-size: 38px;
                letter-spacing: -1.3px;
            }}
            .qb-steps {{
                grid-template-columns: repeat(2, minmax(0,1fr));
            }}
        }}

        @media (max-width: 560px) {{
            .qb-steps {{
                grid-template-columns: 1fr;
            }}
            .qb-hero h1 {{
                font-size: 32px;
            }}
            .qb-email-address {{
                font-size: 21px;
            }}
            .qb-recent {{
                align-items: flex-start;
                flex-direction: column;
            }}
        }}
        </style>
        """,
        unsafe_allow_html=True,
    )


# -----------------------------------------------------------------------------
# Hidden Super Admin route: https://your-domain/?admin=1
# -----------------------------------------------------------------------------
if admin_route_requested():
    apply_theme(st.session_state.dark_mode)
    admin_theme_col, _ = st.columns([2, 8])
    with admin_theme_col:
        admin_dark = st.toggle("Dark mode", value=st.session_state.dark_mode, key="admin_dark_toggle")
        if admin_dark != st.session_state.dark_mode:
            st.session_state.dark_mode = admin_dark
            st.rerun()
    render_super_admin()
    st.stop()


# -----------------------------------------------------------------------------
# Header + theme switch
# -----------------------------------------------------------------------------
header_left, header_right = st.columns([8.7, 1.3], vertical_alignment="center")

with header_left:
    st.markdown(
        """
        <div class="qb-brand-row">
            <div>
                <div class="qb-brand">Quick<span>Inbox</span></div>
                <div class="qb-brand-sub">Private temporary inboxes for fast email verification.</div>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

with header_right:
    dark_toggle = st.toggle("Dark mode", value=st.session_state.dark_mode)
    if dark_toggle != st.session_state.dark_mode:
        st.session_state.dark_mode = dark_toggle
        st.rerun()

apply_theme(st.session_state.dark_mode)


# -----------------------------------------------------------------------------
# Hero / steps
# -----------------------------------------------------------------------------
st.markdown(
    """
    <section class="qb-hero">
        <div>
            <div class="qb-kicker">Real email inbox</div>
            <h1>Create your inbox.<br>Continue your signup.</h1>
            <p>
                Create an email address on your domain, use it during registration,
                then read incoming messages directly inside QuickInbox.
            </p>
        </div>
    </section>

    <div class="qb-steps">
        <div class="qb-step">
            <div class="qb-step-num">1</div>
            <strong>Create Email</strong>
            <span>Choose the email address you want.</span>
        </div>
        <div class="qb-step">
            <div class="qb-step-num">2</div>
            <strong>Choose Platform</strong>
            <span>Open the official registration page.</span>
        </div>
        <div class="qb-step">
            <div class="qb-step-num">3</div>
            <strong>Complete Registration</strong>
            <span>Finish the signup manually on the official website.</span>
        </div>
        <div class="qb-step">
            <div class="qb-step-num">4</div>
            <strong>Receive Email</strong>
            <span>Return here and refresh your inbox.</span>
        </div>
    </div>
    """,
    unsafe_allow_html=True,
)


# -----------------------------------------------------------------------------
# Create inbox
# -----------------------------------------------------------------------------
st.markdown('<div class="qb-section-title">Create an inbox</div>', unsafe_allow_html=True)
st.markdown(
    '<div class="qb-section-sub">Pick a username, choose how long to reserve it, and create the address.</div>',
    unsafe_allow_html=True,
)

username_col, domain_col = st.columns([2.7, 1.3], gap="medium")

with username_col:
    username = st.text_input(
        "Inbox name",
        placeholder="e.g. test123",
        help="Letters, numbers, dots, underscores and hyphens are allowed.",
        label_visibility="collapsed",
    )

with domain_col:
    st.text_input(
        "Domain",
        value=DOMAIN,
        disabled=True,
        label_visibility="collapsed",
    )

expiry_col, create_col = st.columns([2.7, 1.3], gap="medium", vertical_alignment="bottom")

with expiry_col:
    expiry = st.selectbox(
        "Reservation duration",
        [1, 6, 12, 24],
        index=3,
        format_func=lambda hours: f"{hours} hour" if hours == 1 else f"{hours} hours",
        label_visibility="collapsed",
    )

with create_col:
    create_clicked = st.button("Create Email", type="primary", use_container_width=True)

if DOMAIN == "yourdomain.com":
    st.warning("Set MAIL_DOMAIN in your .env file before using this app in production.")

if create_clicked:
    address, error = reserve_inbox(username, expiry)
    if error:
        st.error(error)
    else:
        st.session_state.active_email = address
        st.success("Inbox created successfully.")


# -----------------------------------------------------------------------------
# Active inbox / platform flow
# -----------------------------------------------------------------------------
if st.session_state.active_email:
    address = st.session_state.active_email

    st.markdown(
        f"""
        <div class="qb-email-card">
            <div class="qb-email-label">Your active email</div>
            <div class="qb-email-address">{html.escape(address)}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    st.code(address, language=None)

    st.markdown('<div class="qb-section-title">Continue your registration</div>', unsafe_allow_html=True)
    st.markdown(
        '<div class="qb-section-sub">Open an official platform page, finish the signup yourself, then come back here for the email.</div>',
        unsafe_allow_html=True,
    )

    instagram_col, facebook_col = st.columns(2, gap="medium")

    with instagram_col:
        st.markdown(
            """
            <div class="qb-platform-card instagram">
                <div class="qb-platform-icon">◎</div>
                <strong>Instagram</strong>
                <span>Open Instagram's official registration page and continue using your QuickInbox address.</span>
            </div>
            """,
            unsafe_allow_html=True,
        )
        st.link_button("Continue to Instagram ↗", INSTAGRAM_SIGNUP, use_container_width=True)

    with facebook_col:
        st.markdown(
            """
            <div class="qb-platform-card facebook">
                <div class="qb-platform-icon">f</div>
                <strong>Facebook</strong>
                <span>Open Facebook's official signup page and continue using your QuickInbox address.</span>
            </div>
            """,
            unsafe_allow_html=True,
        )
        st.link_button("Continue to Facebook ↗", FACEBOOK_SIGNUP, use_container_width=True)

    st.markdown(
        """
        <div class="qb-info">
            <strong>Next step:</strong> Complete registration on the platform website.
            QuickInbox does not create accounts or complete signups on your behalf; it only creates your inbox address and displays incoming email.
        </div>
        """,
        unsafe_allow_html=True,
    )

    st.markdown('<div class="qb-section-title">Inbox</div>', unsafe_allow_html=True)
    inbox_title_col, refresh_col = st.columns([4.4, 1.2], vertical_alignment="bottom")

    with inbox_title_col:
        st.markdown(
            '<div class="qb-section-sub">Incoming messages sent to your generated address will appear here.</div>',
            unsafe_allow_html=True,
        )

    with refresh_col:
        refresh_clicked = st.button("↻ Refresh inbox", use_container_width=True, key="refresh_inbox")
        if refresh_clicked:
            log_event("inbox_refresh", address)

    messages, error = fetch_messages_for(address)

    if error:
        st.info(error)
    elif not messages:
        st.info("No messages have arrived for this address yet.")
    else:
        for index, msg in enumerate(messages, start=1):
            with st.expander(
                f"{index:02d}  ·  {msg['subject']}",
                expanded=(index == 1),
            ):
                metadata_col1, metadata_col2 = st.columns(2)
                with metadata_col1:
                    st.markdown(f"**From:** {html.escape(msg['from'])}")
                with metadata_col2:
                    st.markdown(f"**Date:** {html.escape(msg['date'])}")

                st.markdown(f"**To:** `{html.escape(msg['to'])}`")
                st.text_area(
                    "Message",
                    value=msg["body"] or "(Empty message body)",
                    height=230,
                    key=f"message_body_{index}",
                    disabled=True,
                )


st.markdown(
    '<div style="height:22px"></div><div class="qb-section-sub">QuickInbox stores reservation history and operational metadata for Super Admin analytics. Message bodies are not stored in analytics.</div>',
    unsafe_allow_html=True,
)
