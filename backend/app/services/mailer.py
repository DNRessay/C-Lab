import logging
import smtplib
from email.message import EmailMessage

from ..config import settings

log = logging.getLogger(__name__)


def send_mail(to, subject, body):
    if not settings.email_host:
        log.warning("EMAIL_HOST not set — email to %s not sent:\n%s\n%s", to, subject, body)
        return
    msg = EmailMessage()
    msg["From"] = settings.default_from_email
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    with smtplib.SMTP(settings.email_host, settings.email_port, timeout=20) as smtp:
        if settings.email_use_tls:
            smtp.starttls()
        if settings.email_user:
            smtp.login(settings.email_user, settings.email_password)
        smtp.send_message(msg)
