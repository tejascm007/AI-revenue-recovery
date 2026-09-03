"""Minimal SMTP client for internal escalation notifications.

Design reference: Design_Spec_and_Decisions.md, section 11, Problem 9 gap
fix — ESCALATE_TO_PROCUREMENT/FINANCE notify the merchant's OWN internal
contacts, explicitly NOT via the Meta WhatsApp channel (that's reserved for
customer-facing communication). No email provider is configured yet — same
"clean error at point of use, not at import" pattern as every other external
client in this codebase, not a full provider integration (SendGrid/SES/etc.
can replace this later without changing server.py's call site).
"""

import os
import smtplib
from email.message import EmailMessage

import rzp_common.env  # noqa: F401  (side-effect import: loads codes/.env)

SMTP_HOST = os.environ.get("SMTP_HOST", "")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
SMTP_FROM_ADDRESS = os.environ.get("SMTP_FROM_ADDRESS", "")


def send_email(to_address: str, subject: str, body: str) -> None:
    if not SMTP_HOST or not SMTP_USER or not SMTP_PASSWORD:
        raise RuntimeError(
            "SMTP_HOST / SMTP_USER / SMTP_PASSWORD are not set. Internal "
            "escalation email is still an open item - set these env vars "
            "once a transactional email provider is configured."
        )
    message = EmailMessage()
    message["From"] = SMTP_FROM_ADDRESS or SMTP_USER
    message["To"] = to_address
    message["Subject"] = subject
    message.set_content(body)

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
        server.starttls()
        server.login(SMTP_USER, SMTP_PASSWORD)
        server.send_message(message)
