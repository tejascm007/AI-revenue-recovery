"""Thin wrapper around Meta's WhatsApp Cloud API (Graph API).

Design reference: Design_Spec_and_Decisions.md, section 11, Problem 8.
Confirmed during research: no official Meta Python SDK exists — direct Graph
API calls via HTTP is the standard, documented path. No `notify.whatsapp`
parameter exists on Razorpay's Payment Links either, so this client is the
entire outbound/inbound WhatsApp surface for the whole project, not just
Problem 8.
"""

import hashlib
import hmac
import os

import httpx

META_WA_ACCESS_TOKEN = os.environ.get("META_WA_ACCESS_TOKEN", "")
META_WA_PHONE_NUMBER_ID = os.environ.get("META_WA_PHONE_NUMBER_ID", "")
META_WA_APP_SECRET = os.environ.get("META_WA_APP_SECRET", "")
GRAPH_API_VERSION = "v21.0"


def _require_credentials() -> None:
    if not META_WA_ACCESS_TOKEN or not META_WA_PHONE_NUMBER_ID:
        raise RuntimeError(
            "META_WA_ACCESS_TOKEN / META_WA_PHONE_NUMBER_ID are not set. "
            "The Meta WhatsApp Cloud API account is still an open item "
            "(Design_Spec_and_Decisions.md section 10) - set these env vars "
            "once a Meta Developer app + WABA exist."
        )


def _post(payload: dict) -> dict:
    _require_credentials()
    url = f"https://graph.facebook.com/{GRAPH_API_VERSION}/{META_WA_PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {META_WA_ACCESS_TOKEN}"}
    response = httpx.post(url, json=payload, headers=headers, timeout=10.0)
    response.raise_for_status()
    return response.json()


def send_template_message(to_phone: str, template_name: str, language_code: str = "en",
                           body_parameters: list[str] | None = None) -> dict:
    """Business-initiated send, outside any Customer Service Window - MUST use
    a pre-approved template (confirmed: there is no way to send free-form text
    proactively). All templates in this project are "utility" category, per
    the design's decision to never use marketing-category templates."""
    components = []
    if body_parameters:
        components.append({
            "type": "body",
            "parameters": [{"type": "text", "text": p} for p in body_parameters],
        })
    payload = {
        "messaging_product": "whatsapp",
        "to": to_phone,
        "type": "template",
        "template": {
            "name": template_name,
            "language": {"code": language_code},
            "components": components,
        },
    }
    return _post(payload)


def send_text_message(to_phone: str, text: str) -> dict:
    """Free-form send - only valid inside an open 24h Customer Service Window
    (the customer messaged us, or replied to a template, within the last 24h).
    Callers must check check_csw_status before calling this - this client
    does not enforce the window itself, that's server.py's job."""
    payload = {
        "messaging_product": "whatsapp",
        "to": to_phone,
        "type": "text",
        "text": {"body": text},
    }
    return _post(payload)


def verify_webhook_signature(raw_body: bytes, signature_header: str) -> bool:
    """Meta signs webhook payloads as X-Hub-Signature-256: sha256=<hex digest>,
    HMAC-SHA256 over the raw body using the app secret - same "raw bytes, not
    re-serialized JSON" requirement as Razorpay's webhook verification."""
    if not META_WA_APP_SECRET:
        raise RuntimeError("META_WA_APP_SECRET is not set.")
    expected = hmac.new(META_WA_APP_SECRET.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    provided = signature_header.removeprefix("sha256=")
    return hmac.compare_digest(expected, provided)
