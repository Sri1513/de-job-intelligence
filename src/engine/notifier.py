# src/engine/notifier.py
"""Outbound alerts for pipeline failures — WhatsApp to Omkar's phone.

Sends via the Meta WhatsApp Business Cloud API (official, free tier covers
1,000 service conversations/month). Configuration is environment-based —
secrets never live in files:

    WHATSAPP_ENABLED=true
    WHATSAPP_PHONE_NUMBER_ID=<from Meta developer dashboard>
    WHATSAPP_ACCESS_TOKEN=<system user / temporary token>
    WHATSAPP_RECIPIENT=<Omkar's phone in international format, e.g. 19515452146>

When not configured (or when sending fails), alerts degrade gracefully to a
loud WARNING log line — nothing is ever silently dropped, and the existing
dashboard logs remain the source of truth for diagnosis.
"""
import logging
import os

import requests

logger = logging.getLogger(__name__)

_GRAPH_VERSION = "v21.0"


def _env(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def whatsapp_configured() -> bool:
    """True when all WhatsApp Cloud API settings are present and enabled."""
    return (
        _env("WHATSAPP_ENABLED").lower() == "true"
        and bool(_env("WHATSAPP_PHONE_NUMBER_ID"))
        and bool(_env("WHATSAPP_ACCESS_TOKEN"))
        and bool(_env("WHATSAPP_RECIPIENT"))
    )


def send_whatsapp(message: str) -> bool:
    """Sends a WhatsApp text message. Returns True on success.

    Falls back to a WARNING log when WhatsApp is not configured — the alert
    is still visible in the service logs exposed by the admin API.
    """
    prefix = "🚨 de-job-intelligence"
    text = f"{prefix}\n{message}"
    if not whatsapp_configured():
        logger.warning("notifier: WhatsApp not configured; alert logged instead:\n%s", text)
        return False
    phone_number_id = _env("WHATSAPP_PHONE_NUMBER_ID")
    url = f"https://graph.facebook.com/{_GRAPH_VERSION}/{phone_number_id}/messages"
    try:
        resp = requests.post(
            url,
            headers={
                "Authorization": f"Bearer {_env('WHATSAPP_ACCESS_TOKEN')}",
                "Content-Type": "application/json",
            },
            json={
                "messaging_product": "whatsapp",
                "to": _env("WHATSAPP_RECIPIENT"),
                "type": "text",
                "text": {"body": text[:4000]},
            },
            timeout=20,
        )
        resp.raise_for_status()
        logger.info("notifier: WhatsApp alert sent (%d chars)", len(text))
        return True
    except Exception as exc:  # noqa: BLE001 - alerting must never crash the pipeline
        logger.warning("notifier: WhatsApp send failed (%s); alert logged instead:\n%s", exc, text)
        return False


# -- Convenience alerts ----------------------------------------------------

def alert_llm_failover(task: str, provider: str, reason: str, next_provider: str) -> None:
    """Fires when a provider is sidelined (quota/billing) and traffic moves on."""
    send_whatsapp(
        f"⚠️ LLM failover ({task}): {provider} sidelined — {reason}. "
        f"Now serving via {next_provider}."
    )


def alert_llm_all_down(task: str, last_error: str) -> None:
    """Fires when every provider in the chain is exhausted/failed."""
    send_whatsapp(
        f"🛑 LLM chain exhausted ({task}): all providers down. "
        f"Last error: {last_error[:300]}. Pipeline used fallback behavior; "
        f"check logs."
    )


def alert_paid_engaged(task: str, provider: str) -> None:
    """Fires when a paid-tier provider engages — usage WILL incur charges."""
    send_whatsapp(
        f"💰 Paid LLM engaged ({task}): now serving via {provider}. "
        f"Usage will incur charges."
    )
