"""Operator notification (stage 13).

* Alarm: an audible signal is raised client-side; the service emits an
  ``alarm`` change event that every open console receives.
* Notification: an e-mail-style message with identity, date and time.
* Every failure is logged and swallowed - recognition must never break
  because the mail server is down.
"""
from __future__ import annotations

import smtplib
import threading
import time
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Dict, List, Optional

from ..core.logging_setup import get_logger

log = get_logger("actions.notify")


@dataclass
class Notification:
    subject: str
    body: str
    recipient: str
    sent: bool = False
    error: str = ""
    at: float = 0.0

    def as_dict(self) -> Dict:
        return {
            "subject": self.subject,
            "body": self.body,
            "recipient": self.recipient,
            "sent": self.sent,
            "error": self.error,
            "at": self.at,
        }


class Notifier:
    """Sends operator notifications; always returns (never raises)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._history: List[Notification] = []

    def send_detection(
        self,
        settings,
        identity: str,
        when_text: str,
        camera: str,
        score: float = 0.0,
    ) -> Optional[Notification]:
        if not settings.alarm_enabled:
            return None
        subject = f"FACW detection: {identity}"
        body = (
            f"Identity: {identity}\n"
            f"Date/time: {when_text}\n"
            f"Camera: {camera}\n"
            f"Similarity: {score:.3f}\n"
        )
        note = Notification(subject=subject, body=body, recipient=settings.notify_recipient, at=time.time())
        try:
            if settings.notify_method == "smtp" and settings.smtp_host:
                self._send_smtp(settings, note)
            else:
                # "log" transport: the notification is recorded and emitted to
                # the console; this is the offline default.
                log.info(
                    "notification",
                    extra={"to": note.recipient, "identity": identity, "when": when_text},
                )
                note.sent = True
        except Exception as exc:  # noqa: BLE001 - must never break recognition
            note.sent = False
            note.error = str(exc)
            log.warning("notification failed", extra={"error": str(exc), "to": note.recipient})
        with self._lock:
            self._history.append(note)
            if len(self._history) > 200:
                del self._history[: len(self._history) - 200]
        return note

    def _send_smtp(self, settings, note: Notification) -> None:
        msg = EmailMessage()
        msg["Subject"] = note.subject
        msg["From"] = settings.smtp_host and f"facw@{settings.smtp_host}" or "facw@localhost"
        msg["To"] = note.recipient or "operator@example.local"
        msg.set_content(note.body)
        with smtplib.SMTP(settings.smtp_host, int(settings.smtp_port), timeout=5) as smtp:
            smtp.send_message(msg)
        note.sent = True

    def history(self, limit: int = 50) -> List[Dict]:
        with self._lock:
            return [n.as_dict() for n in self._history[-limit:]]
