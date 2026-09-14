import os
import re
import sqlite3
import imaplib
import email
import html
from email.header import decode_header
from datetime import datetime, timedelta, timezone
from pathlib import Path

import streamlit as st
from dotenv import load_dotenv

load_dotenv()

APP_NAME = "QuickInbox"
DB_FILE = Path(os.getenv("DB_FILE", "platform.db"))

DOMAIN = os.getenv("MAIL_DOMAIN", "yourdomain.com").strip().lower()
IMAP_HOST = os.getenv("IMAP_HOST", "").strip()
IMAP_PORT = int(os.getenv("IMAP_PORT", "993"))
IMAP_USER = os.getenv("IMAP_USER", "").strip()
IMAP_PASSWORD = os.getenv("IMAP_PASSWORD", "")
INBOX_FOLDER = os.getenv("IMAP_FOLDER", "INBOX").strip() or "INBOX"

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


# -----------------------------------------------------------------------------
# Database
# -----------------------------------------------------------------------------
def db_connection():
    conn = sqlite3.connect(DB_FILE, check_same_thread=False)
    conn.execute(
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
    conn.commit()
    return conn


conn = db_connection()


def utc_now():
    return datetime.now(timezone.utc)


def clean_expired_inboxes():
    conn.execute("DELETE FROM inboxes WHERE expires_at <= ?", (utc_now().isoformat(),))
    conn.commit()


def clean_username(value: str) -> str:
    value = (value or "").strip().lower()
    value = re.sub(r"[^a-z0-9._-]", "", value)
    value = value.strip("._-")
    return value[:40]


def reserve_inbox(username: str, hours: int = 24):
    clean_expired_inboxes()
    username = clean_username(username)

    if len(username) < 3:
        return None, "Use at least 3 letters or numbers for the inbox name."

    address = f"{username}@{DOMAIN}"
    now = utc_now()
    expires_at = now + timedelta(hours=hours)

    try:
        conn.execute(
            "INSERT INTO inboxes(username, email_address, created_at, expires_at) VALUES(?,?,?,?)",
            (username, address, now.isoformat(), expires_at.isoformat()),
        )
        conn.commit()
        return address, None
    except sqlite3.IntegrityError:
        return None, "That inbox name is already reserved. Try another one."



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
        return [], "IMAP authentication failed. Check IMAP_USER and IMAP_PASSWORD in your .env file."
    except Exception as exc:
        return [], f"Mailbox connection error: {exc}"
    finally:
        if mail is not None:
            try:
                mail.logout()
            except Exception:
                pass


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
        st.button("↻ Refresh inbox", use_container_width=True, key="refresh_inbox")

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
    '<div style="height:22px"></div>'
    '<div class="qb-section-sub">'
    'QuickInbox creates temporary inbox addresses on your own domain and displays incoming email. '
    'Keep your server environment variables private.'
    '</div>',
    unsafe_allow_html=True,
)
