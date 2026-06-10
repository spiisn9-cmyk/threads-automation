"""Email notification client (SMTP + STARTTLS)."""
from __future__ import annotations

import logging
import smtplib
from dataclasses import dataclass
from email.message import EmailMessage

logger = logging.getLogger(__name__)

SMTP_TIMEOUT = 30.0


@dataclass(frozen=True)
class SmtpConfig:
    host: str
    port: int
    user: str
    password: str
    mail_to: str


class NotifyClient:
    def __init__(self, config: SmtpConfig) -> None:
        self._config = config

    def notify(self, subject: str, body: str) -> None:
        """Send a single plain-text email to MAIL_TO."""
        cfg = self._config
        message = EmailMessage()
        message["Subject"] = subject
        message["From"] = cfg.user
        message["To"] = cfg.mail_to
        message.set_content(body)

        try:
            with smtplib.SMTP(cfg.host, cfg.port, timeout=SMTP_TIMEOUT) as server:
                server.starttls()
                server.login(cfg.user, cfg.password)
                server.send_message(message)
            logger.info("Email sent to %s (subject=%r)", cfg.mail_to, subject)
        except (smtplib.SMTPException, OSError) as exc:
            logger.error("Failed to send email: %s", exc)
            raise RuntimeError("Failed to send notification email") from exc
