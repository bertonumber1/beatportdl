from __future__ import annotations

import requests


def send_notification(webhook_url: str, title: str, message: str) -> bool:
    """Fires a plain HTTP POST at whatever's configured — a generic pipeline
    rather than a specific provider integration. The payload includes several
    common field names so it works out of the box with the most common
    receivers without per-provider config:
      - Discord incoming webhooks read "content"
      - Slack incoming webhooks read "text"
      - ntfy.sh / Gotify / most custom bots read "title" + "message"
    Silently returns False on any failure — a notification hook must never be
    allowed to break the download/watch pipeline it's reporting on."""
    if not webhook_url:
        return False
    body = f"{title}\n{message}" if message else title
    payload = {
        "title": title,
        "message": message,
        "content": body,
        "text": body,
    }
    try:
        resp = requests.post(webhook_url, json=payload, timeout=10)
        return resp.status_code < 300
    except requests.RequestException:
        return False
