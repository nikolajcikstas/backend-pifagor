
"""
EasyPay email receipt parser.
"""
import imaplib
import email
import os
import re
import logging
import socket
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import List, Dict, Optional

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, cast, Date

logger = logging.getLogger(__name__)

IMAP_HOST = os.getenv("EMAIL_IMAP_HOST", "imap.gmail.com")
IMAP_PORT = int(os.getenv("EMAIL_IMAP_PORT", "993"))
EMAIL_USER = os.getenv("EMAIL_USER", "")
EMAIL_PASSWORD = os.getenv("EMAIL_PASSWORD", "")
LESSON_PRICE = float(os.getenv("LESSON_PRICE", "80"))
IMAP_TIMEOUT = 15


def _get_email_body(msg) -> str:
    body = ""
    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            cd = str(part.get("Content-Disposition", ""))
            if ct == "text/plain" and "attachment" not in cd:
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    body += payload.decode(charset, errors="ignore")
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            charset = msg.get_content_charset() or "utf-8"
            body = payload.decode(charset, errors="ignore")
    return body


def parse_easypay_receipt(text: str) -> Optional[Dict]:
    if "счет-заказа" not in text and "Итого оплачено" not in text:
        return None

    result: Dict = {}

    # Receipt number
    m = re.search(r"Номер счет-заказа\s*[:\s]*([\w\d]+)", text)
    if m:
        result["receipt_number"] = m.group(1).strip()

    # Payer full name — убираем суффикс BY
    m = re.search(r"ФИО:\s*([А-ЯЁа-яёA-Za-z]+(?:\s+[А-ЯЁа-яёA-Za-z]+){1,2})", text)
    if m:
        name = m.group(1).strip()
        name = re.sub(r'\s+BY\s*$', '', name).strip()
        result["payer_name"] = name
    else:
        return None

    # Amount — два формата
    amount = None
    m = re.search(r"Итого оплачено[^:]*:\s*([\d]+[,\.][\d]+)", text, re.IGNORECASE)
    if m:
        try:
            amount = float(m.group(1).replace(",", "."))
        except ValueError:
            pass
    if amount is None:
        m = re.search(r"Сумма счета:\s*([\d]+[,\.][\d]+)", text, re.IGNORECASE)
        if m:
            try:
                amount = float(m.group(1).replace(",", "."))
            except ValueError:
                pass
    if amount is None:
        return None
    result["amount"] = amount

    return result


def _fetch_raw_receipts() -> List[Dict]:
    if not EMAIL_USER or not EMAIL_PASSWORD:
        logger.warning("Email credentials not configured — skipping inbox check.")
        return []

    old_timeout = socket.getdefaulttimeout()
    socket.setdefaulttimeout(IMAP_TIMEOUT)

    parsed: List[Dict] = []
    mail = None
    try:
        mail = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
        mail.login(EMAIL_USER, EMAIL_PASSWORD)
        mail.select("INBOX")

        # Только письма за последние 7 дней — не сканируем весь ящик
        since_date = (datetime.now() - timedelta(days=7)).strftime("%d-%b-%Y")
        _, message_ids = mail.search(None, f'(SINCE {since_date})')

        ids = message_ids[0].split()
        logger.info("Found %d emails in last 7 days", len(ids))

        for uid in ids:
            try:
                _, data = mail.fetch(uid, "(RFC822)")
                if not data or data[0] is None:
                    continue
                raw = data[0][1]
                msg = email.message_from_bytes(raw)

                body = _get_email_body(msg)
                receipt = parse_easypay_receipt(body)
                if receipt is None:
                    continue

                date_header = msg.get("Date", "")
                try:
                    receipt["payment_date"] = parsedate_to_datetime(date_header)
                except Exception:
                    receipt["payment_date"] = datetime.utcnow()

                receipt["raw_text"] = body[:3000]
                parsed.append(receipt)

            except Exception as e:
                logger.error("Failed to parse email uid=%s: %s", uid, e)

    except Exception as e:
        logger.error("IMAP connection error: %s", e)
    finally:
        if mail:
            try:
                mail.close()
                mail.logout()
            except Exception:
                pass
        socket.setdefaulttimeout(old_timeout)

    return parsed


async def _find_child_by_payer_name(payer_name: str, db: AsyncSession) -> Optional[int]:
    """
    Логика сопоставления:
    1. Ищем пользователя с ролью parent по фамилии+имени из чека
    2. Находим ParentProfile этого пользователя
    3. Через ParentChild получаем child_id
    """
    from app.models.models import User, ParentProfile, ParentChild, ChildProfile, RoleEnum

    parts = payer_name.strip().split()
    if len(parts) < 2:
        return None

    last_name = parts[0]
    first_name = parts[1]

    # Ищем родителя по фамилии и имени
    result = await db.execute(
        select(ParentProfile)
        .join(ParentProfile.user)
        .where(
            User.role == RoleEnum.parent,
            User.last_name.ilike(last_name),
            User.first_name.ilike(first_name),
        )
    )
    parent = result.scalar_one_or_none()

    if parent is None:
        # Мягкий поиск — фамилия без последней буквы (падежи)
        last_stem = last_name[:-1] if len(last_name) > 3 else last_name
        result = await db.execute(
            select(ParentProfile)
            .join(ParentProfile.user)
            .where(
                User.role == RoleEnum.parent,
                User.last_name.ilike(f"{last_stem}%"),
                User.first_name.ilike(first_name),
            )
        )
        parent = result.scalar_one_or_none()

    if parent is None:
        # Fallback for admin-created students: the payer may be a parent who has no
        # account/link yet. Match by child surname only when it points to exactly one child.
        last_stem = last_name[:-1] if len(last_name) > 3 else last_name
        result = await db.execute(
            select(ChildProfile)
            .join(ChildProfile.user)
            .where(
                User.role == RoleEnum.child,
                User.last_name.ilike(f"{last_stem}%"),
            )
        )
        children = result.scalars().all()
        if len(children) == 1:
            logger.info(
                "Matched payer '%s' to child_id=%s by unique child surname",
                payer_name, children[0].id,
            )
            return children[0].id
        if len(children) > 1:
            logger.warning(
                "Payer '%s' matched %d children by surname, cannot auto-match receipt",
                payer_name, len(children),
            )
        return None

    # Берём ребёнка этого родителя
    pc_result = await db.execute(
        select(ParentChild).where(ParentChild.parent_id == parent.id)
    )
    children = pc_result.scalars().all()

    if len(children) == 1:
        return children[0].child_id

    # Если детей несколько — логируем, не угадываем
    if len(children) > 1:
        logger.warning(
            "Parent %s has %d children, cannot auto-match receipt",
            payer_name, len(children)
        )
    return None


async def rematch_unlinked_receipts(db: AsyncSession) -> int:
    from app.models.models import EmailReceipt

    result = await db.execute(
        select(EmailReceipt).where(EmailReceipt.child_id.is_(None))
    )
    receipts = result.scalars().all()
    matched = 0

    for receipt in receipts:
        child_id = await _find_child_by_payer_name(receipt.payer_name, db)
        if child_id is None:
            continue
        receipt.child_id = child_id
        matched += 1

    if matched:
        await db.commit()

    logger.info("Email parser: rematched %d existing receipts.", matched)
    return matched


async def run_email_parse(db: AsyncSession) -> int:
    from app.models.models import EmailReceipt

    raw_receipts = _fetch_raw_receipts()
    if not raw_receipts:
        return await rematch_unlinked_receipts(db)

    saved = 0
    for r in raw_receipts:
        receipt_number = r.get("receipt_number")

        # Дедупликация по номеру чека
        if receipt_number:
            exists = await db.execute(
                select(EmailReceipt).where(EmailReceipt.receipt_number == receipt_number)
            )
        else:
            payment_date = r.get("payment_date")
            payment_date_only = payment_date.date() if hasattr(payment_date, 'date') else payment_date
            exists = await db.execute(
                select(EmailReceipt).where(
                    EmailReceipt.payer_name == r["payer_name"],
                    EmailReceipt.amount == r["amount"],
                    cast(EmailReceipt.payment_date, Date) == payment_date_only,
                )
            )

        existing_receipt = exists.scalar_one_or_none()
        if existing_receipt:
            if existing_receipt.child_id is None:
                child_id = await _find_child_by_payer_name(existing_receipt.payer_name, db)
                if child_id is not None:
                    existing_receipt.child_id = child_id
                    saved += 1
            continue

        # Сопоставление: ФИО родителя → ребёнок
        child_id = await _find_child_by_payer_name(r["payer_name"], db)

        if child_id is None:
            logger.warning("Could not match payer '%s' to any child", r["payer_name"])

        receipt_obj = EmailReceipt(
            receipt_number=receipt_number,
            payer_name=r["payer_name"],
            amount=r["amount"],
            payment_date=r.get("payment_date").replace(tzinfo=None) if r.get("payment_date") else None,
            raw_text=r.get("raw_text"),
            child_id=child_id,
        )
        db.add(receipt_obj)
        saved += 1

    if saved:
        await db.commit()

    rematched = await rematch_unlinked_receipts(db)
    logger.info("Email parser: saved/rematched %d receipts.", saved + rematched)
    return saved + rematched
