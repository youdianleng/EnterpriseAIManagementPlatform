"""Outbound mail, behind the one seam this system allows in a test.

`docs/DESIGN.md` §7.1 puts a summary mail in every manager's morning, and mail is
the one thing this codebase cannot exercise for real: a test that opened an SMTP
connection would need a mail server, would fail when it was down, and would be one
configuration mistake away from mailing actual people. So the transport is an
interface — `Mailer` — with two implementations that matter:

* `SmtpMailer` is the real one, configured entirely from settings.
* A recording double lives in the tests, and nothing else in the suite reaches a
  socket. The seam is *the transport only*: what to send, to whom, and what to
  write down afterwards are domain decisions and are tested against PostgreSQL.

**`enabled` is the switch, and it is a property of the mailer rather than a flag
threaded through the caller.** `MAIL_ENABLED=false` therefore means "this object
will not send", which is one thing to assert and impossible to half-apply; the
sender refuses even if it is called, so a code path that forgot to ask still
cannot open a connection. Development and demo run with it *on* and SMTP pointed
at Mailpit, which accepts everything and delivers nothing anywhere.

**Two parts, always.** Every message is `multipart/alternative` with the plain
text first and the HTML second, which is the order a client reads them in and the
reason a text-only reader sees a complete message rather than an empty one. Both
parts are composed by the caller: this module knows about envelopes and ports, not
about anomalies.

`mail.send` is synchronous `smtplib` and is run in a worker thread, because the
alternative is a blocked event loop for the duration of a network round trip —
and because `smtplib` is the standard library, which keeps a mail dependency out
of a project that needs exactly one kind of message.
"""

import asyncio
import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formataddr, formatdate
from typing import Protocol

from app.config import Settings


class MailError(RuntimeError):
    """A message that did not go out, carrying the reason the sender gave.

    The reason is written onto the delivery row verbatim: "why did this fail" is
    a question about *this* relay at *this* moment, and a tidied-up summary loses
    the one detail — refused, timed out, bad credentials — that says what to fix.
    """


@dataclass(slots=True, frozen=True)
class MailMessage:
    """One message, composed and ready. No addresses derived here, ever."""

    to: str
    subject: str
    text: str
    html: str


class Mailer(Protocol):
    """What the rest of the system knows about sending mail.

    Two members, and the first is not a formality: a caller asks before it
    composes anything, so "mail is off" costs no work and leaves no half-written
    attempt behind.
    """

    @property
    def enabled(self) -> bool: ...

    async def send(self, message: MailMessage) -> None:
        """Send it, or raise `MailError` with the sender's own reason."""
        ...


def build_envelope(message: MailMessage, sender: str) -> EmailMessage:
    """One `multipart/alternative` message: text, then HTML, in that order.

    A public function because the envelope is the part a test can assert without
    a server: the two parts exist, both carry the same content, and the text one
    is not markup.
    """
    envelope = EmailMessage()
    envelope["From"] = formataddr(("Enterprise AI Management Platform", sender))
    envelope["To"] = message.to
    envelope["Subject"] = message.subject
    envelope["Date"] = formatdate(localtime=True)
    envelope.set_content(message.text)
    envelope.add_alternative(message.html, subtype="html")
    return envelope


class SmtpMailer:
    """SMTP, configured from settings and refusing to guess at anything.

    The connection is opened per message rather than held open: a digest run
    sends a handful of messages once a day, and a long-lived connection is a
    state machine to keep alive for no gain. `starttls` is off by default because
    the sibling container speaks plain SMTP on a private network; a real relay is
    configured with it on.
    """

    def __init__(
        self,
        *,
        enabled: bool,
        host: str,
        port: int,
        sender: str,
        username: str | None = None,
        password: str | None = None,
        starttls: bool = False,
        timeout: int = 10,
    ) -> None:
        self._enabled = enabled
        self._host = host
        self._port = port
        self._sender = sender
        self._username = username
        self._password = password
        self._starttls = starttls
        self._timeout = timeout

    @property
    def enabled(self) -> bool:
        return self._enabled

    async def send(self, message: MailMessage) -> None:
        if not self._enabled:
            raise MailError("mail is disabled (MAIL_ENABLED=false)")
        if bool(self._username) != bool(self._password):
            raise MailError("SMTP_USER and SMTP_PASSWORD must be set together")
        await asyncio.to_thread(self._send, message)

    def _send(self, message: MailMessage) -> None:
        envelope = build_envelope(message, self._sender)
        try:
            with smtplib.SMTP(self._host, self._port, timeout=self._timeout) as client:
                if self._starttls:
                    client.starttls(context=ssl.create_default_context())
                if self._username and self._password:
                    client.login(self._username, self._password)
                client.send_message(envelope)
        except (OSError, smtplib.SMTPException) as error:
            # One exception type for the caller: whether the socket refused or the
            # relay refused, the delivery row records a reason either way.
            raise MailError(f"{type(error).__name__}: {error}") from error


def build_mailer(settings: Settings) -> Mailer:
    """The configured sender. The only place settings become a transport."""
    return SmtpMailer(
        enabled=settings.mail_enabled,
        host=settings.smtp_host,
        port=settings.smtp_port,
        sender=settings.smtp_from,
        username=settings.smtp_user,
        password=settings.smtp_password,
        starttls=settings.smtp_starttls,
        timeout=settings.smtp_timeout_seconds,
    )


__all__ = [
    "MailError",
    "MailMessage",
    "Mailer",
    "SmtpMailer",
    "build_envelope",
    "build_mailer",
]
