import os
import smtplib
import imaplib
import logging
import time
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

logger = logging.getLogger(__name__)

SMTP_HOST = os.getenv("EMAIL_SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("EMAIL_SMTP_PORT", "587"))
EMAIL_USER = os.getenv("EMAIL_USER", "")
EMAIL_PASSWORD = os.getenv("EMAIL_PASSWORD", "")
NOTIFICATION_EMAIL = os.getenv("NOTIFICATION_EMAIL", "")


def _format_phone(phone: str) -> str:
    digits = "".join(ch for ch in phone if ch.isdigit())
    local = digits[3:] if digits.startswith("375") else digits
    local = local[:9]
    if len(local) != 9:
        return phone
    return f"+375 ({local[:2]}) {local[2:5]}-{local[5:7]}-{local[7:9]}"


def _get_request_type(message: str) -> str:
    if message == "Заявка репетитора":
        return "репетитор"
    return "ученик"


def _append_notification_to_inbox(msg: MIMEMultipart) -> bool:
    if EMAIL_USER.lower() != NOTIFICATION_EMAIL.lower():
        return False

    try:
        with imaplib.IMAP4_SSL("imap.gmail.com", 993) as mail:
            mail.login(EMAIL_USER, EMAIL_PASSWORD)
            status, _ = mail.append(
                "INBOX",
                None,
                imaplib.Time2Internaldate(time.time()),
                msg.as_bytes(),
            )
        if status == "OK":
            logger.info("Notification appended to %s inbox via IMAP fallback", NOTIFICATION_EMAIL)
            return True
        logger.error("IMAP fallback failed with status: %s", status)
    except Exception as e:
        logger.error("IMAP fallback failed: %s", e)
    return False


def send_lead_notification(name: str, phone: str, subject_name: str = "", message: str = "") -> bool:
    if not EMAIL_USER or not EMAIL_PASSWORD or not NOTIFICATION_EMAIL:
        logger.warning("Email notification not configured - skipping")
        return False

    request_type = _get_request_type(message)
    details = subject_name or ""
    if message and message not in {"Заявка ученика", "Заявка репетитора"}:
        details = f"{details}, {message}" if details else message

    body = f"""📩 Новая заявка с сайта!

🧑‍🏫 Тип заявки: {request_type}
👤 Имя: {name}
📞 Телефон: {_format_phone(phone)}
🧾 Предмет и класс: {details or "не указано"}
"""

    msg = MIMEMultipart()
    msg["From"] = f"pifagor.by <{EMAIL_USER}>"
    msg["To"] = NOTIFICATION_EMAIL
    msg["Subject"] = "Заявка с сайта pifagor.by"
    msg.attach(MIMEText(body, "plain", "utf-8"))

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as server:
            server.starttls()
            server.login(EMAIL_USER, EMAIL_PASSWORD)
            server.send_message(msg)
        logger.info("Notification sent to %s about lead from %s", NOTIFICATION_EMAIL, name)
        return True
    except Exception as e:
        logger.error("Failed to send notification email: %s", e)
        return _append_notification_to_inbox(msg)

def send_password_reset_code(to_email: str, code: str) -> bool:
    """Отправляет код подтверждения для восстановления пароля личного
    кабинета. В отличие от заявок с сайта, здесь нет резервного варианта
    через IMAP — если письмо не ушло, пользователь должен об этом узнать
    (возвращаем False), а не тихо считать, что всё получилось."""
    if not EMAIL_USER or not EMAIL_PASSWORD:
        logger.warning("Email not configured - cannot send password reset code")
        return False

    body = f"""Код для восстановления пароля личного кабинета Пифагор: {code}

Никому не сообщайте этот код. Он действителен 15 минут.
Если вы не запрашивали восстановление пароля — просто проигнорируйте это письмо.
"""

    msg = MIMEMultipart()
    msg["From"] = f"pifagor.by <{EMAIL_USER}>"
    msg["To"] = to_email
    msg["Subject"] = "Код для восстановления пароля — Пифагор"
    msg.attach(MIMEText(body, "plain", "utf-8"))

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as server:
            server.starttls()
            server.login(EMAIL_USER, EMAIL_PASSWORD)
            server.send_message(msg)
        logger.info("Password reset code sent to %s", to_email)
        return True
    except Exception as e:
        logger.error("Failed to send password reset code: %s", e)
        return False

