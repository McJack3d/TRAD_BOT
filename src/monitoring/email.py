"""Email digest sender.

Daily and weekly summaries via SMTP (Gmail App Password by default).
"""

from __future__ import annotations

from email.message import EmailMessage

import aiosmtplib

from src.logging_setup import log


class EmailNotifier:
    def __init__(
        self,
        smtp_host: str,
        smtp_port: int,
        username: str,
        password: str,
        from_addr: str,
        to_addr: str,
    ):
        self.smtp_host = smtp_host
        self.smtp_port = smtp_port
        self.username = username
        self.password = password
        self.from_addr = from_addr or username
        self.to_addr = to_addr

    @property
    def enabled(self) -> bool:
        return bool(self.username and self.password and self.to_addr)

    async def send(self, subject: str, body: str) -> None:
        if not self.enabled:
            log.info("email.disabled.no_credentials", subject=subject)
            return
        msg = EmailMessage()
        msg["From"] = self.from_addr
        msg["To"] = self.to_addr
        msg["Subject"] = subject
        msg.set_content(body)
        try:
            await aiosmtplib.send(
                msg,
                hostname=self.smtp_host,
                port=self.smtp_port,
                username=self.username,
                password=self.password,
                start_tls=True,
            )
            log.info("email.sent", subject=subject)
        except Exception as e:
            log.warning("email.send.failed", error=str(e))
