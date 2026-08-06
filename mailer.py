import logging
import os
import smtplib
import ssl
from email.message import EmailMessage

logger = logging.getLogger(__name__)

SMTP_HOST = os.environ.get("POCKET_SMTP_HOST", "").strip()
SMTP_PORT = int(os.environ.get("POCKET_SMTP_PORT", "587"))
SMTP_USER = os.environ.get("POCKET_SMTP_USER", "")
SMTP_PASS = os.environ.get("POCKET_SMTP_PASS", "")
SMTP_FROM = os.environ.get("POCKET_SMTP_FROM", "").strip()
SMTP_STARTTLS = os.environ.get("POCKET_SMTP_STARTTLS", "1") == "1"
SMTP_SSL = os.environ.get("POCKET_SMTP_SSL", "0") == "1"


def smtp_configured() -> bool:
    return bool(SMTP_HOST and SMTP_FROM)


def send_email(to: str, subject: str, body_text: str, body_html: str | None = None) -> None:
    if not smtp_configured():
        raise RuntimeError("SMTP not configured: set POCKET_SMTP_HOST and POCKET_SMTP_FROM")
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = SMTP_FROM
    msg["To"] = to
    msg.set_content(body_text)
    if body_html:
        msg.add_alternative(body_html, subtype="html")

    context = ssl.create_default_context()
    if SMTP_SSL:
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=context, timeout=30) as srv:
            _login(srv)
            srv.send_message(msg)
    else:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as srv:
            if SMTP_STARTTLS:
                srv.starttls(context=context)
            _login(srv)
            srv.send_message(msg)
    logger.info("email sent to %s (subject=%r)", to, subject)


def _login(srv: smtplib.SMTP) -> None:
    if SMTP_USER:
        srv.login(SMTP_USER, SMTP_PASS)


def send_activation_email(to: str, activation_url: str, username: str) -> None:
    subject = "Conferma la tua registrazione - Pocket Log Analyzer"
    body_text = (
        f"Ciao {username},\n\n"
        "conferma la tua registrazione a Pocket Log Analyzer cliccando su questo link:\n\n"
        f"{activation_url}\n\n"
        "Il link scade tra 24 ore. Se non hai richiesto la registrazione, ignora questa email.\n"
    )
    body_html = (
        "<p>Ciao <strong>%s</strong>,</p>"
        "<p>conferma la tua registrazione a Pocket Log Analyzer cliccando su questo link:</p>"
        '<p><a href="%s">Conferma la registrazione</a></p>'
        "<p>Il link scade tra 24 ore. Se non hai richiesto la registrazione, ignora questa email.</p>"
    ) % (username, activation_url)
    send_email(to, subject, body_text, body_html)
