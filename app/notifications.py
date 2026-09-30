"""Local alert receipt, with optional explicit Telegram forwarding."""
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Literal

import httpx
from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, Field

from app.ops import require_ops

app = FastAPI(title="Bonus 2 local alert receiver")
STORE = Path(os.getenv("NOTIFICATION_STORE", "runtime/notifications.jsonl"))
STORE.parent.mkdir(parents=True, exist_ok=True)
lock = Lock()


class Message(BaseModel):
    text: str = Field(min_length=1, max_length=3500)


class Alert(BaseModel):
    status: Literal["firing", "resolved"]
    labels: dict[str, str]


class AlertBatch(BaseModel):
    alerts: list[Alert] = Field(max_length=100)


def record(text, source, status):
    mode = "local"
    if os.getenv("TELEGRAM_ENABLED", "0") == "1":
        token, chat = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
        if not token or not chat:
            raise HTTPException(status_code=503, detail="Telegram credentials are not configured")
        try:
            response = httpx.post(f"https://api.telegram.org/bot{token}/sendMessage",
                                  json={"chat_id": chat, "text": text}, timeout=10)
            response.raise_for_status()
            if not response.json().get("ok"):
                raise ValueError("Telegram rejected the message")
        except (httpx.HTTPError, ValueError):
            # Do not log the request URL: it contains the bot token.
            raise HTTPException(status_code=502, detail="Telegram delivery failed") from None
        mode = "telegram"
    entry = {"timestamp": datetime.now(timezone.utc).isoformat(), "source": source,
             "status": status, "text": text, "delivery": mode}
    with lock:
        with STORE.open("a", encoding="utf-8") as out:
            out.write(json.dumps(entry) + "\n")
    return entry


@app.get("/health")
def health():
    return {"status": "ok", "telegram_enabled": os.getenv("TELEGRAM_ENABLED", "0") == "1"}


@app.post("/alerts")
def alerts(payload: AlertBatch):
    entries = []
    for alert in payload.alerts:
        status = alert.status
        text = f"[{status.upper()}] {alert.labels.get('alertname', 'alert')}"
        entries.append(record(text, "alertmanager", status))
    return {"received": len(entries)}


@app.post("/notify", dependencies=[Depends(require_ops)])
def notify(message: Message):
    return record(message.text, "airflow", "event")


@app.get("/events", dependencies=[Depends(require_ops)])
def events():
    with lock:
        lines = STORE.read_text().splitlines() if STORE.exists() else []
    return {"events": [json.loads(line) for line in lines[-100:]]}
